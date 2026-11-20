# Spike image (never shipped): the Python Agent SDK (bundled Claude Code CLI) in a linux/arm64 container,
# authenticated by CLAUDE_CODE_OAUTH_TOKEN (ADR-0005). Non-root, as the worker would run.
FROM python:3.13-slim-bookworm
RUN pip install --no-cache-dir claude-agent-sdk==0.2.186 && useradd -m -u 1000 worker
COPY probe.py /probe.py
USER worker
WORKDIR /home/worker
ENTRYPOINT ["python", "/probe.py"]
