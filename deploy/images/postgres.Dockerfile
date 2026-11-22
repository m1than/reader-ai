# reader-postgres:local — the pinned official image plus the initdb-only secret wait (OPS-DEPLOY FR-DEPLOY-9/10).
# Build context: deploy/images. postgres:18-trixie (18.7); its postgres user is uid 999.
FROM postgres:18-trixie@sha256:eb63372c1e05c755f55fa30cbd07aecc443676fc6c0fa90b9002d01537841e1a
# pgvector for SRV-SEARCH (ADR-0028, FR-DEPLOY-2): from the base image's own PGDG apt source, exact version.
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-18-pgvector=0.8.7-1.pgdg13+2 \
 && rm -rf /var/lib/apt/lists/*
COPY --chmod=0755 reader-postgres-entrypoint.sh /usr/local/bin/reader-postgres-entrypoint
ENTRYPOINT ["/usr/local/bin/reader-postgres-entrypoint"]
CMD ["postgres"]
