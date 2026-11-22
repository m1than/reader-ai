"""Search (SRV-SEARCH, slice): bge-m3 chunks and Postgres full text, fused by reciprocal rank, with span ids.

This package's `__init__` imports nothing: the embed image (ADR-0028) carries only this file,
embed_service.py, fetch_model.py and models.lock.json, and `python -I -m reader.search.embed_service` must
import no other module.
"""
