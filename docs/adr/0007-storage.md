---
id: ADR-0007
title: Storage — Postgres with pgvector, content-addressed blobs on a volume
status: proposed
date: 2026-11-20
verified_by: "OPS-BACKUP: restore of pgdata dump plus blobs volume reproduces a published lesson byte for byte; SRV-SEARCH recall@10 on the pilot question set"
revisit_when: "Vector count above 5 million, or hybrid search recall@10 below 0.8 on the pilot question set"
---

## Context

Data kinds: source material (PDFs up to tens of MB; the pilot PDF is 20.9 MB, 935 pages), parsed text and chunks, embeddings for retrieval, notebook revisions, learner state, events, jobs, findings for the knowledge base.

## Decision

- **Postgres** (with the `vector` extension) holds all structured state, events, jobs, chunk text, embeddings and full-text indexes. Retrieval is hybrid: `tsvector` full-text plus pgvector HNSW, merged by reciprocal rank fusion.
- **Blobs** (uploads, notebook revisions, rendered artifacts) are files on the `blobs` volume named by sha256; Postgres stores the hash and metadata. Revisions are immutable; a lesson revision is a pointer to a blob.
- Embedding model is a config entry (ADR-0011); vectors carry the model id, so a model change is a re-embed job, not a migration.

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **Postgres + pgvector + blob volume** | One database to back up; transactional consistency between chunks, vectors and state | Vector search tuned by hand (HNSW parameters) |
| Postgres + dedicated vector DB (Qdrant) | Better vector tooling | Second store to keep consistent with chunks |
| Blobs inside Postgres (`bytea`) | One backup artifact | 20.9 MB rows bloat WAL and backups; streaming files through the DB |
| Per-course git repository for notebooks | History and diffs for free | Merge semantics for pipeline-owned vs learner-owned cells need custom tooling |

## Rejected options — why (numbers)

- **Qdrant.** The pilot is ~935 pages; at roughly 2 chunks per page that is ~2,000 vectors, three orders of magnitude below where pgvector HNSW needs tuning effort (revisit at 5 million). A second store would add a consistency path for no measured gain.
- **Blobs in Postgres.** Every upload passes through WAL and every base backup grows by the full material size; files on a volume are copied once.
- **Git per course.** Possible later as an export format; as the primary store it couples learner state to merge conflicts (ADR-0004 keeps learner state out of the notebook file).

## Consequences

- Backup is two artifacts: `pg_dump` and the `blobs` volume (ADR-0012).
- Search quality is measured on a fixed pilot question set (VEC fixture in SRV-SEARCH), not asserted.
