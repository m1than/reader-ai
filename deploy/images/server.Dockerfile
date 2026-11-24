# reader-server:local — one image for the api, worker and mcp roles (OPS-DEPLOY FR-DEPLOY-2). Build context: the
# repository root. The roles' entry point `reader-server` comes with the server modules that claim it.
FROM python:3.14-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
RUN useradd --uid 10001 --create-home --home-dir /home/reader reader
# Only what the build needs (FR-DEPLOY-2 allow-list): copying server/ would bake .venv, caches, tests and any .env.
COPY server/pyproject.toml /opt/reader/pyproject.toml
COPY server/src /opt/reader/src
# The `agent` extra: the Agent SDK and its bundled CLI for the worker's provider adapter (SRV-AGENT).
RUN pip install --no-cache-dir "/opt/reader[agent]"
COPY --chmod=0755 deploy/images/wait-for-secret.sh /usr/local/bin/wait-for-secret
# The blobs mount point, owned by the service uid: Docker copies its ownership into an empty named volume on first
# mount, so uid 10001 with cap_drop ALL can write there (FR-DEPLOY-5).
RUN mkdir -p /var/lib/reader/blobs && chown 10001:10001 /var/lib/reader/blobs
# The converter source hash (FR-DEPLOY-14, SRV-INGEST FR-INGEST-23): the api compares the host watcher's code with it.
RUN python -I -c "from pathlib import Path; from reader.ingest.host.closure import source_sha256; out = Path('/usr/local/share/reader'); out.mkdir(parents=True, exist_ok=True); (out / 'converter_source_sha256').write_text(source_sha256(Path('/opt/reader/src')))" && chmod 0444 /usr/local/share/reader/converter_source_sha256
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/wait-for-secret", "db-password", "--"]
