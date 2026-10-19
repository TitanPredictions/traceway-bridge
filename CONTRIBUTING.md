# Contributing to traceway-bridge

The bridge is small by design: ~600 lines of Python across two sidecars,
one OTel collector config, and a docker-compose overlay. Most changes
are config-only; new functionality usually means adding a new metric
or a new log field rather than restructuring existing code.

## Where things live

- **`cw-metrics-exporter/`** — polls CloudWatch Metrics (CPU, mem, disk
  from CWAgent) and ships them as OTLP. ~110 lines.
- **`cw-logs-metrics-exporter/`** — polls CloudWatch Logs, parses the
  activity-log JSON, emits OTLP metrics + synthetic spans. ~370 lines.
- **`otel-collector/config.yaml`** — `awscloudwatch` receiver → parse +
  transform → OTLP HTTP exporter. The `transform/logs` block does
  semantic-attribute mapping; the `transform/logs` block under
  `context: log` parses the JSON body of each log line.
- **`docker-compose.titan.yml`** — the three sidecar services
  (`cw-metrics-exporter`, `cw-logs-metrics-exporter`,
  `otel-collector`) plus a `Makefile` for the dev workflow.

## Dev environment

You need:
- Docker + docker compose (matches the upstream traceway stack)
- A traceway project + ingest token (one per dev, or one shared project)
- AWS IAM access to `cloudwatch:GetMetricData` and
  `logs:FilterLogEvents` + `logs:DescribeLogGroups` on the TitanPrediction
  account

The bridge is run by `make up` (see the Makefile). It assumes the
traceway repo is cloned at the parent directory:

```
~/code/traceway/        ← cloned from tracewayapp/traceway
~/code/traceway-bridge/ ← this repo
```

If your layout differs, edit the `../traceway/` paths in the Makefile
and `docker-compose.titan.yml`.

## Testing your change

Before opening a PR:

1. `make lint` — Python syntax check on the two sidecars.
2. `make validate` — runs the OTel collector config through
   `validate` to catch YAML / OTTL typos.
3. `make up` and watch the logs. You should see:
   - `cw-metrics-exporter`: `poll ok: events=N` every 60s
   - `cw-logs-metrics-exporter`: `poll ok: events=N` every 60s
   - `otel-collector`: quiet except for periodic AWS SDK log lines
4. Open <http://localhost/>, pick your project, verify the new
   metric/span shows up under **Metrics** or **Logs**.

## Adding a new metric

The pattern:

1. **Pick the right sidecar.** `cw-metrics-exporter` polls CloudWatch
   Metrics API — use it for anything that's already a CW metric
   (CloudWatch Agent metrics, AWS/EC2 metrics, custom CW metrics).
   `cw-logs-metrics-exporter` parses activity logs — use it for
   anything that needs to derive from per-request data the agent
   already records.
2. **Declare the metric** in the sidecar's `_setup` block. Use
   `meter.create_counter` / `create_histogram` / `create_gauge` from
   the OTel SDK. The OTel HTTP exporter pushes the data; you only
   need to record it.
3. **Record the metric** in the sidecar's per-event / per-poll loop.
   Keep attribute cardinality bounded — every unique combination of
   attribute values creates a new time series in traceway.
4. **Verify in the UI.** The metric appears under **Metrics** in the
   project. Build a dashboard widget to confirm aggregation works.

## Adding a new log field to parse

When the backend/ repo grows a new key in the activity-log JSON:

1. **Add a `set(log.attributes[...], log.attributes["titan.parsed"][...])`
   statement** in the `otel-collector/config.yaml` `transform/logs`
   block (under `context: log`). Use the same key as the backend
   side, e.g. `titan.user_agent`.
2. **Mirror to OTel semantic convention** if there's a standard one
   (`user_agent.original`, `client.address`, etc.) — a second `set`
   line maps Titan's name to the standard. This is what makes the
   field show up under the right filter in traceway.
3. **Update the cw-logs-metrics-exporter** if the new field is needed
   for metrics or span attributes (not every parsed field needs to
   make it into metrics).
4. **Run `make validate`** to catch OTTL typos.

## Adding a new endpoint detection rule

The `path_category` function in `cw-logs-metrics-exporter.py` decides
whether a request is "real" (`/api/...`) or "noise" (bot scan). The
prefix lists at the top of that file are easy to extend:

- **New bot signature?** Add a path prefix to `BOT_SCAN_PREFIXES`.
- **New API path that should be "real"?** Add to `REAL_API_PREFIXES`.
- **Need a third category?** Add a constant, branch in `path_category`,
  and add a new attribute to all metric / span emits.

## Gotchas

- **`make up` doesn't pick up bind-mounted config changes.** When you
  edit `otel-collector/config.yaml`, the OTel collector caches its
  parsed config from the first start. Use `make push` (which
  `--force-recreate`s the relevant services) or `docker compose ... up
  -d --force-recreate otel-collector` to apply changes.
- **`docker cp` for SQLite can read stale data.** traceway uses WAL
  mode. To see the latest rows, copy all three files: `traceway.db`,
  `traceway.db-wal`, `traceway.db-shm`. The base file alone is the
  last checkpoint, which can be minutes old.
- **OTLPSpanExporter vs. OTLP HTTP exporter semantics differ.** When
  you pass `endpoint=` to the Python OTel SDK constructor, the URL
  is used verbatim — there's no auto-append of `/v1/<signal>`. The
  metrics exporter relies on the constructor; if you change the
  endpoint, include the full `/v1/metrics` path. The collector is
  different — its `otlphttp` exporter appends `/v1/<signal>` unless
  given a full URL.
- **`IsMatch`, `Index`, `ReplacePattern` are not all in OTTL 0.119.**
  See TODO.md §"OTTL gotchas" for the working set.

## Reviewing a PR

Before approving, sanity-check:

- [ ] `make lint` passes
- [ ] `make validate` passes
- [ ] `make up` actually starts; new metric / span shows up in traceway
- [ ] Any new attribute has bounded cardinality (don't key by raw `id`)
- [ ] Any new prefix in `path_category` is correctly classified
- [ ] README is updated if a new env var, new sidecar, or new file
      layout was introduced
- [ ] TODO.md is updated if the change closes an open item

## Release

This repo doesn't tag releases yet. When the team is ready, the
release flow will be:

1. Bump version in `cw-metrics-exporter/cw_metrics_exporter.py` and
   `cw-logs-metrics-exporter/cw_logs_metrics_exporter.py` (currently
   hardcoded as `0.0.0`).
2. Tag with semver (`v0.1.0`).
3. The dev team's CI pulls the tagged sidecar image by digest.
