# Spike image (never shipped): one lesson compile through the Agent SDK (ADR-0005, ADR-0009).
# The token arrives on stdin and is exported only inside the container's process tree, so it never appears
# in `docker inspect` (Config.Env). The whole run is capped by `timeout -s KILL` (seconds in CAP_S).
FROM python:3.14-slim-trixie
RUN pip install --no-cache-dir claude-agent-sdk==0.2.186 && useradd -m -u 1000 worker
COPY pipeline.py prompts.py /app/
USER worker
WORKDIR /home/worker
ENTRYPOINT ["sh", "-c", "IFS= read -r CLAUDE_CODE_OAUTH_TOKEN && export CLAUDE_CODE_OAUTH_TOKEN && exec timeout -s KILL \"${CAP_S:-3000}\" python /app/pipeline.py \"$@\"", "--"]
