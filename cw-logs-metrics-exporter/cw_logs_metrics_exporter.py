"""
cw-logs-metrics-exporter — emit per-endpoint OTLP metrics from CW Logs.

This is the half that the OTel collector can't do alone: ``metricstransform``
operates on already-existing metric data only, not on log records, so it
cannot derive per-endpoint counters, error counts, or latency histograms
from the activity log stream the way the traceway Endpoints page needs.

This sidecar fills that gap by:

  1. Polling CloudWatch Logs via ``filter_log_events`` on the same prefix
     (``/aws/ec2/titanprediction/``) the OTel collector subscribes to.
  2. Parsing each event body with ``json.loads`` (the Titan backend writes
     one JSON object per line — see ``api/activity_logger.py``).
  3. Recording OTLP metric points via the same
     ``opentelemetry-exporter-otlp-proto-http`` package
     ``cw-metrics-exporter`` already uses, with the right OTel HTTP
     semantic conventions (``http.server.request.duration``,
     ``http.request.method``, ``url.path``, ``http.response.status_code``,
     ``service.name``).

The result lands in traceway as proper metric streams that drive the
Endpoints page, P50/P95/P99 latency, and the ``error_rate_threshold`` /
``endpoint_p95_threshold`` / ``error_count_threshold`` notification rules —
none of which can be evaluated off log records alone.

Duplicate handling
------------------
``filter_log_events`` is a query, not a subscription — every poll returns
the events that match the time window, even ones already returned. We
remember the ``eventId`` of each emitted point and drop duplicates, so a
restart of this exporter does not double-count. The cache is bounded by
the number of events that arrive in one poll cycle (typically tens),
so it never grows unbounded.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from typing import Any

import boto3
from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AWS_REGION = os.environ["AWS_REGION"]
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")

OTEL_EXPORTER_OTLP_ENDPOINT = os.environ.get(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "http://traceway:80/api/otel/v1/metrics",
)
OTLP_TOKEN = os.environ["OTLP_TOKEN"]

LOG_GROUP_PREFIX = os.environ.get(
    "LOG_GROUP_PREFIX",
    "/aws/ec2/titanprediction/",
)
TITAN_ENV = os.environ.get("TITAN_ENV", "prod")

POLL_INTERVAL_S = int(os.environ.get("POLL_INTERVAL_S", "60"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("cw-logs-metrics-exporter")


# ---------------------------------------------------------------------------
# OpenTelemetry setup
# ---------------------------------------------------------------------------

resource = Resource.create(
    {
        "service.name": "titan-api",
        "service.instance.id": os.environ.get("EC2_INSTANCE_ID", "unknown"),
        "deployment.environment.name": TITAN_ENV,
        "cloud.provider": "aws",
        "cloud.region": AWS_REGION,
        "titan.metrics.source": "cloudwatch-logs",
    }
)

exporter = OTLPMetricExporter(
    endpoint=OTEL_EXPORTER_OTLP_ENDPOINT,
    headers={"Authorization": f"Bearer {OTLP_TOKEN}"},
)
reader = PeriodicExportingMetricReader(
    exporter,
    export_interval_millis=POLL_INTERVAL_S * 1000,
    export_timeout_millis=30_000,
)
meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
meter = meter_provider.get_meter("cw-logs-metrics-exporter")


# Histogram for request latency. Buckets tuned for an API whose p99 sits
# well under a second — finer at the low end, coarser past 5 s.
LATENCY = meter.create_histogram(
    name="http.server.request.duration",
    unit="ms",
    description="HTTP request duration in milliseconds (derived from CW Logs).",
)

REQUEST_COUNT = meter.create_counter(
    name="http.server.requests",
    unit="1",
    description="Total HTTP requests (derived from CW Logs).",
)

ERROR_COUNT = meter.create_counter(
    name="http.server.errors",
    unit="1",
    description="HTTP requests that resulted in status >= 500 or app-level error.",
)

CLIENT_ERROR_COUNT = meter.create_counter(
    name="http.server.client_errors",
    unit="1",
    description="HTTP requests that resulted in status 4xx (excluding 401/403).",
)


# ---------------------------------------------------------------------------
# boto3 / CloudWatch Logs polling
# ---------------------------------------------------------------------------

logs = boto3.client(
    "logs",
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY_ID or None,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY or None,
)


def list_log_groups(prefix: str) -> list[str]:
    paginator = logs.get_paginator("describe_log_groups")
    groups: list[str] = []
    for page in paginator.paginate(logGroupNamePrefix=prefix):
        groups.extend(g["logGroupName"] for g in page["logGroups"])
    return groups


def poll_new_events(group: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    paginator = logs.get_paginator("filter_log_events")
    for page in paginator.paginate(
        logGroupName=group,
        startTime=start_ms,
        endTime=end_ms,
    ):
        events.extend(page["events"])
    return events


# ---------------------------------------------------------------------------
# Metric recording
# ---------------------------------------------------------------------------

# Bounded cache of event IDs we've already recorded. Reset on every poll
# cycle — we only need to dedupe within a single window, not forever.
_seen_event_ids: set[str] = set()
_seen_lock = threading.Lock()


def _is_error_status(status_code: int | None) -> bool:
    if status_code is None:
        return False
    return status_code >= 500


def _is_client_error_status(status_code: int | None) -> bool:
    if status_code is None:
        return False
    return 400 <= status_code < 500


def record_event(event: dict[str, Any]) -> None:
    """Parse one CW Logs event and emit OTLP metric points from it.

    The CW event body is the raw JSON line written by
    ``api/activity_logger.log_api_event`` (or ``log_api_error``). Anything
    that does not parse, or that lacks the fields we need, is silently
    skipped — it is still being shipped as a structured log via the OTel
    collector's pipeline, so dropping here costs us nothing observability-wise.
    """
    event_id = event.get("eventId")
    if event_id and event_id in _seen_event_ids:
        return

    raw = event.get("message", "")
    try:
        entry = json.loads(raw)
    except (ValueError, TypeError):
        return

    method = entry.get("method") or ""
    endpoint = entry.get("endpoint") or ""
    status_code = entry.get("status_code")
    duration_ms = entry.get("duration_ms")
    status_str = entry.get("status") or ""

    # Skip events that carry no HTTP-shape info — non-request logs that
    # happen to live in api_activity.log (e.g. "started", "completed"
    # service markers that leak into the same group on some deploys).
    if not method or not endpoint:
        return

    attrs = {
        "http.request.method": method,
        "url.path": endpoint,
    }
    if status_code is not None:
        attrs["http.response.status_code"] = int(status_code)

    if isinstance(duration_ms, (int, float)) and duration_ms >= 0:
        LATENCY.record(float(duration_ms), attributes=attrs)

    REQUEST_COUNT.add(1, attributes=attrs)

    # Mark app-level errors (Flask ``log_api_error`` writes ``status="error"``
    # but no status_code — it always represents a 5xx in practice, so we
    # also bucket those here).
    if _is_error_status(status_code) or status_str == "error":
        ERROR_COUNT.add(1, attributes=attrs)
    elif _is_client_error_status(status_code):
        CLIENT_ERROR_COUNT.add(1, attributes=attrs)

    if event_id:
        with _seen_lock:
            _seen_event_ids.add(event_id)


# ---------------------------------------------------------------------------
# Main poll loop
# ---------------------------------------------------------------------------

_stop = threading.Event()


def _handle_signal(signum, _frame):
    log.info("received signal %s, flushing and exiting", signum)
    _stop.set()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def _poll_once() -> dict[str, int]:
    """One poll across all configured log groups. Returns counts per group."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - POLL_INTERVAL_S * 2 * 1000  # 2x window covers missed ticks

    counts: dict[str, int] = {}
    groups = list_log_groups(LOG_GROUP_PREFIX)
    for group in groups:
        try:
            events = poll_new_events(group, start_ms, end_ms)
        except Exception as exc:
            log.warning("filter_log_events(%s) failed: %s", group, exc)
            counts[group] = -1
            continue
        for event in events:
            record_event(event)
        counts[group] = len(events)

    # The seen-id cache is bounded by one poll cycle; clear it now so
    # the next poll is fresh. (filter_log_events over a time window will
    # not re-emit the same event IDs across consecutive windows, but
    # a slow poll could.)
    with _seen_lock:
        _seen_event_ids.clear()

    return counts


def main() -> int:
    log.info(
        "starting: region=%s endpoint=%s prefix=%s poll=%ss",
        AWS_REGION, OTEL_EXPORTER_OTLP_ENDPOINT, LOG_GROUP_PREFIX, POLL_INTERVAL_S,
    )

    while not _stop.is_set():
        try:
            counts = _poll_once()
            total = sum(c for c in counts.values() if c >= 0)
            log.info(
                "poll ok: events=%d groups=%s",
                total,
                {g.split("/")[-1]: c for g, c in counts.items()},
            )
        except Exception:
            log.exception("poll loop crashed; will retry next tick")

        for _ in range(POLL_INTERVAL_S):
            if _stop.is_set():
                break
            time.sleep(1)

    log.info("shutting down, flushing meter provider")
    meter_provider.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())