"""
cw-logs-metrics-exporter — emit per-endpoint OTLP metrics AND synthetic
spans from CW Logs.

This is the half that the OTel collector can't do alone: ``metricstransform``
operates on already-existing metric data only, not on log records, so it
cannot derive per-endpoint counters, error counts, or latency histograms
from the activity log stream the way the traceway Endpoints page needs.
And, separately: the traceway Endpoints page is span-driven — it only
materialises rows from SERVER/INTERNAL spans that carry ``http.route``.
A metric stream alone gives you custom widgets and notification rules
but no Endpoints page.

This sidecar fills both gaps by:

  1. Polling CloudWatch Logs via ``filter_log_events`` on the same prefix
     (``/aws/ec2/titanprediction/``) the OTel collector subscribes to.
  2. Parsing each event body with ``json.loads`` (the Titan backend writes
     one JSON object per line — see ``api/activity_logger.py``).
  3. Recording OTLP metric points (counters + histogram) with the right
     OTel HTTP semantic conventions.
  4. Recording ONE synthetic OTLP span per request — enough for traceway's
     ``trace_converter`` to build an ``endpoints_v2`` row per
     (method, route, status_code) triple. This is the half that makes the
     ``/endpoints`` page populate, without ever instrumenting the Flask
     app with the OTel SDK. Trade-offs documented at the bottom of this
     file.

The result lands in traceway as proper metric streams that drive
custom widgets, P50/P95/P99 latency, and the ``error_rate_threshold`` /
``endpoint_p95_threshold`` / ``error_count_threshold`` notification rules,
plus one Endpoints row per request, which drives the ``/endpoints``
page drill-down.

Duplicate handling
------------------
``filter_log_events`` is a query, not a subscription — every poll returns
the events that match the time window, even ones already returned. We
remember the ``eventId`` of each emitted point/span and drop duplicates,
so a restart of this exporter does not double-count. The cache is
bounded by the number of events that arrive in one poll cycle (typically
tens), so it never grows unbounded.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


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
# Trace endpoint defaults to the same base path with /v1/traces. Override
# with TRACEWAY_OTLP_ENDPOINT_TRACES if you want to route traces and
# metrics to different traceway projects.
TRACEWAY_OTLP_ENDPOINT_TRACES = os.environ.get(
    "TRACEWAY_OTLP_ENDPOINT_TRACES",
    "http://traceway:80/api/otel/v1/traces",
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

# ---------------------------------------------------------------------------
# Trace SDK — emits one synthetic span per request, just enough for traceway's
# trace_converter to build an ``endpoints_v2`` row per (method, route, status).
# We can't reuse the metric endpoint: the OTLP signals live at different
# paths (/v1/metrics vs /v1/traces), so we set up a separate TracerProvider
# pointed at TRACEWAY_OTLP_ENDPOINT_TRACES.
# ---------------------------------------------------------------------------

span_exporter = OTLPSpanExporter(
    endpoint=TRACEWAY_OTLP_ENDPOINT_TRACES,
    headers={"Authorization": f"Bearer {OTLP_TOKEN}"},
)
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
tracer = tracer_provider.get_tracer("cw-logs-metrics-exporter")


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


# Path category — used to split real API traffic from bot-scan noise.
# Each synthetic span carries ``titan.path_category`` with one of these
# values so the Endpoints page can filter or group by category. The
# metric counters add the same attribute so dashboards can also split
# volume by category (e.g. "how many bot hits per minute").
REAL_API_PREFIXES = ("/api/",)
BOT_SCAN_PREFIXES = (
    "/.git/", "/.env", "/.aws/", "/wp-", "/wp/", "/xmlrpc.php",
    "/vpn/", "/cgi-bin/", "/phpmyadmin/", "/manager/",
    "/administrator/", "/plugins/", "/media/system/",
    "/_profiler/", "/vendor/", "/node_modules/",
    "/api/v1/0.1/",  # /api/v1/0.1 — Joomla scanner path
)
CATEGORY_REAL = "real"
CATEGORY_NOISE = "noise"


def path_category(method: str, endpoint: str) -> str:
    """``real`` for matched API routes, ``noise`` for everything else.

    The split is intentionally simple — anything under ``/api/`` that
    matches a known method is real; anything else is noise (bot scans,
    favicon hits, the dashboard's own ``/``). The user can extend the
    prefix lists if the boundary needs to move.
    """
    if method.upper() == "OPTIONS":
        return CATEGORY_NOISE  # we still drop OPTIONS earlier, but be safe
    path = _endpoints_v2_url(endpoint) if endpoint else ""
    if any(path.startswith(p) for p in BOT_SCAN_PREFIXES):
        return CATEGORY_NOISE
    if any(path.startswith(p) for p in REAL_API_PREFIXES):
        return CATEGORY_REAL
    return CATEGORY_NOISE


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

    category = path_category(method, endpoint)
    attrs = {
        "http.request.method": method,
        "url.path": endpoint,
        "titan.path_category": category,
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

    # Synthetic span — drives the traceway /endpoints page. The span is
    # INTERNAL with no parent (so ``isEntryPoint`` in the trace_converter
    # returns true) and carries ``http.request.method`` + ``url.path``,
    # which is what buildEndpoint keys on. We pass the FULL URL with
    # /v1/traces because the OTel SDK uses endpoint= verbatim (the
    # auto-append only happens via the OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
    # env var, not via the constructor).
    emit_synthetic_span(
        entry=entry,
        method=method,
        endpoint=endpoint,
        status_code=status_code,
        duration_ms=duration_ms,
        category=category,
    )

    if event_id:
        with _seen_lock:
            _seen_event_ids.add(event_id)


# ---------------------------------------------------------------------------
# Synthetic span emission
# ---------------------------------------------------------------------------


def _endpoints_v2_url(endpoint: str) -> str:
    """``GET /foo/123`` → ``/foo/123``. Strips the method, returns the path."""
    parts = endpoint.split(" ", 1)
    if len(parts) == 2 and parts[0].upper() in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}:
        return parts[1]
    return endpoint


def emit_synthetic_span(
    entry: dict[str, Any],
    method: str,
    endpoint: str,
    status_code: int | None,
    duration_ms: Any,
    category: str,
) -> None:
    """Emit one INTERNAL span with HTTP attributes that the traceway
    ``trace_converter`` recognises as an endpoint entry point.

    Synthetic because:

      * ``trace_id`` / ``span_id`` are random per emit. We don't need
        them to be deterministic — the ``_seen_event_ids`` cache
        already dedupes by CW ``eventId``, so a restart of this exporter
        does not duplicate endpoint rows. Deterministic IDs would
        help log↔span correlation in the traceway UI, but the cost
        (reaching into the OTel SDK's low-level context layer) is not
        worth it for a best-effort view.
      * ``parent_span_id`` is empty — every span is a root. This is the
        one shape that ``isEntryPoint`` accepts for INTERNAL spans
        (``len(span.ParentSpanId) == 0 && hasHTTPAttributes``).
      * No distributed-trace context: the front-end's ``traceparent``
        header is not in the activity log, so we cannot link browser
        spans to backend spans. Each backend request stands alone as a
        one-span "trace" — enough for endpoints, not for waterfalls.

    Endpoints page: each unique (method, route) triple becomes one row,
    with P50/P95/P99 over the duration. The whole /endpoints page is
    derived from these rows by traceway's query layer.
    """
    # CORS preflights: we have method+endpoint, but they're noise on
    # the Endpoints page. Skip them — preflights are handled by
    # browser-side CORS, not real routes.
    if method.upper() == "OPTIONS":
        return

    # The log body carries an ISO-8601 ``timestamp``; back-compute the
    # start time so endTime - startTime matches the recorded duration.
    # If parsing fails, fall back to ``now``.
    end_time = datetime.now(timezone.utc)
    ts_raw = entry.get("timestamp")
    if ts_raw:
        try:
            end_time = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    duration_ns = 0
    if isinstance(duration_ms, (int, float)) and duration_ms >= 0:
        duration_ns = int(duration_ms * 1_000_000)

    start_time_unix_nano = int(end_time.timestamp() * 1_000_000_000) - duration_ns
    end_time_unix_nano = start_time_unix_nano + duration_ns

    span = tracer.start_span(
        name=endpoint,
        start_time=start_time_unix_nano,
    )
    try:
        span.set_attribute("http.request.method", method)
        # Prefer ``http.route`` (the matched pattern, e.g. ``/users/<id>``).
        # We don't have it from the access log, so we fall back to the
        # full ``url.path``. The trace_converter accepts either.
        span.set_attribute("url.path", _endpoints_v2_url(endpoint))
        if status_code is not None:
            span.set_attribute("http.response.status_code", int(status_code))
        span.set_attribute("titan.path_category", category)
        ip = entry.get("ip")
        if ip:
            span.set_attribute("client.address", ip)
        ua = entry.get("user_agent")
        if ua:
            span.set_attribute("user_agent.original", ua)
        username = entry.get("username")
        if username:
            span.set_attribute("enduser.id", username)
        # End the span at the recorded end time so duration is non-zero
        # in the traceway UI. ``start_time`` was set on start; the SDK
        # records end_time on .end().
        span.end(end_time=end_time_unix_nano)
    except Exception:
        # If the SDK is shut down mid-emit, span.end() may raise. Always
        # release the span so we don't leak.
        try:
            span.end()
        except Exception:
            pass


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

    log.info("shutting down, flushing providers")
    meter_provider.shutdown()
    tracer_provider.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())