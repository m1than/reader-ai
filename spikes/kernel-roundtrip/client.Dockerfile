# Spike client image (never shipped): client.py with its dependencies, run on the spike's internal network.
FROM python:3.14-slim-trixie
RUN pip install -q --no-cache-dir httpx==0.28.1 websockets==17.4
COPY client.py /client.py
