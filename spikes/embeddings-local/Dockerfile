# Spike image (never shipped): local multilingual embeddings on CPU (roadmap 0.3, embeddings-local).
# Toolchain baseline: python:3.14-slim-trixie (proposed ADR-0020). Models are fetched at build time;
# runs use --network none and HF_HUB_OFFLINE=1.
#   docker build -t reader-spike-embeddings spikes/embeddings-local
#   docker build --build-arg MODELS=intfloat/multilingual-e5-small -t reader-spike-embeddings:smoke spikes/embeddings-local
FROM python:3.14-slim-trixie

# CPU-only torch: the default linux/aarch64 wheel pulls CUDA libraries, useless in Docker on macOS (no GPU).
RUN pip install --no-cache-dir torch==2.15.0 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir sentence-transformers==6.3.0 pypdfium2==5.15.2 pyyaml==6.0.3 \
      numpy==2.5.5 huggingface-hub==2.4.0

ARG MODELS="intfloat/multilingual-e5-small deepvk/USER-bge-m3 BAAI/bge-m3"
# Only the files the dense sentence-transformers path needs: no ONNX/OpenVINO copies, no duplicate .bin
# next to safetensors (e5-small shipped 3 × 449 MB of the same weights), no bge-m3 colbert/sparse heads.
COPY fetch_models.py /fetch_models.py
RUN python /fetch_models.py $MODELS && du -sh /models/*/*

ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
COPY bench.py /bench.py
ENTRYPOINT ["python", "/bench.py"]
