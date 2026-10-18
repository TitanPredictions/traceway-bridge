# titan → traceway bridge

Replaces the legacy [`titan-monitor`](#migration-off-titan-monitor) CLI
(CloudWatch Logs + EC2/CWAgent Metrics viewer) with a local Traceway stack
ingesting the same data via OpenTelemetry.

## Layout

```
titan/
├── README.md                       ← you are here
├── .env.example                    ← copy to .env and fill in
├── docker-compose.titan.yml        ← adds the two sidecars
├── otel-collector/
│   └── config.yaml                 ← CW Logs → OTLP pipeline
└── cw-metrics-exporter/
    ├── Dockerfile
    └── cw_metrics_exporter.py      ← CW Metrics → OTLP via boto3
```

## Architecture

```
        ┌──────────────────┐         ┌─────────────────┐
        │ CloudWatch Logs  │         │ CloudWatch      │
        │ /aws/ec2/.../api │         │ Metrics (EC2 +  │
        │ /aws/ec2/.../db  │         │  CWAgent)       │
        └─────────┬────────┘         └────────┬────────┘
                  │                          │ GetMetricData
                  │ poll                     │ every 60 s
                  ▼                          ▼
       ┌────────────────────┐     ┌────────────────────────┐
       │ OTel Collector     │     │ cw-metrics-exporter    │
       │ (contrib 0.119.0)  │     │ (Python, boto3 +       │
       │  awscloudwatch r/c │     │  OTel SDK)             │
       └─────────┬──────────┘     └───────────┬────────────┘
                 │ OTLP/HTTP                  │ OTLP/HTTP
                 │ (Bearer OTLP_TOKEN)        │ (Bearer OTLP_TOKEN)
                 ▼                            ▼
            ┌────────────────────────────────────────┐
            │ traceway                               │
            │ (port 80 dashboard, port 8082 API)     │
            │  └─ project: titan-ec2 / titan-cwlogs  │
            └────────────────────────────────────────┘
```

The two sidecars exist because the OTel Collector's
[`awscloudwatch`](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/receiver/awscloudwatchreceiver)
receiver only ingests **Logs**. There is no first-party OTel receiver for
the CloudWatch **Metrics** API — the only metric path is
[CloudWatch Metrics Streams → Kinesis Firehose → Prometheus scrape](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Metric-Streams.html),
which adds AWS-side plumbing (Firehose + IAM + CW Metrics Stream) we don't
need for a laptop viewer. `cw-metrics-exporter` keeps that small bit of
boto3 logic honest and containerised.

## Run it

### 1. Create the project + token in traceway

Traceway auths every OTLP request with a project bearer token. Create one
once after traceway's first boot:

1. Open <http://localhost:80> (the dashboard)
2. Register / log in
3. **Settings → Projects → New project**
   - Name: anything (e.g. `titan`)
   - Framework: **OpenTelemetry**
4. Copy the project token — that's `OTLP_TOKEN`

The token lives in two places (logs + metrics sidecars) and routes into
the same project by default. If you want them in different projects,
override `TRACEWAY_OTLP_ENDPOINT_LOGS` / `TRACEWAY_OTLP_ENDPOINT_METRICS`
in `.env`.

### 2. Fill in `.env`

```bash
cp titan/.env.example titan/.env
$EDITOR titan/.env
```

Required:

| Var | Where to get it |
|-----|-----------------|
| `AWS_ACCESS_KEY_ID` | Same key your `backend/config.ini [AWS]` uses |
| `AWS_SECRET_ACCESS_KEY` | Same |
| `AWS_REGION` | `eu-west-3` (matches `[DEFAULT]` in config.ini) |
| `EC2_INSTANCE_ID` | `[INFRASTRUCTURE] instance_id` in config.ini, else `aws ec2 describe-instances --region <r> --filters Name=instance-state-name,Values=running` |
| `TITAN_ENV` | `dev` on a laptop, `prod` on EC2 |
| `OTLP_TOKEN` | The project bearer token from step 1 |

`titan/.env` is gitignored — secrets never land in the traceway repo.

### 3. Bring up the stack

From the traceway repo root:

```bash
docker compose \
    -f docker-compose.yml \
    -f docker-compose.sqlite.yml \
    -f titan/docker-compose.titan.yml \
    --env-file titan/.env \
    up -d --build
```

The first `up` builds:
- traceway (Go + Node frontend, ~5 min)
- `cw-metrics-exporter` (Python slim + boto3 + OTel SDK, ~30 s)

The OTel Collector image (`otel/opentelemetry-collector-contrib:0.119.0`)
is pulled.

### 4. Verify

```bash
# All five containers up and healthy
docker compose -f docker-compose.yml -f docker-compose.sqlite.yml -f titan/docker-compose.titan.yml --env-file titan/.env ps

# traceway serves the dashboard
curl -sI http://localhost/health

# cw-metrics-exporter polls successfully (look for `poll ok:` lines)
docker logs -f titan-cw-metrics-exporter

# OTel collector subscribed to log groups (look for `starting:` + per-group info)
docker logs -f titan-otel-collector
```

If both sidecars are logging warnings instead of values, your AWS creds
are wrong — check `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` against
`backend/config.ini [AWS]`.

### 5. View data in the dashboard

Open <http://localhost/>, pick the project from step 1, then:

- **Metrics** → `system.cpu.utilization`, `system.memory.utilization`,
  `system.filesystem.utilization`, `aws.ec2.status_check.failed` —
  come from `cw-metrics-exporter`.
- **Logs** → events tagged with `service.name=titan-cloudwatch` and the
  CloudWatch log group name as an attribute — come from the OTel
  Collector.

Time picker matters. EC2 `CPUUtilization` is a 60-second metric, so a
`30m` preset shows ~30 points; CWAgent's `mem_used_percent` /
`disk_used_percent` are 60-second too. CloudWatch Logs use the message
timestamp.

## Tweaks

| Want | How |
|------|-----|
| Faster / slower metrics poll | `CW_POLL_INTERVAL_S=15` in `.env` (and `TraceWayWayOtlCollector.logs.poll_interval` in `titan/otel-collector/config.yaml` for logs) |
| Different log-group prefix | `titan/otel-collector/config.yaml` → `awscloudwatch.logs.groups.autodiscover.prefix` (default `/aws/ec2/titanprediction/`) |
| Different log streams | `titan/otel-collector/config.yaml` → `awscloudwatch.logs.groups.autodiscover.streams.prefixes` (defaults to `/api /api-errors /api-admin /db /services`) |
| Route logs and metrics to different projects | Set `TRACEWAY_OTLP_ENDPOINT_LOGS` / `TRACEWAY_OTLP_ENDPOINT_METRICS` in `.env` to two different traceway URLs and create two projects |
| Run traceway outside docker (macOS/Windows) | Override both `*_OTLP_ENDPOINT` env vars to `http://host.docker.internal:80/...` |
| Drop the collector (logs are off the table) | Comment out the `otel-collector:` block in `titan/docker-compose.titan.yml` |
| Drop the metrics sidecar | Comment out the `cw-metrics-exporter:` block |

## Migration off titan-monitor

The legacy `cli/monitor.py titan-monitor` (source files removed; the
`__pycache__` shows the menu) had six subcommands. Each one maps to a
traceway UI affordance:

| titan-monitor subcommand | What it did | traceway replacement |
|---------------|-------------|----------------------|
| `once` | one-shot health report (CPU, mem, disk, status checks) | Open <http://localhost/> → project → **Issues / Endpoints / Metrics**: query `system.cpu.utilization`, `system.memory.utilization`, `system.filesystem.utilization`, `aws.ec2.status_check.failed` |
| `watch` | live TUI dashboard, refresh every N s | Browser stays open; metrics update as the sidecars push. Use the time-range picker for `30m` |
| `groups` | list CW log groups + event counts | Dashboard → **Logs** filter by `service.name=titan-cloudwatch`; the `aws.cloudwatch.log.group.name` resource attr is searchable |
| `logs <group>` | read events from one CW log group | Dashboard → **Logs** → `service.name=titan-cloudwatch` + filter on log body / `severity_text` |
| `correlate` | overlay CPU/mem metrics with error log spikes on one timeline | Dashboard → **Metrics** + **Logs** side by side; both time-sync against `time-range` URL params |
| `graph` | 3-day multi-panel chart with live updates | Dashboard → time range `3d`, query all four CW metrics, group by metric name |

**Endpoints page (the gap `titan-monitor graph` left):** traceway's
**Endpoints** page is span-driven, not metric-driven — it only
materialises rows from server/INTERNAL spans that carry `http.route`.
The `cw-logs-metrics-exporter` sidecar pushes one synthetic INTERNAL
span per request log to `/api/otel/v1/traces`, which is enough for
traceway's `trace_converter` to build an `endpoints_v2` row per
`(method, route, status_code)` triple. **No OTel SDK in the Flask app
required.** Trade-offs: every span has a fresh random `trace_id` /
`span_id` (no link to browser-side `traceparent` headers), and there's
no waterfall view — but `/endpoints` populates with the same
P50/P95/P99 / error rate / counts as a properly-instrumented app.

**Subcommands without a direct equivalent**:

- `--follow` streaming. Traceway's log view auto-refreshes when the time
  window advances; for true tailing, run
  `aws logs tail <group> --follow` outside the stack.
- `--filter PAT` CloudWatch filter patterns. Traceway's log query syntax
  is OTel/Loki-flavoured; you can replicate most patterns with `severity_text`
  and body substring matches.

**State that did not survive**:

- The `--instance-id auto` flag (lookup the single running instance).
  Drop it; pin the instance in `.env` once.
- The `[yellow]config.ini has no real AWS keys[/yellow]` warning. The
  sidecars fail loudly instead of silently — if the dashboard shows no
  data, check `docker logs titan-cw-metrics-exporter`.

## What's NOT here (yet)

These are the natural next steps once the basic bridge works:

1. **OTel-instrument the Flask backend.** Add `opentelemetry-distro` +
   `opentelemetry-exporter-otlp-proto-http` to `backend/pyproject.toml`,
   point at `http://traceway:8082/api/otel/v1/{traces,metrics,logs}` with
   the same `OTLP_TOKEN`. Gives you traces + spans + app metrics instead
   of just EC2 + log messages.
2. **CloudWatch Logs Insights queries** via the dashboard's log search
   bar — the OTel Collector already maps the legacy groups.
3. **Run the bridge on the EC2 host** as a `docker compose` service,
   pulling the same image + config. Traceway's local instance then
   becomes a remote viewer; or move traceway to a small EC2 of its own.
4. **Add the legacy `infra_monitor` metrics** (CPU/memory/disk via
   psutil). They live on `titan-prediction/backend` as
   `services/infrastructure/monitor.py` and emit Discord alerts. The
   psutil data is already captured by the CWAgent, so this is mostly a
   parity exercise.

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| Sidecar logs `An error occurred (InvalidClientTokenId)` | AWS creds in `.env` are wrong or expired |
| Sidecar logs `An error occurred (AccessDenied)` on `GetMetricData` | IAM key lacks `cloudwatch:GetMetricData` (metrics sidecar) or `logs:*` (collector) |
| Collector logs `NoCredentialProviders` | `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` not set in `.env` |
| Dashboard empty for `titan-ec2` / `titan-cloudwatch` | `OTLP_TOKEN` wrong, or traceway project was deleted, or traceway isn't on `:80` (check `TRACEWAY_OTLP_ENDPOINT_*`) |
| `docker compose config` complains about `docker-compose.sqlite.yml` not found | Run from the traceway repo root (`cd /path/to/traceway`), not from inside `titan/` |
| OTel collector OOMKilled | `awscloudwatch` is streaming too many events; raise `logs.max_events_per_request` cap or narrow `groups.autodiscover.prefix` |

## File-by-file reference

- **`titan/otel-collector/config.yaml`** — pipeline config. Top-of-file
  comment block explains the design choices. Edit in place; `docker
  compose restart titan-otel-collector` picks up changes.
- **`titan/cw-metrics-exporter/cw_metrics_exporter.py`** — the
  boto3/OTel SDK sidecar. Single file, ~150 lines. Top-of-file docstring
  is the API contract.
- **`titan/docker-compose.titan.yml`** — adds the two sidecars; merges
  with `docker-compose.yml` + `docker-compose.sqlite.yml`. No service
  overrides on the upstream traceway image.
- **`titan/.env.example`** — annotated env-var template. Copy to `.env`.