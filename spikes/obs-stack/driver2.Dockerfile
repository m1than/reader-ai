# Spike driver for phase 2 (never shipped): the full MLflow 3.18.0 client (GenAI datasets and evaluate need it, not
# only mlflow-skinny), the OTel SDK and PyYAML. Zero credit: no model provider package, no key.
FROM python:3.14-slim-trixie
RUN pip install --no-cache-dir "mlflow==3.18.0" opentelemetry-sdk==1.45.2 opentelemetry-exporter-otlp-proto-http==1.45.2 \
      pyyaml
ENTRYPOINT ["python"]
