# Spike probe image (never shipped): our parent spans (OTel SDK) + the Agent SDK whose bundled CLI exports
# its beta traces; the SDK injects TRACEPARENT from the active span into the CLI (subprocess_cli.py).
FROM python:3.14-slim-trixie
RUN pip install --no-cache-dir claude-agent-sdk==0.2.186 opentelemetry-sdk==1.45.2 \
      opentelemetry-exporter-otlp-proto-http==1.45.2 \
 && useradd -m -u 1000 worker
COPY probe.py /probe.py
USER worker
WORKDIR /home/worker
ENTRYPOINT ["timeout", "-s", "KILL", "120", "python", "/probe.py"]
