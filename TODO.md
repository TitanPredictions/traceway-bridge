# traceway ↔ TitanPrediction bridge — TODO

Last updated: 2026-10-21. Tracks follow-on work after the bridge was
split out of the `traceway` clone into this standalone repo (item 4).

---

## Immediate (next sprint)

### 1. Add `app_version` to synthetic spans

Each span currently has `service.name=titan-api` and `service.instance.id`
but no `service.version`. traceway's `endpoints_v2.app_version` column is
left empty, so release-comparison dashboards can't split "endpoint looks
slow today" by deploy.

**Action:** read `TITAN_APP_VERSION` env var (set at gunicorn launch from
`$(git rev-parse --short HEAD)` in the systemd unit) and stamp it on
the span resource. Falls back to `"unknown"` so the column is never
empty. **No backend/ change needed** — entirely in
`cw-logs-metrics-exporter/cw_logs_metrics_exporter.py`.

### 2. Wire a TitanPrediction-specific dashboard template

The `EC2 System Monitoring` dashboard shipped in commit
`a34918f` is generic. The real TitanPrediction needs a dashboard with:

- Per-endpoint P50/P95 over the last 1h / 6h / 24h
- `titan.path_category` split: real vs noise, with both counts visible
  on the same panel
- Bot-scan volume alert widget: `count` of `titan.api.requests` with
  `titan.path_category=noise` over a 1-minute window
- EC2 host metrics row (CPU / mem / disk) as a baseline
- Notification-rule status row (green/red chips for each rule)

**Action:** build a JSON dashboard with the same `endpoints_v2` /
`metric_points` schema as the existing templates; commit it under
`dashboards/titanprediction.json`. Apply via
`POST /api/dashboards/populate-defaults` after creating the project.

### 3. Set up notification rules

Five rules cover the basic operational needs. Each is one row in
`notification_rules` with a `notification_channel_id` pointing at a
Discord/Slack/email channel (channels live in `notification_channels`):

| Rule type | Condition | Why |
|---|---|---|
| `error_rate_threshold` | `http.server.errors / http.server.requests > 0.05` over 5 min, per service | Real API errors |
| `endpoint_p95_threshold` | `http.server.request.duration p95 > 1000ms` over 5 min | Latency regression |
| `metric_threshold` | `titan.api.requests` with `titan.path_category=noise` > 100/min | Bot flood |
| `error_count_threshold` | `http.server.errors` > 10 in 5 min | Error burst |
| `no_data` | `system.cpu.utilization` no points in 5 min | Agent down |

**Action:** scripted setup (one-shot curl per rule) — see
[setup-notification-rules.sh](scripts/setup-notification-rules.sh)
(stub — to write).

---

## Short term (next month)

### 4. ~~Move `titan/` out of the traceway clone into a standalone repo~~ — **done**

This *is* the standalone repo: `TitanPredictions/traceway-bridge`, split out of
the `traceway` clone with `git subtree split` and restructured in
`aa281133`. Done:

- Repo created, `titan/` split into it as `main`, build contexts and paths
  flattened to the repo root.
- `Makefile` (`up` / `logs` / `down` / `token`) and `CONTRIBUTING.md` added.
- CI (`.github/workflows/ci.yml`) runs `py_compile` on both sidecars and
  validates `otel-collector/config.yaml` with the contrib image.

Two things the split deliberately did **not** do:

- The compose stack still needs a traceway checkout alongside it — the two
  upstream compose files (`docker-compose.yml`, `docker-compose.sqlite.yml`)
  are traceway's, and `TRACEWAY_OTLP_ENDPOINT_BASE=http://traceway:80/api/otel`
  resolves by service name on the shared network. Running the published
  traceway image and `docker network connect`ing the bridge to it is the
  remaining step to make this truly single-repo.
- `titan/README.md` inside the old traceway clone is retained for two release
  cycles so the team can switch over, then deleted.

Historical detail on what the coupling was, and what broke: see
[`SESSION-2026-10-21-log-search.md`](./SESSION-2026-10-21-log-search.md).

### 5. Dev-team onboarding doc

Each new dev runs the bridge on their own laptop (or in a shared
staging box). The current `README.md` covers the run-it steps
but not the onboarding flow. Add `docs/onboarding.md`:

- How to create a traceway account (cloud or local) and grab a token
- How to populate their `.env` from `backend/config.ini`
  (the same script we ran during setup)
- How to point the backend at the bridge (via tunnel, ngrok, or
  a shared host)
- Troubleshooting matrix: token expired, AWS throttling, OTLP
  exporter connection refused, etc.

**Status:** partially covered by `README.md` Troubleshooting
section. Extend with the dev-specific scenarios.

### 6. CI for the bridge sidecars

Add `.github/workflows/test.yml` in the new repo:

- `python -m pytest` for the sidecar modules (currently zero tests —
  the sidecars rely on integration testing against real CW)
- Mock-based unit tests for the JSON parser, the path categoriser,
  and the trace-id hashing — fast feedback on PRs
- `make smoke-test` that spins up the bridge against a fixture
  CloudWatch account or localstack

**Blocker:** the existing `cw_logs_metrics_exporter.py` is hard to
unit-test because it pulls in boto3 / OTel SDK at module import.
Refactor: split the parsing/categorisation logic into a pure-function
module that the sidecar imports.

---

## Medium term (next quarter)

### 7. Span/log correlation in traceway

The synthetic spans have random `trace_id` / `span_id`; the structured
logs (from the OTel collector) have whatever IDs the collector
assigned. The two streams don't link, so the traceway log-↔-span view
shows nothing.

**Fix:** record the synthetic `trace_id` / `span_id` in the
activity-log JSON body (under a `titan.trace_id` /
`titan.span_id` pair). The OTel collector's transform can then set
the log record's `trace_id` / `span_id` fields from these. End result:
clicking a log line takes you to its span (and vice versa).

### 8. Trace propagation from front-end

Right now the front-end's `traceparent` header is dropped at the
browser → API boundary because nothing reads it. Even with the bridge
running, every backend request is a one-span "trace" with no
upstream context.

**Path 1 (cheap):** add a tiny Flask `before_request` hook that
parses `traceparent` and stores the trace_id in `request.environ`;
the activity logger stamps it into the log JSON. The collector
links the log back to whatever trace_id the front-end sent, even
if the backend itself doesn't propagate.

**Path 2 (proper):** install the OTel SDK on the Flask backend and
on `titan-ai-forecasts` / `titan-admin-hub` so spans form a real
distributed trace. The bridge becomes redundant for traces but
keeps its value for the cheap observability (logs, host metrics,
bot noise).

### 9. Cardinality control

Each request creates:
- 1 metric point × 4 attribute dimensions (`method`, `url.path`,
  `status_code`, `path_category`) — bounded by the number of unique
  (method, path, status, category) tuples. Realistic: a few hundred
  for a busy API.
- 1 span row in `endpoints_v2` — same key set, same cardinality.
- 1 log row in `log_records` — unbounded if the API gets chatty.
  Traceway's default 30-day SQLite retention is 10–30 GB at our
  rate; if you exceed, push retention down or shard across multiple
  `metric_points` partitions.

**Action:** add a per-second rate cap in
`cw-logs-metrics-exporter.py`: drop synthetic-span emission
(metric still goes through) if the poll batch exceeds N events.
Keeps the Endpoints page readable under attack. Make N configurable.

### 10. Retention / storage review

Default `SQLITE_RETENTION_DAYS=30` is too long for high-cardinality
endpoint data and too short for a bot-noise baseline. Two policies:
- `endpoints_v2` and `spans_v2`: 7 days (we keep the high-cardinality
  recent view, drop the rest)
- `metric_points`: 90 days (one point per minute per metric, low
  cardinality, cheap to keep)
- `log_records`: 7 days, then ship to S3 via a sidecar cron (cold
  storage; same pattern as the existing `cwlogs_archive` service)

---

## Long term (no fixed date)

### 11. OTel SDK on the backend

The bridge is a workaround for the lack of an OTel SDK in the
Flask app. Once the team is comfortable with traceway, install
`opentelemetry-distro` on the backend:

```bash
cd backend
poetry add opentelemetry-distro opentelemetry-exporter-otlp
poetry run opentelemetry-bootstrap -a install
OTEL_SERVICE_NAME=titan-api \
OTEL_EXPORTER_OTLP_ENDPOINT=http://traceway:8082/api/otel \
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
poetry run opentelemetry-instrument gunicorn wsgi:app
```

At that point, the `cw-logs-metrics-exporter` sidecar's span
synthesiser becomes unnecessary — the SDK already emits real spans
with `http.route` from Flask's URL matcher. The sidecar can shrink
to: metric emission only (counters/histograms) + structured-log
forwarding.

### 12. Front-end OTel

`@tracewayapp/browser-sdk` (or `@opentelemetry/instrumentation-fetch`
+ `@opentelemetry/exporter-trace-otlp-http`) on `titan-ai-forecasts`
and `titan-admin-hub`. Unlocks:

- Real distributed traces from browser click → API → DB → AI
- Web vitals, page load timing, JS errors
- Session replay (traceway supports it)

### 13. Service-map / dependency graph

traceway auto-builds a service map from spans. With the bridge, only
`titan-api` exists as a node. With OTel SDK on the backend AND the
DB, the map grows to `titan-frontend → titan-api → titan-db`. With
the AI agent, a third node appears. This is the "everything
connected" view that needs real spans everywhere.

### 14. Self-hosted traceway on a stable host

The bridge currently runs on a dev laptop. For production this is
fragile. Two paths:

- **Run on EC2 alongside the backend** — simplest, but the EC2
  needs ~500 MB of RAM for the traceway + ClickHouse + Postgres
  stack, plus disk for 30-day retention. Verify the existing
  `t3.medium` (or whatever's running) has headroom.
- **Run on a small separate VM** (a `t3.nano` is enough for ~100
  req/s) with the docker-compose stack, security-group locked to
  EC2's IP and the team's IPs. Most flexibility for the dev team
  to add their own dashboards without touching prod.

### 15. Alert routing

Wire the notification channels:
- `#alerts-critical` Slack channel → high-priority rules (errors,
  latency regression, bridge down)
- `#alerts-noise` Slack channel → bot noise spike
- Email digest → daily summary of endpoint latency percentiles

Each rule type in traceway is independent; one rule → one channel
is the simplest mapping.

---

## Open questions

- **Repo name** — `traceway-bridge` is my suggestion. The team may
  prefer `titan-observability` or `titan-cloudwatch-bridge`. Open a
  GitHub org-level decision before creating.
- **Storage** — SQLite is fine until ~50 M rows. Beyond that,
  migrate to ClickHouse (the traceway container bundles the
  ClickHouse stack; we'd just stop using the SQLite file). The
  `docker-compose.sqlite.yml` is named "sqlite" precisely so
  flipping to `docker-compose.yml` (all-in-one) is a one-liner.
- **Schema migration** — when the sidecar gains a new attribute
  (e.g. `app_version` above), the dashboard widgets need to be
  updated too. Worth a `make dashboards-refresh` rule that hits
  `POST /api/dashboards/:id/apply`.
- **Tenancy** — the bridge emits a single `service.name=titan-api`.
  If a future service (say `titan-worker`) wants to use the same
  bridge, it would need a separate project + token. Decide whether
  the bridge is one-project-per-service or shared.
