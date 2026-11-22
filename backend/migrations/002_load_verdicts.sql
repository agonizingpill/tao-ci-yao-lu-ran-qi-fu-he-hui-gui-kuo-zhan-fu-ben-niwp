-- 燃气参考区间判定：源数据版本、核查结论与签核留档
ALTER TABLE firing_batches ADD COLUMN source_revision integer NOT NULL DEFAULT 0;
ALTER TABLE load_runs ADD COLUMN source_revision integer NOT NULL DEFAULT 0;
ALTER TABLE load_runs ADD COLUMN verdict text;
-- 源数据更正后同一快照可再次试算，历史试算按源数据版本保留。
ALTER TABLE load_runs DROP CONSTRAINT load_runs_batch_code_input_hash_key;
ALTER TABLE load_runs ADD CONSTRAINT load_runs_batch_code_input_hash_revision_key
  UNIQUE (batch_code, input_hash, source_revision);

CREATE TABLE load_check_conclusions (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  batch_code text NOT NULL REFERENCES firing_batches(code),
  run_id bigint NOT NULL,
  input_hash text NOT NULL,
  source_revision integer NOT NULL DEFAULT 0,
  note text NOT NULL,
  registered_by text NOT NULL REFERENCES accounts(username),
  registered_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(batch_code, run_id, input_hash, source_revision),
  FOREIGN KEY(batch_code, run_id) REFERENCES load_runs(batch_code,id)
);
CREATE TABLE load_verdict_signoffs (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  batch_code text NOT NULL REFERENCES firing_batches(code),
  run_id bigint NOT NULL,
  input_hash text NOT NULL,
  source_revision integer NOT NULL,
  verdict text NOT NULL CHECK (verdict IN ('within','outside','indeterminate')),
  reference_low_m3 numeric(13,3) NOT NULL,
  reference_high_m3 numeric(13,3) NOT NULL,
  interval_level numeric(4,3) NOT NULL,
  conclusion_id bigint REFERENCES load_check_conclusions(id),
  basis jsonb NOT NULL,
  note text,
  signed_by text NOT NULL REFERENCES accounts(username),
  signed_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(batch_code, run_id),
  FOREIGN KEY(batch_code, run_id) REFERENCES load_runs(batch_code,id)
);
CREATE INDEX verdict_signoff_batch ON load_verdict_signoffs (batch_code, id);
