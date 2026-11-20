# reader-postgres:local — the pinned official image plus the initdb-only secret wait (OPS-DEPLOY FR-DEPLOY-9/10).
# Build context: deploy/images. postgres:18-trixie (18.7); its postgres user is uid 999.
FROM postgres:18-trixie@sha256:eb63372c1e05c755f55fa30cbd07aecc443676fc6c0fa90b9002d01537841e1a
COPY --chmod=0755 reader-postgres-entrypoint.sh /usr/local/bin/reader-postgres-entrypoint
ENTRYPOINT ["/usr/local/bin/reader-postgres-entrypoint"]
CMD ["postgres"]
