# Spike image (never shipped): Docling on CPU in Docker (ADR-0008; no Apple GPU inside containers).
FROM python:3.13-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 libgl1 \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "docling[easyocr]==2.151.1" rapidfuzz==3.14.6

# Models are fetched at build time; the run itself needs no network.
RUN docling-tools models download layout tableformer code_formula easyocr \
      --easyocr-lang ru --easyocr-lang en -o /models

COPY convert.py /convert.py
ENTRYPOINT ["python", "/convert.py"]
