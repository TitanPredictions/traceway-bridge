# traceway-bridge — TitanPrediction observability

Replaces the legacy [`titan-monitor`](#migration-off-titan-monitor) CLI
(CloudWatch Logs + EC2/CWAgent Metrics viewer) with a local Traceway stack
ingesting the same data via OpenTelemetry.



```
traceway-bridge/
├── README.md                       ← you are here
├── TODO.md                         ← roadmap / open work
├── SESSION-2026-10-21-log-search.md ← log-search work: changes, gotchas, open items
├── CONTRIBUTING.md                 ← dev guide
├── Makefile                        ← make up / logs / down / token
├── .env.example                    ← copy to .env and fill in
├── docker-compose.titan.yml        ← the three sidecars + the OTel collector
├── otel-collector/
│   └── config.yaml                 ← CW Logs → OTLP pipeline + synthetic span transform
├── cw-metrics-exporter/
│   ├── Dockerfile
│   └── cw_metrics_exporter.py      ← CW Metrics → OTLP via boto3
└── cw-logs-metrics-exporter/
    ├── Dockerfile
    └── cw_logs_metrics_exporter.py  ← CW Logs → OTLP metrics + synthetic spans
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
cp .env.example .env
$EDITOR .env
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

`./.env` is gitignored — secrets never land in the traceway repo.

### 3. Bring up the stack

The bridge still needs the two upstream traceway compose files, which live in
a traceway checkout one level up (see the two items left open in
[TODO #4](./TODO.md)). From **this** repo:

```bash
make up
```

which is:

```bash
docker compose --project-directory . \
    -f ../traceway/docker-compose.yml \
    -f ../traceway/docker-compose.sqlite.yml \
    -f docker-compose.titan.yml \
    --env-file .env \
    up -d --build
```

> **`--project-directory .` is not optional.** Compose otherwise takes the
> project directory from the *first* `-f` file — `../traceway` — and resolves
> this repo's relative paths (the two sidecar build contexts, the collector
> config bind mount) against the traceway checkout, where they do not exist.
> If you prefer to invoke compose yourself, either keep the flag or use
> absolute paths for all three files.

The first `up` builds:
- traceway (Go + Node frontend, ~5 min)
- `cw-metrics-exporter` (Python slim + boto3 + OTel SDK, ~30 s)

The OTel Collector image (`otel/opentelemetry-collector-contrib:0.119.0`)
is pulled.

> **After any collector config edit**, `docker compose restart otel-collector`
> — not `up --build`, which rebuilds images you did not touch (~143 s for
> nothing) and does not necessarily recreate the collector. The collector
> parses its config **only at startup**, so a bind-mounted config change that
> skips a restart looks like it succeeded and then does nothing. Run
> `make validate` first.

### 4. Verify

```bash
# All five containers up (the collector has no healthcheck — see below)
docker compose --project-directory . \
    -f ../traceway/docker-compose.yml \
    -f ../traceway/docker-compose.sqlite.yml \
    -f docker-compose.titan.yml \
    --env-file .env ps

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

`titan-otel-collector` shows a bare `Up` with no `(healthy)` marker,
and that is correct: it has no healthcheck. The collector image is
distroless (no shell, no `wget`/`curl`) and the config does not enable
the `health_check` extension, so a container-local probe could never
have worked — it just reported `unhealthy` forever while the collector
was fine. Check it with:

```bash
docker logs --since 2m titan-otel-collector | grep -E 'Everything is ready|invalid configuration'
```

`Everything is ready. Begin running and processing data.` is the line
that means the config parsed and the pipeline is wired.

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
| Faster / slower metrics poll | `CW_POLL_INTERVAL_S=15` in `.env` (and `TraceWayWayOtlCollector.logs.poll_interval` in `./otel-collector/config.yaml` for logs) |
| Different log-group prefix | `./otel-collector/config.yaml` → `awscloudwatch.logs.groups.autodiscover.prefix` (default `/aws/ec2/titanprediction/`) |
| Different log streams | `./otel-collector/config.yaml` → `awscloudwatch.logs.groups.autodiscover.streams.prefixes` (defaults to `/api /api-errors /api-admin /db /services`) |
| Route logs and metrics to different projects | Set `TRACEWAY_OTLP_ENDPOINT_LOGS` / `TRACEWAY_OTLP_ENDPOINT_METRICS` in `.env` to two different traceway URLs and create two projects |
| Run traceway outside docker (macOS/Windows) | Override both `*_OTLP_ENDPOINT` env vars to `http://host.docker.internal:80/...` |
| Drop the collector (logs are off the table) | Comment out the `otel-collector:` block in `./docker-compose.titan.yml` |
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

**Bot-scan / noise visibility:** every synthetic span carries the
attribute `titan.path_category: real|noise`. Noise is anything not
under `/api/` (Joomla-exploit scanners, `.git/HEAD` probes, etc.); real
is matched API routes. The Endpoints page's attribute filter lets you
split noise from real traffic, set alerts on noise volume, and assess
the EC2 performance cost of serving 404s (count × p50 = CPU-seconds
spent on bots). Adjust the prefix lists in
`./cw-logs-metrics-exporter/cw_logs_metrics_exporter.py` if the
boundary needs to move.

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

## Searching titan logs in the dashboard

The bridge stamps every log record with structured attributes so the
Traceway **Logs** page can filter precisely. Time-range picker plus
filter chips together cover most queries; no CLI required.

| To find | Recipe |
|---|---|
| All titan-api requests in the last 1h | Time range `1h`, **Search** → `service` → `titan-api` |
| titan-services activity | Time range, **Search** → `service` → `titan-services` (or **Add filter** → `resource.titan.subsystem = services`) |
| Errors only | **Severity** chip → `ERROR+` |
| A specific user's actions | **Add filter** → `resource.enduser.id = alice` |
| 5xx responses | **Add filter** → `log.http.response.status_code` operator `~=` value `5` |
| A specific endpoint pattern (Flask route) | **Add filter** → `log.titan.route = /api/v1/matches/<id>` |
| A specific concrete path | **Add filter** → `log.url.path = /api/v1/matches` |
| Slow requests (≥1 s) | **Add filter** → `log.titan.duration_ms` operator `>=` value `1000` |
| Inspect a request context payload | Click row → expanded view → Attributes grid → **`titan.raw`** tile. It holds the complete original JSON line for the event, `details` dict included. Search it with **Add filter** → `log.titan.raw` operator `~=` value `user_type` |
| Services / DB operation input | Same `titan.raw` tile — operation and db-audit events put their summarized `parameters` in there |
| Combine service + status | **Add filter** → `log.http.response.status_code = 500` then **Search** → `service` → `titan-api` |

For each row, the **Trace ID** and **Span ID** tiles at the top of
the expanded view link to the matching synthetic span in the **Spans**
tab.

### The two attributes worth knowing

- **`log.titan.raw`** — the entire original JSON log line, verbatim, as
  a string. Everything the backend wrote is in it, including the
  nested dicts (`details`, `parameters`) that the flat `titan.*`
  lifts can't carry. The `titan.*` scalars exist so you can filter on
  them; `titan.raw` exists so nothing is lost.
- **`resource.enduser.id`** — the username, promoted to a resource
  attribute so you can filter with `resource.enduser.id = alice`
  instead of the more error-prone `log.titan.username`.

> **Heads-up on request/response bodies.** The activity loggers
> (`api/activity_logger.py`, `services/activity_logger.py`,
> `db/activity_logger.py`) intentionally **do not write** raw request
> bodies, response bodies, query params, or headers — see
> `_summarize_value` in `services/activity_logger.py:175` and the
> docstring "Summarize call arguments without dumping sensitive
> payloads" at `db/activity_logger.py:137`, both of which summarize
> rather than dump. So the closest thing to a request payload today is
> the `details` dict an action carries, visible inside `titan.raw`.
> Capturing real bodies would be a change to the Titan backend, not to
> this bridge.

### Full attribute reference

Flat scalar lifts (all reachable as `log.titan.<name>`): `action`,
`status`, `username`, `ip`, `country`, `method`, `endpoint`, `route`,
`user_agent`, `status_code`, `duration_ms`, `service`, `operation`,
`phase`, `error`, `traceback`.

OTel semantic-convention mirrors: `log.http.request.method`,
`log.url.path`, `log.http.response.status_code`,
`log.client.address`, `log.user_agent.original`,
`log.exception.message`, `log.exception.stacktrace`.

Resource attributes (survive `keep_keys`): `service.name`,
`service.instance.id`, `deployment.environment.name`, `cloud.provider`,
`cloud.region`, `host.name`, `titan.subsystem`, `enduser.id`.

If a search returns a 422 ("Body search over more than 24 hours
requires a filter"), narrow the time range or add at least one of:
service name, severity floor, trace id, attribute filter.

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
| `docker compose` fails with a missing `../traceway/docker-compose.yml` | This repo needs a traceway checkout one level up — clone it beside `traceway-bridge`, or point the `-f` paths at your checkout |
| `make up` builds from the wrong directory / `cw-metrics-exporter` context not found | You dropped `--project-directory .`; without it compose resolves this repo's relative paths against the first `-f` file. See README §3 |
| OTel collector OOMKilled | `awscloudwatch` is streaming too many events; raise `logs.max_events_per_request` cap or narrow `groups.autodiscover.prefix` |
| `titan-otel-collector` in `docker ps` has no `(healthy)` / `(unhealthy)` marker | By design — distroless image plus no `health_check` extension, so there is no probe to run. Grep the logs for `Everything is ready` (README §4) |

## File-by-file reference

- **`./otel-collector/config.yaml`** — pipeline config. Top-of-file
  comment block explains the design choices. Edit in place; `docker
  compose restart titan-otel-collector` picks up changes.
- **`./cw-metrics-exporter/cw_metrics_exporter.py`** — the
  boto3/OTel SDK sidecar. Single file, ~150 lines. Top-of-file docstring
  is the API contract.
- **`./docker-compose.titan.yml`** — adds the two sidecars; merges
  with `docker-compose.yml` + `docker-compose.sqlite.yml`. No service
  overrides on the upstream traceway image.
- **`./.env.example`** — annotated env-var template. Copy to `.env`.