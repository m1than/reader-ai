# CLI canary harness image (SRV-OBS INV-OBS-2; test only, never shipped): the Agent SDK at the server's pin (its
# bundled CLI) plus the OTel SDK. The test passes CLAUDE_AGENT_SDK as a build argument from server/uv.lock. Runs with
# --network none: only 127.0.0.1 exists.
FROM python:3.14-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
ARG CLAUDE_AGENT_SDK
RUN pip install --no-cache-dir "claude-agent-sdk==${CLAUDE_AGENT_SDK}" opentelemetry-sdk==1.45.2 \
      opentelemetry-exporter-otlp-proto-http==1.45.2 && useradd -m -u 1000 worker
COPY stub.py probe.py entry.sh /canary/
USER worker
WORKDIR /home/worker
ENTRYPOINT ["sh", "/canary/entry.sh"]
