.PHONY: help up down logs ps build push lint validate clean token

# traceway-bridge top-level Makefile.
#
# Run `make help` for the menu. Each target assumes you have a populated
# `.env` (copy `.env.example` and fill in your AWS keys + traceway token).
# The compose stack pulls `traceway` (Postgres + ClickHouse + traceway
# app) from the traceway repo one level up — this is documented in
# README.md.
#
# Every compose call passes `--project-directory .`. Without it compose
# takes the project directory from the FIRST `-f` file, i.e.
# `../traceway/`, and then resolves this repo's relative paths (the two
# sidecar build contexts, the collector config bind mount) against the
# traceway checkout — where they do not exist. The flag pins them here.

SHELL := /bin/bash

help:                   ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

up:                    ## Build images (if needed) and start the full stack in the background.
	docker compose --project-directory . \
	               -f ../traceway/docker-compose.yml \
	               -f ../traceway/docker-compose.sqlite.yml \
	               -f docker-compose.titan.yml \
	               --env-file .env \
	               up -d --build

down:                  ## Stop the stack (volumes kept).
	docker compose --project-directory . \
	               -f ../traceway/docker-compose.yml \
	               -f ../traceway/docker-compose.sqlite.yml \
	               -f docker-compose.titan.yml \
	               --env-file .env \
	               down

logs:                  ## Tail logs from all three sidecars + the OTel collector.
	docker logs -f titan-cw-metrics-exporter &
	docker logs -f titan-cw-logs-metrics-exporter &
	docker logs -f titan-otel-collector &
	wait

ps:                    ## List the running containers.
	docker compose --project-directory . \
	               -f ../traceway/docker-compose.yml \
	               -f ../traceway/docker-compose.sqlite.yml \
	               -f docker-compose.titan.yml \
	               --env-file .env \
	               ps

build:                 ## Build the two Python sidecar images.
	docker compose --project-directory . \
	               -f ../traceway/docker-compose.yml \
	               -f ../traceway/docker-compose.sqlite.yml \
	               -f docker-compose.titan.yml \
	               --env-file .env \
	               build cw-metrics-exporter cw-logs-metrics-exporter

push:                  ## Restart the three bridge services (after config edits).
	docker compose --project-directory . \
	               -f ../traceway/docker-compose.yml \
	               -f ../traceway/docker-compose.sqlite.yml \
	               -f docker-compose.titan.yml \
	               --env-file .env \
	               up -d --force-recreate cw-metrics-exporter cw-logs-metrics-exporter otel-collector

validate:              ## Run the OTel collector config through `validate` (catches typos before `up`).
	docker run --rm \
	  -v $(CURDIR)/otel-collector/config.yaml:/etc/otelcol/config.yaml:ro \
	  -e AWS_REGION=$${AWS_REGION:-eu-west-3} \
	  -e OTLP_TOKEN=placeholder \
	  -e TRACEWAY_OTLP_ENDPOINT_BASE=http://traceway:80/api/otel \
	  otel/opentelemetry-collector-contrib:0.119.0 \
	  validate --config=/etc/otelcol/config.yaml

lint:                  ## Python syntax check on the two sidecar modules.
	@for m in cw-metrics-exporter/cw_metrics_exporter.py \
	         cw-logs-metrics-exporter/cw_logs_metrics_exporter.py; do \
	  echo "checking $$m"; \
	  python3 -m py_compile "$$m" || exit 1; \
	done

clean:                 ## Stop stack, remove bridge images and the traceway-data volume.
	docker compose --project-directory . \
	               -f ../traceway/docker-compose.yml \
	               -f ../traceway/docker-compose.sqlite.yml \
	               -f docker-compose.titan.yml \
	               --env-file .env \
	               down -v --rmi local
	docker volume rm traceway_traceway-data 2>/dev/null || true

token:                 ## Print the traceway ingest token from .env (handy when wiring new tools).
	@grep '^TRACEWAY_BACKEND_TOKEN=' .env | cut -d= -f2

# Convenience target: open the traceway dashboard in the default browser.
dashboard:
	@xdg-open http://localhost/ 2>/dev/null || open http://localhost/ 2>/dev/null || echo "Open http://localhost/ in your browser"
