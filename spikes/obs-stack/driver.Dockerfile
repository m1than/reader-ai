# Spike driver image (never shipped): OTel SDK for the replay, the MLflow client for queries and retention.
FROM python:3.14-slim-trixie
RUN pip install --no-cache-dir opentelemetry-sdk==1.45.2 opentelemetry-exporter-otlp-proto-http==1.45.2 \
      mlflow-skinny==3.18.0
COPY driver.py /spike/driver.py
ENTRYPOINT ["python", "/spike/driver.py"]
