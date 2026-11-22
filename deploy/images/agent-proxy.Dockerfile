# reader-agent-proxy:local — agent-proxy, the credential-injecting reverse proxy (SRV-AGENT FR-AGENT-1, ADR-0025).
# Owned by SRV-AGENT (OPS-DEPLOY note). Build context: the repository root. The server package without the `agent`
# extra: no Agent SDK or CLI in this image. `sh`, `cat` and `mv` (base image) serve OPS-CLI's token delivery into
# /run/secrets.
FROM python:3.14-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
RUN useradd --uid 10002 --create-home --home-dir /home/proxy agentproxy
# Only what the build needs (allow-list, as for server.Dockerfile).
COPY server/pyproject.toml /opt/reader/pyproject.toml
COPY server/src /opt/reader/src
RUN pip install --no-cache-dir /opt/reader \
 && printf '%s\n' '#!/usr/local/bin/python3 -I' 'import socket, sys' \
    'try:' '    socket.create_connection(("127.0.0.1", 8080), timeout=2).close()' \
    'except OSError:' '    sys.exit(1)' > /usr/local/bin/reader-agent-proxy-health \
 && chmod 0755 /usr/local/bin/reader-agent-proxy-health
USER 10002:10002
ENTRYPOINT ["python", "-I", "-m", "reader.agent.proxy"]
