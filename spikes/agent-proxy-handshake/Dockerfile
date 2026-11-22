# Spike image (never shipped): the Agent SDK (bundled CLI) plus a local stub upstream and a logging forward proxy.
# Runs with --network none: only 127.0.0.1 exists, so any other host the CLI tries fails and is logged by the proxy.
FROM python:3.14-slim-trixie
RUN pip install --no-cache-dir claude-agent-sdk==0.2.187 && useradd -m -u 1000 worker
COPY stub.py probe.py entry.sh /spike/
USER worker
WORKDIR /home/worker
ENTRYPOINT ["sh", "/spike/entry.sh"]
