-- Spike retrieval-ablation, stage 2 (throwaway pgvector container, --network none, data on tmpfs):
-- the OR full-text list over spans exactly as SRV-SEARCH builds it (search/store.py fts_list, russian config,
-- ts_rank_cd, ties by id), and the vector step alone, exact scan vs HNSW, at the measured size.
-- Output: /out/fts.json and /out/latency.json, ids and numbers only.
\set ON_ERROR_STOP on
CREATE EXTENSION vector;

CREATE TABLE raw (line text);
COPY raw FROM '/in/spans.jsonl' WITH (FORMAT csv, QUOTE E'\x01', DELIMITER E'\x02');
CREATE TABLE spans AS
  SELECT (line::jsonb->>'seq')::int AS seq, line::jsonb->>'text' AS text FROM raw;
DROP TABLE raw;
DELETE FROM spans WHERE btrim(text) = '';
ALTER TABLE spans ADD COLUMN fts tsvector;
UPDATE spans SET fts = to_tsvector('russian', text);
CREATE INDEX ON spans USING gin (fts);

CREATE TABLE q (id text, question text, v vector(1024));
COPY q FROM '/in/queries_vec.tsv';
CREATE TABLE vec (seq int, page int, kind text, embedding vector(1024));
COPY vec FROM '/in/spans_vec.tsv';
VACUUM ANALYZE spans; VACUUM ANALYZE vec;

-- full text: the query's own lexemes, quoted (fusion.quote_lexeme) and OR-ed; top 50 by ts_rank_cd, then id
CREATE TABLE fts_rank AS
SELECT q.id, coalesce((
  SELECT array_agg(seq ORDER BY r DESC, seq) FROM (
    SELECT s.seq, ts_rank_cd(s.fts, t.tsq) AS r FROM spans s
    WHERE s.fts @@ t.tsq ORDER BY r DESC, s.seq LIMIT 50) x), '{}') AS ids
FROM q CROSS JOIN LATERAL (
  SELECT (SELECT string_agg('''' || replace(replace(l, '\', '\\'), '''', '''''') || '''', ' | ' ORDER BY l)
          FROM unnest(tsvector_to_array(to_tsvector('russian', q.question))) l)::tsquery AS tsq) t;
\copy (SELECT json_object_agg(id, ids) FROM fts_rank) TO '/out/fts.json'

-- vector step alone: SELECT seq ORDER BY distance LIMIT 50 (the candidate count, params.CANDIDATES)
CREATE TABLE lat (variant text, qid text, rep int, ms float8, ids int[]);
CREATE FUNCTION bench(variant text, reps int) RETURNS void LANGUAGE plpgsql AS $$
DECLARE r record; t0 timestamptz; got int[];
BEGIN
  FOR i IN 0..reps LOOP            -- pass 0 warms the cache and is not kept
    FOR r IN SELECT id, v FROM q ORDER BY id LOOP
      t0 := clock_timestamp();
      SELECT array_agg(seq) INTO got FROM (SELECT seq FROM vec ORDER BY embedding <=> r.v LIMIT 50) x;
      IF i > 0 THEN
        INSERT INTO lat VALUES (variant, r.id, i, extract(epoch FROM clock_timestamp() - t0) * 1000, got);
      END IF;
    END LOOP;
  END LOOP;
END $$;

SELECT bench('exact', 5);
\o /out/plan_exact.txt
EXPLAIN SELECT seq FROM vec ORDER BY embedding <=> (SELECT v FROM q LIMIT 1) LIMIT 50;
\o
\timing on
CREATE INDEX vec_hnsw ON vec USING hnsw (embedding vector_cosine_ops);   -- defaults m 16, ef_construction 64
\timing off
SET enable_seqscan = off;  -- the plan files show which scan each variant used
SET hnsw.ef_search = 40;   -- default
SELECT bench('hnsw_ef40', 5);
SET hnsw.ef_search = 100;
SELECT bench('hnsw_ef100', 5);
\o /out/plan_hnsw.txt
EXPLAIN SELECT seq FROM vec ORDER BY embedding <=> (SELECT v FROM q LIMIT 1) LIMIT 50;
\o

\copy (SELECT json_build_object('pgvector', (SELECT extversion FROM pg_extension WHERE extname = 'vector'), 'postgres', current_setting('server_version'), 'rows', (SELECT count(*) FROM vec), 'table_mb', round(pg_total_relation_size('vec') / 1048576.0, 1), 'hnsw_mb', round(pg_relation_size('vec_hnsw') / 1048576.0, 1), 'variants', (SELECT json_object_agg(variant, s) FROM (SELECT l.variant, json_build_object('n', count(*), 'p50_ms', round(percentile_cont(0.5) WITHIN GROUP (ORDER BY l.ms)::numeric, 2), 'p95_ms', round(percentile_cont(0.95) WITHIN GROUP (ORDER BY l.ms)::numeric, 2), 'max_ms', round(max(l.ms)::numeric, 2), 'recall10_vs_exact', round(avg((SELECT count(*) FROM unnest(l.ids[1:10]) a WHERE a = ANY(e.ids[1:10])) / 10.0)::numeric, 4), 'recall50_vs_exact', round(avg((SELECT count(*) FROM unnest(l.ids) a WHERE a = ANY(e.ids)) / 50.0)::numeric, 4)) AS s FROM lat l JOIN lat e ON e.variant = 'exact' AND e.qid = l.qid AND e.rep = l.rep GROUP BY l.variant) v))) TO '/out/latency.json'
