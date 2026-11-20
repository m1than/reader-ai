# Spike image (never shipped): Docling on CPU in Docker (ADR-0008; no Apple GPU inside containers).
# Toolchain baseline (owner, 2026-11-20: latest LTS/stable; proposed ADR-0020): Debian 13 trixie and the newest
# Python with stable wheels for the whole stack. 3.15 fails here: scikit-image (EasyOCR) has no stable cp315 wheel.
FROM python:3.14-slim-trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 libgl1 \
 && rm -rf /var/lib/apt/lists/*

# CPU-only torch first: the default linux/aarch64 wheel pulls CUDA libraries (cuDNN alone 595 MB), useless in
# Docker on macOS (no GPU, ADR-0008). Docling then reuses the installed torch.
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir "docling[easyocr]==2.151.1" rapidfuzz==3.14.6

# Models are fetched at build time; the run itself needs no network.
RUN docling-tools models download layout tableformer code_formula easyocr \
      --easyocr-lang ru --easyocr-lang en -o /models

COPY convert.py /convert.py
ENTRYPOINT ["python", "/convert.py"]
