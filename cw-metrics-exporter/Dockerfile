# Python 3.12 — slim enough to keep the image small, modern enough for the
# OTel SDK's typing.
FROM python:3.12-slim

# Same metric set as the legacy titan-monitor once|watch|graph:
#   - boto3 to call CloudWatch GetMetricData
#   - opentelemetry SDK + OTLP HTTP exporter to ship the values
RUN pip install --no-cache-dir \
    "boto3>=1.35" \
    "opentelemetry-api>=1.27" \
    "opentelemetry-sdk>=1.27" \
    "opentelemetry-exporter-otlp-proto-http>=1.27"

COPY cw_metrics_exporter.py /app/cw_metrics_exporter.py

# Non-root so the container matches the rest of the stack's hardening.
RUN useradd --create-home --uid 10001 titan && chown -R titan:titan /app
USER titan
WORKDIR /app

# Healthcheck that flips to "starting" until the first poll succeeds. The
# exporter writes one CW read per 60 s, so use a generous start period.
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import os,sys; sys.exit(0 if os.environ.get('AWS_REGION') else 1)"

ENTRYPOINT ["python", "/app/cw_metrics_exporter.py"]