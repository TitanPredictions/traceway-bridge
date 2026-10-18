# Python 3.12 slim. Same base image as the metrics-side exporter so the
# AWS SDK and OTLP package versions stay in lockstep.
FROM python:3.12-slim

RUN pip install --no-cache-dir \
    "boto3>=1.35" \
    "opentelemetry-api>=1.27" \
    "opentelemetry-sdk>=1.27" \
    "opentelemetry-exporter-otlp-proto-http>=1.27"

COPY cw_logs_metrics_exporter.py /app/cw_logs_metrics_exporter.py

RUN useradd --create-home --uid 10001 titan && chown -R titan:titan /app
USER titan
WORKDIR /app

HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import os,sys; sys.exit(0 if os.environ.get('AWS_REGION') else 1)"

ENTRYPOINT ["python", "/app/cw_logs_metrics_exporter.py"]