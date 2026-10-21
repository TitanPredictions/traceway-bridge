# Session — 2026-10-21: making Titan logs searchable in Traceway

Working notes for the log-search work. The **durable** documentation lives in
[`README.md`](./README.md) (search recipes, attribute reference). This file
records what we set out to do, what actually landed, and what is still open.

> These notes were written while the bridge still lived at `titan/` inside a
> traceway checkout. Paths have been rewritten for this standalone repo; the
> compose invocation drops the `-f titan/…` prefix. Behaviour described here is
> unchanged.

Changed in this session (nothing else was touched):

```
 README.md                    | 66 ++++++
 otel-collector/config.yaml   | 155 ++++++++++++++-----
 docker-compose.titan.yml     |  20 ++-      (healthcheck, fixed after)
 TODO.md                      |  26 ++--     (item 4 rewritten — now done)
```

## Goal

Three complaints about the Logs page, in priority order:

1. `titan-services` logs were effectively invisible.
2. Request/response context could not be inspected.
3. `titan.username` could not be searched on.

Constraint agreed for the whole session: **changes stay inside the bridge**. No
edits to traceway core, the frontend, the CLI, or the Titan backend — even
when the backend turned out to be the actual blocker for item 2.

## What we changed

### `otel-collector/config.yaml`

The transform ran, but it flattened too little. Five additions, in order of
value:

| Change | Why |
|---|---|
| `titan.raw` — the whole JSON line captured before the body is rewritten | Nothing is lost; searchable as a string |
| `resource.titan.subsystem` — derived from the CW log-group name | Makes plain-text service logs findable |
| `resource.enduser.id` — promoted out of `log.titan.username` | Correct OTel scope; a resource attr is what filter UIs key on |
| Scalar lifts for `route`, `service`, `operation`, `phase`, `status_code`, `duration_ms` | These were never lifted, so they were unfilterable |
| `http.response.status_code` mirror fixed | It read `titan.status_code`, which nothing lifted — **always empty** |

Body rules are ordered **least-specific first** (so `websocket` is tried before
the action/status/endpoint rules) and every presence check uses `IsString(...)`.

### `README.md`

Search recipe table, `titan.raw` guidance, full attribute reference, and the
body-capture caveat.

## Deployment gotcha — read this before any config edit

`docker compose up -d --build` **does not restart the collector** when only the
bind-mounted config changed:

```
✔ Container titan-otel-collector  Running  0.0s      ← never recreated
```

The file on disk is updated, but the collector parses its config **only at
startup**. Config edits appear to succeed and then do nothing. We hit this
exactly: the collector had been running the config from **18 Oct** while the
host file had been edited on 21 Oct.

Correct sequence:

```bash
# first bring-up
docker compose -f docker-compose.yml -f docker-compose.sqlite.yml \
  -f docker-compose.titan.yml --env-file .env up -d --build

# every subsequent CONFIG change — no --build, it only rebuilds images we
# did not touch (cost ~143 s for nothing)
docker compose -f docker-compose.yml -f docker-compose.sqlite.yml \
  -f docker-compose.titan.yml --env-file .env restart otel-collector
```

Confirm it actually took:

```bash
docker inspect titan-otel-collector --format 'Started: {{.State.StartedAt}}'   # must be ~now
docker logs --since 2m titan-otel-collector | grep -E 'Everything is ready|error'
```

`Everything is ready` and no `invalid configuration` / `undefined function`.

The second command is the health signal, and it is the *only* one — see open
item 1 below.

## Verifying changes without the UI

The dashboard is the slow feedback loop (minutes of poll + batching). Querying
the telemetry DB directly is much faster. Note traceway runs on **SQLite**
(`docker-compose.sqlite.yml`), so there is no ClickHouse `log_records` table —
`clickhouse-client` returns `UNKNOWN_TABLE`, which is not a bug.

```bash
for f in traceway_telemetry.db traceway_telemetry.db-wal traceway_telemetry.db-shm; do
  docker cp "traceway-traceway-1:/data/$f" "/tmp/x.db${f#traceway_telemetry.db}"
done
python3 - <<'PY'
import sqlite3
c = sqlite3.connect('/tmp/x.db')
print(c.execute("SELECT service_name,timestamp FROM log_records ORDER BY timestamp DESC LIMIT 5").fetchall())
print(c.execute("SELECT COUNT(*) FROM log_records WHERE log_attributes LIKE '%\"titan.raw\"%'").fetchone())
PY
```

**Copy all three files.** The `.db` alone is a stale checkpoint; recent writes
sit in the `-wal` and you will conclude there is no data when there is.

Clocks: `docker logs` / `StartedAt` are **UTC**; file mtimes are **local**
(`+01:00`). Comparing them directly made a 4-minute gap look like an hour and
sent us chasing a phantom outage.

## Verification result — production, not synthetic

After restarting at `2026-10-21T16:42:08Z`:

| Attribute | Coverage |
|---|---|
| `resource.titan.subsystem` | **2799/2799** |
| `log.titan.raw` | 2262/2799 (JSON events only) |
| `log.titan.status_code` | 51/2799 |
| `log.http.response.status_code` | 51/2799 (was 0) |
| `log.url.path` | 51/2799 |
| `resource.enduser.id` | 4/2799 |

`syslog`-free plain-text service logs (e.g. `League 292: actualizando ronda…`)
now carry `titan.subsystem = services` with the body untouched — that was goal
1, and the ~2,748 such records are the bulk of the table.

Real username record:

```
SEV   WARN
BODY  login failure (j.doe@example.com)
RES   enduser.id = j.doe@example.com
      service.name = titan-api        titan.subsystem = api
LOG   titan.action = login            titan.status = failure
      titan.route = /api/v1/auth/login
      titan.duration_ms = 251         http.response.status_code = 200
      titan.raw = {"timestamp":"…","action":"login",…}
```

### Caveat worth knowing before you build filters

**A `titan.status = failure` event is often HTTP 200.** The login failure above
is `status: failure` but `http.response.status_code = 200`. So
`http.response.status_code = 500` will **not** surface business-logic
failures. Filter on `resource.titan.subsystem = api` + `severity ERROR+`, or
search the `titan.status` attribute — do not assume 5xx == failure.

## Not fixed: request/response bodies (goal 2, partially)

`titan.raw` gives you the complete JSON the backend wrote, including the
summarized `details` / `parameters` dicts. It does **not** give HTTP bodies,
because Titan never logs them. The loggers summarize deliberately:

- `services/activity_logger.py:175` — `_summarize_value`
- `db/activity_logger.py:137` — *"Summarize call arguments without dumping
  sensitive payloads"*, plus its own `_summarize_value` at `:111`

This is a Titan **backend** decision, not a bridge one: which routes, what
redaction, size caps. Left untouched this session. Flag it as the open item
with the largest payoff.

## Open items

1. ~~**Broken collector healthcheck**~~ — **fixed after this session.**
   `docker-compose.titan.yml` ran `wget`, which is not in the distroless
   collector image, so the container reported `unhealthy` permanently since
   18 Oct while working correctly. Investigating it turned up a *second* bug:
   nothing listens on `:13133` either, because `config.yaml` never enabled the
   `health_check` extension — so even a shell-bearing image would have been
   connection-refused. Healthcheck removed (`healthcheck: {disable: true}`)
   rather than papered over. Nothing in either compose file gates on
   `service_healthy` for the collector and `restart: unless-stopped` covers
   crash loops, so the removal costs no behaviour. `docker ps` now shows a bare
   `Up` for it; verification is the `Everything is ready` log line, now
   documented in README §4 and the troubleshooting table.
2. **Nothing is committed yet** — see the repo history; the log-search and
   healthcheck changes were committed on 21 Oct and were still being tested.
3. ~~**Move the bridge out of the traceway clone**~~ — **done**, this is now
   `TitanPredictions/traceway-bridge`, with `CONTRIBUTING.md`, a `Makefile`,
   and CI that validates the collector config. TODO #4 is retained only as a
   record of what the move entailed.
4. **Collector is chatty about config errors** — OTLP `error_mode: ignore`
   hides *runtime* errors only. Every malformed OTTL statement still refuses
   to start. Run `validate --config=…` on a copy before restarting — CI now
   does this on every PR, but it is not a substitute for a local check before
   you `restart` a collector that is currently feeding production.

### Enabling the health endpoint later

If you want a real health signal, it takes both halves, not one:
`extensions: {health_check: {endpoint: 0.0.0.0:13133}}` in `config.yaml`,
**and** a probe run from another container on the compose network. There is no
container-local option while the image stays distroless — no official tag of
`otel/opentelemetry-collector-contrib` ships a shell.