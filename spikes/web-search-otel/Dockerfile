# Spike image (never shipped): the Agent SDK (bundled CLI 2.1.330, the server's pin) plus the OTel SDK, a stub
# upstream that answers the CLI's WebSearch flow, a logging forward proxy and a capturing OTLP/HTTP receiver.
# Runs with --network none: only 127.0.0.1 exists. Same base as spikes/cli-otel-content.
FROM python:3.14-slim-trixie
RUN pip install --no-cache-dir claude-agent-sdk==0.2.187 opentelemetry-sdk==1.45.2 \
      opentelemetry-exporter-otlp-proto-http==1.45.2 && useradd -m -u 1000 worker
COPY stub.py probe.py entry.sh /spike/
USER worker
WORKDIR /home/worker
ENTRYPOINT ["sh", "/spike/entry.sh"]
