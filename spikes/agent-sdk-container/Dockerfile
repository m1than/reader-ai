# Spike image (never shipped): the Python Agent SDK (bundled Claude Code CLI) in a linux/arm64 container,
# authenticated by CLAUDE_CODE_OAUTH_TOKEN (ADR-0005). Non-root, as the worker would run.
# Baseline (owner asked for the latest LTS/stable; proposed ADR-0020 on branch 0/toolchain-baseline): Debian 13 trixie, Python 3.14. Round 2 results were measured on 3.13-slim-bookworm.
FROM python:3.14-slim-trixie
RUN pip install --no-cache-dir claude-agent-sdk==0.2.186 && useradd -m -u 1000 worker
COPY probe.py /probe.py
USER worker
WORKDIR /home/worker
ENTRYPOINT ["python", "/probe.py"]
