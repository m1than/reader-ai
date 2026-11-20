# Spike probe image (never shipped): our parent spans (OTel SDK) + the Agent SDK whose bundled CLI exports
# its beta traces; the SDK injects TRACEPARENT from the active span into the CLI (claude-agent-sdk 0.2.186,
# _internal/transport/subprocess_cli.py lines 782-804).
# The token arrives on stdin (first line), is exported only inside the container and never appears in
# `docker inspect` (pattern from spikes/compile-cost). replay.py (entrypoint override) needs no token.
FROM python:3.14-slim-trixie
RUN pip install --no-cache-dir claude-agent-sdk==0.2.186 opentelemetry-sdk==1.45.2 \
      opentelemetry-exporter-otlp-proto-http==1.45.2 \
 && useradd -m -u 1000 worker
COPY probe.py replay.py /app/
USER worker
WORKDIR /home/worker
ENTRYPOINT ["sh", "-c", "IFS= read -r CLAUDE_CODE_OAUTH_TOKEN && export CLAUDE_CODE_OAUTH_TOKEN && exec timeout -s KILL 120 python /app/probe.py", "--"]
