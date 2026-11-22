# reader-embed:local — SRV-SEARCH's embed service (ADR-0028; OPS-DEPLOY FR-DEPLOY-2). Build context: the repository
# root. Dependencies only from the hash lock (deploy/images/embed.lock.sh generates it), binary wheels only; the model
# files of models.lock.json fetched at build at the pinned revision and verified by sha256 (a mismatch fails the
# build); only the four embed files of reader.search go into site-packages, so the image has no config, db or api
# code. At run time: no network (its own internal network), read-only root, uid 10003, HF offline.
FROM python:3.14-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
COPY deploy/images/embed.requirements.lock /tmp/embed.requirements.lock
RUN pip install --no-cache-dir --require-hashes --only-binary :all: --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple -r /tmp/embed.requirements.lock \
 && rm /tmp/embed.requirements.lock
# The model layer depends only on the lock and the fetcher, so a service edit does not download 2.27 GB again.
COPY server/src/reader/search/__init__.py server/src/reader/search/fetch_model.py server/src/reader/search/models.lock.json /usr/local/lib/python3.14/site-packages/reader/search/
RUN python -I -m reader.search.fetch_model /opt/models/bge-m3 \
 && useradd --uid 10003 --no-create-home --shell /usr/sbin/nologin embed
COPY server/src/reader/search/embed_service.py /usr/local/lib/python3.14/site-packages/reader/search/
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HOME=/tmp
USER 10003:10003
EXPOSE 8100
ENTRYPOINT ["python", "-I", "-m", "reader.search.embed_service"]
