# reader-server:local — one image for the api, worker and mcp roles (OPS-DEPLOY FR-DEPLOY-2). Build context: the
# repository root. The roles' entry point `reader-server` comes with the server modules that claim it.
FROM python:3.14-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
RUN useradd --uid 10001 --create-home --home-dir /home/reader reader
# Only what the build needs (FR-DEPLOY-2 allow-list): copying server/ would bake .venv, caches, tests and any .env.
COPY server/pyproject.toml /opt/reader/pyproject.toml
COPY server/src /opt/reader/src
RUN pip install --no-cache-dir /opt/reader
COPY --chmod=0755 deploy/images/wait-for-secret.sh /usr/local/bin/wait-for-secret
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/wait-for-secret", "db-password", "--"]
