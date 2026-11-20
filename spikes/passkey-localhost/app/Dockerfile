# Spike image (never shipped): py_webauthn (ADR-0012) relying party on the proposed ADR-0020 baseline.
FROM python:3.14-slim-trixie
RUN pip install --no-cache-dir webauthn==3.0.1 fastapi==0.165.0 uvicorn==0.54.0 && useradd -m -u 1000 app
COPY main.py index.html /app/
USER app
WORKDIR /app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
