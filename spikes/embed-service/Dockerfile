# Spike image (never shipped): bge-m3 embed service footprint under the OPS-DEPLOY hardening flags.
# The model is baked in at build time at a pinned revision; runs use --network none, read-only root,
# cap-drop ALL, no-new-privileges, a non-root uid and a tmpfs /tmp.
#   docker build --platform linux/arm64 -t reader-spike-embed-service spikes/embed-service
FROM python:3.14-slim-trixie

# CPU-only torch (same pins as spikes/embeddings-local).
RUN pip install --no-cache-dir torch==2.15.0 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir sentence-transformers==6.3.0 numpy==2.5.5 huggingface-hub==2.4.0

ARG MODEL=BAAI/bge-m3
ARG REVISION=5617a9f61b028005a4858fdac845db406aefb181
# Dense path only: no ONNX copy, no colbert/sparse heads, no images.
RUN python -c "from huggingface_hub import snapshot_download as s; s('$MODEL', revision='$REVISION', local_dir='/models/$MODEL', ignore_patterns=['onnx/*','*.onnx','imgs/*','*.jpg','colbert_linear.pt','sparse_linear.pt'])" \
 && rm -rf /models/$MODEL/.cache && du -sh /models/$MODEL

RUN useradd --uid 10003 --no-create-home --shell /usr/sbin/nologin embed
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HOME=/tmp
COPY measure.py /measure.py
USER 10003
ENTRYPOINT ["python", "-I", "/measure.py"]
