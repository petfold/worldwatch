-- Worldwatch's exported archive as DuckDB views. Run from the archive directory:
--   cd ~/worldwatch-archive && duckdb archive.duckdb < /path/to/worldwatch/ops/local/archive.sql
-- A row can appear in more than one batch (a bin merged again later; each
-- run's first row repeats the previous run's last): per primary key, the row
-- with the highest (_rowid, _batch) is the current one.
CREATE OR REPLACE VIEW surprise AS
    SELECT * EXCLUDE (_rowid, _batch) FROM read_parquet('surprise/*.parquet')
    QUALIFY row_number() OVER (PARTITION BY stream_id, cell, scale, bin_start
                               ORDER BY _rowid DESC, _batch DESC) = 1;
CREATE OR REPLACE VIEW bins AS
    SELECT * EXCLUDE (_rowid, _batch) FROM read_parquet('bins/*.parquet')
    QUALIFY row_number() OVER (PARTITION BY stream_id, cell, scale, bin_start
                               ORDER BY _rowid DESC, _batch DESC) = 1;
CREATE OR REPLACE VIEW model_state    AS SELECT * FROM read_parquet('snapshots/model_state.parquet');
CREATE OR REPLACE VIEW presence_state AS SELECT * FROM read_parquet('snapshots/presence_state.parquet');
CREATE OR REPLACE VIEW alerts         AS SELECT * FROM read_parquet('snapshots/alerts.parquet');
CREATE OR REPLACE VIEW push_log       AS SELECT * FROM read_parquet('snapshots/push_log.parquet');
CREATE OR REPLACE VIEW digests        AS SELECT * FROM read_parquet('snapshots/digests.parquet');
CREATE OR REPLACE VIEW health         AS SELECT * FROM read_parquet('snapshots/health.parquet');
CREATE OR REPLACE VIEW sources        AS SELECT * FROM read_parquet('snapshots/sources.parquet');
