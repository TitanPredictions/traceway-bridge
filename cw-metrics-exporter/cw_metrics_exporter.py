"""
cw-metrics-exporter — poll CloudWatch Metrics and ship them to traceway via OTLP.

Replaces the EC2 + CloudWatch-Agent half of the legacy
``titan-monitor once|watch|correlate|graph`` CLI. The CloudWatch Logs half
is handled by the OTel Collector (see ``titan/otel-collector/``); the
metrics API has no first-party OTel receiver, so we poll ``GetMetricData``
ourselves with boto3 and push through the OTel Python SDK.

The metric set matches what ``titan-monitor`` rendered:

* AWS/EC2 ``CPUUtilization`` — Average, 60 s
* AWS/EC2 ``StatusCheckFailed`` — Average, 60 s
* CloudWatch Agent ``mem_used_percent`` — Average, 60 s
* CloudWatch Agent ``disk_used_percent`` — Average, 60 s

Configuration is 100% env vars so docker-compose can inject everything
from ``titan/.env`` without rebuilding the image.

Run directly::

    AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \\
    AWS_REGION=eu-west-3 EC2_INSTANCE_ID=i-xxxxxxxx \\
    OTLP_ENDPOINT=http://traceway:80/api/otel/v1/metrics \\
    OTLP_TOKEN=... \\
    python cw_metrics_exporter.py
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource


# ---------------------------------------------------------------------------
# Config (all from env — see docker-compose.titan.yml)
# ---------------------------------------------------------------------------

AWS_REGION = os.environ["AWS_REGION"]
EC2_INSTANCE_ID = os.environ["EC2_INSTANCE_ID"]
TITAN_ENV = os.environ.get("TITAN_ENV", "dev")
OTLP_ENDPOINT = os.environ.get(
    "OTLP_ENDPOINT", "http://traceway:80/api/otel/v1/metrics"
)
OTLP_TOKEN = os.environ["OTLP_TOKEN"]
POLL_INTERVAL_S = int(os.environ.get("POLL_INTERVAL_S", "60"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("cw-metrics-exporter")


# ---------------------------------------------------------------------------
# OTel — ship every poll as a counter/gauge update
# ---------------------------------------------------------------------------

resource = Resource.create(
    {
        "service.name": "titan-ec2",
        "service.instance.id": EC2_INSTANCE_ID,
        "deployment.environment.name": TITAN_ENV,
        "host.name": EC2_INSTANCE_ID,
        "cloud.provider": "aws",
        "cloud.region": AWS_REGION,
        "titan.metrics.source": "cloudwatch",
    }
)

# OTLPMetricExporter(endpoint=...) takes the URL verbatim and does NOT
# append /v1/metrics — that auto-append only happens when the endpoint
# comes from OTEL_EXPORTER_OTLP_ENDPOINT. Earlier we stripped the suffix
# expecting the SDK to re-add it, which it does not, and every export
# POSTed to bare /api/otel for a 404.
exporter = OTLPMetricExporter(
    endpoint=OTLP_ENDPOINT,
    headers={"Authorization": f"Bearer {OTLP_TOKEN}"},
)
reader = PeriodicExportingMetricReader(
    exporter,
    export_interval_millis=POLL_INTERVAL_S * 1000,
    export_timeout_millis=30_000,
)
meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
meter = meter_provider.get_meter("cw-metrics-exporter")

cpu_util = meter.create_gauge(
    name="system.cpu.utilization",
    description="EC2 CPUUtilization (Average, percent 0-100).",
    unit="%",
)
status_check_failed = meter.create_gauge(
    name="aws.ec2.status_check.failed",
    description="EC2 StatusCheckFailed (any instance or system check fails).",
    unit="1",
)
mem_used_pct = meter.create_gauge(
    name="system.memory.utilization",
    description="CloudWatch Agent mem_used_percent (0-100).",
    unit="%",
)
disk_used_pct = meter.create_gauge(
    name="system.filesystem.utilization",
    description="CloudWatch Agent disk_used_percent (0-100).",
    unit="%",
)


# ---------------------------------------------------------------------------
# boto3 — pull the same four metrics titan-monitor rendered
# ---------------------------------------------------------------------------

cw = boto3.client("cloudwatch", region_name=AWS_REGION)

# Cache of metric name -> full Dimensions list, so we don't pay a ListMetrics
# round-trip on every poll. Cleared when _lookup_dimensions can't find a
# match — i.e. when the agent has changed its shape since the last poll.
_DIM_CACHE: dict[tuple[str, str], list[dict[str, str]]] = {}


def _lookup_dimensions(metric_name: str, namespace: str) -> list[dict[str, str]]:
    """
    Find the exact dimension set CloudWatch currently publishes for
    ``(namespace, metric_name, InstanceId=EC2_INSTANCE_ID)``.

    CWAgent attaches per-resource dimensions (e.g. disk gets
    ``path``/``device``/``fstype``), so we cannot query with
    ``InstanceId`` alone — GetMetricData returns zero points. AWS/EC2
    native metrics (``CPUUtilization``, ``StatusCheckFailed``) only carry
    ``InstanceId`` and the bare lookup is fine for them.
    """
    key = (namespace, metric_name)
    cached = _DIM_CACHE.get(key)
    if cached is not None:
        return cached

    paginator = cw.get_paginator("list_metrics")
    for page in paginator.paginate(
        Namespace=namespace,
        Dimensions=[{"Name": "InstanceId", "Value": EC2_INSTANCE_ID}],
        MetricName=metric_name,
    ):
        for m in page.get("Metrics", []):
            dims = [d for d in m["Dimensions"] if d["Name"] != "InstanceId"]
            _DIM_CACHE[key] = dims
            return dims

    # No match — caller will see zero points and log "no data points for X".
    _DIM_CACHE.pop(key, None)
    return []


def _query(metric_name: str, namespace: str) -> list[dict[str, Any]]:
    """
    Call ``GetMetricData`` for one metric name on the pinned EC2 instance.

    We ask for a single ``Average`` data point over the last ``2 *
    POLL_INTERVAL_S`` so a missed poll still produces a value. The
    CloudWatch API enforces a 60-second minimum resolution on ``Average``
    for EC2 metrics, so we never request finer than that.

    Dimensions are looked up via ``_lookup_dimensions`` — the CWAgent
    attaches per-resource dimensions (path, device, fstype) that GetMetricData
    requires to match exactly; querying with InstanceId alone silently
    returns zero points.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=POLL_INTERVAL_S * 2)
    extra_dims = [
        {"Name": d["Name"], "Value": d["Value"]}
        for d in _lookup_dimensions(metric_name, namespace)
    ]
    resp = cw.get_metric_data(
        StartTime=start,
        EndTime=end,
        MetricDataQueries=[
            {
                "Id": "m",
                "MetricStat": {
                    "Metric": {
                        "Namespace": namespace,
                        "MetricName": metric_name,
                        "Dimensions": [
                            {"Name": "InstanceId", "Value": EC2_INSTANCE_ID},
                            *extra_dims,
                        ],
                    },
                    "Period": POLL_INTERVAL_S,
                    "Stat": "Average",
                },
                "ReturnData": True,
            },
        ],
    )
    return resp.get("MetricDataResults", [{}])[0].get("Values", [])


def poll_once() -> dict[str, float | None]:
    """One polling pass. Returns the values it observed (mostly for logging)."""
    out: dict[str, float | None] = {}
    for metric_name, otel_gauge, namespace in (
        ("CPUUtilization", cpu_util, "AWS/EC2"),
        ("StatusCheckFailed", status_check_failed, "AWS/EC2"),
        ("mem_used_percent", mem_used_pct, "CWAgent"),
        ("disk_used_percent", disk_used_pct, "CWAgent"),
    ):
        try:
            values = _query(metric_name, namespace)
        except Exception as e:
            log.warning("GetMetricData(%s) failed: %s", metric_name, e)
            out[metric_name] = None
            continue

        if not values:
            log.debug("no data points for %s in the last window", metric_name)
            out[metric_name] = None
            continue

        # CW returns most-recent first; the gauge is the latest observation.
        latest = float(values[0])
        otel_gauge.set(latest)
        out[metric_name] = latest

    log.info("poll ok: %s", ", ".join(f"{k}={v}" for k, v in out.items()))
    return out


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_stop = threading.Event()


def _handle_signal(signum, _frame):
    log.info("received signal %s, flushing and exiting", signum)
    _stop.set()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def main() -> int:
    log.info(
        "starting: region=%s instance=%s env=%s endpoint=%s poll=%ss",
        AWS_REGION, EC2_INSTANCE_ID, TITAN_ENV, OTLP_ENDPOINT, POLL_INTERVAL_S,
    )

    while not _stop.is_set():
        try:
            poll_once()
        except Exception:
            log.exception("poll loop crashed; will retry next tick")

        # sleep in 1-second slices so SIGTERM is responsive
        for _ in range(POLL_INTERVAL_S):
            if _stop.is_set():
                break
            time.sleep(1)

    log.info("shutting down, flushing meter provider")
    meter_provider.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())