"""燃气参考区间判定：核查结论、签核留档与源数据失效。"""

from threading import Lock

from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb

from .infra import audit, connect
from .ledger import batch_row, require
from .schemas import BatchStatus

INVALIDATED = '源数据已更正，原判定失效，请重新试算'
_schema_lock = Lock()
_schema_ready = False

SCHEMA = (
    "ALTER TABLE firing_batches ADD COLUMN IF NOT EXISTS source_revision integer NOT NULL DEFAULT 0",
    "ALTER TABLE load_runs ADD COLUMN IF NOT EXISTS source_revision integer NOT NULL DEFAULT 0",
    "ALTER TABLE load_runs ADD COLUMN IF NOT EXISTS verdict text",
    """DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='load_runs_batch_code_input_hash_key') THEN
        ALTER TABLE load_runs DROP CONSTRAINT load_runs_batch_code_input_hash_key;
      END IF;
      IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='load_runs_batch_code_input_hash_revision_key') THEN
        ALTER TABLE load_runs ADD CONSTRAINT load_runs_batch_code_input_hash_revision_key
          UNIQUE (batch_code, input_hash, source_revision);
      END IF;
    END $$""",
    """
    CREATE TABLE IF NOT EXISTS load_check_conclusions (
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
    )
    """,
    """DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname='load_check_conclusions_batch_code_run_id_input_hash_key') THEN
        ALTER TABLE load_check_conclusions DROP CONSTRAINT load_check_conclusions_batch_code_run_id_input_hash_key;
        ALTER TABLE load_check_conclusions ADD CONSTRAINT load_check_conclusions_revision_key
          UNIQUE (batch_code, run_id, input_hash, source_revision);
      END IF;
    END $$""",
    """
    CREATE TABLE IF NOT EXISTS load_verdict_signoffs (
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS verdict_signoff_batch ON load_verdict_signoffs (batch_code, id)",
)


def ensure_schema():
    """Apply the additive verdict schema in its own autocommit connection.

    Callers must invoke this before issuing their first statement on the request
    connection, so the DDL connection never waits on locks held by the caller.
    """
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        conn = connect()
        try:
            conn.autocommit = True
            conn.execute("SET lock_timeout = '5s'")
            conn.execute("SELECT pg_advisory_lock(hashtext('kiln-verdict-schema'))")
            for statement in SCHEMA:
                conn.execute(statement)
        finally:
            conn.close()
        _schema_ready = True


def ready_run(conn, code, run_id, lock=False):
    sql = ("SELECT * FROM load_runs WHERE batch_code=%s AND id=%s AND status='ready'"
           + (' FOR UPDATE' if lock else ''))
    run = conn.execute(sql, (code, run_id)).fetchone()
    require(run, 'LOAD_NOT_READY', '该周期没有可判定的已完成试算')
    return run


def verdict_of(run):
    return run['verdict'] or (run['result'] or {}).get('verdict')


def require_interval_run(run):
    require(run['result'] and run['result'].get('result_version', 0) >= 2, 'RERUN_REQUIRED',
            '该试算没有参考区间，请重新试算后再判定')


def conclusion_for(conn, code, run_id):
    return conn.execute('''SELECT * FROM load_check_conclusions
                           WHERE batch_code=%s AND run_id=%s ORDER BY id DESC LIMIT 1''',
                        (code, run_id)).fetchone()


def signoff_for(conn, code, run_id):
    return conn.execute('SELECT * FROM load_verdict_signoffs WHERE batch_code=%s AND run_id=%s',
                        (code, run_id)).fetchone()


def basis_from(run):
    result = run['result']
    return jsonable_encoder({
        'input_hash': run['input_hash'],
        'source_revision': run['source_revision'],
        'expected_m3': result['expected_m3'],
        'actual_m3': result['actual_m3'],
        'delta_m3': result['delta_m3'],
        'mae_m3': result['mae_m3'],
        'interval': result['interval'],
        'coverage': result['coverage'],
        'verdict_detail': result['verdict_detail'],
        'model': {'seed': result['seed'], 'train_count': result['train_count'],
                  'holdout_count': result['holdout_count'], 'cv_folds': result['cv_folds'],
                  'alpha': result['alpha'], 'l1_ratio': result['l1_ratio'],
                  'training_sha256': result['training_sha256']},
    })


def register_conclusion(conn, code, run_id, form, actor):
    ensure_schema()
    batch = batch_row(conn, code, lock=True)
    require(batch['status'] != BatchStatus.confirmed, 'CYCLE_CLOSED', '周期已确认')
    run = ready_run(conn, code, run_id, lock=True)
    require_interval_run(run)
    require(verdict_of(run) == 'outside', 'CONCLUSION_NOT_REQUIRED',
            '只有超出参考区间的窑次需要登记核查结论')
    require(run['source_revision'] == batch['source_revision'], 'SOURCE_UPDATED', INVALIDATED)
    row = conn.execute('''INSERT INTO load_check_conclusions
        (batch_code,run_id,input_hash,source_revision,note,registered_by)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT(batch_code,run_id,input_hash,source_revision) DO NOTHING RETURNING *''',
        (code, run_id, run['input_hash'], run['source_revision'], form.note, actor)).fetchone()
    if row is None:
        row = conclusion_for(conn, code, run_id)
        require(row['registered_by'] == actor and row['note'] == form.note, 'CONCLUSION_EXISTS',
                '该试算已有核查结论，请沿用原结论或重新试算')
        return row
    audit(conn, code, actor, 'check_conclusion_registered', run_id=run_id,
          input_hash=run['input_hash'], note=form.note)
    return row


def sign_off(conn, code, run_id, form, actor):
    ensure_schema()
    batch = batch_row(conn, code, lock=True)
    require(batch['status'] != BatchStatus.confirmed, 'CYCLE_CLOSED', '周期已确认')
    run = ready_run(conn, code, run_id, lock=True)
    require_interval_run(run)
    require(run['source_revision'] == batch['source_revision'], 'SOURCE_UPDATED', INVALIDATED)
    signed = signoff_for(conn, code, run_id)
    require(not signed, 'VERDICT_SIGNED', '该试算判定已签核留档，不能覆盖')
    verdict = verdict_of(run)
    conclusion = None
    if verdict == 'outside':
        conclusion = conclusion_for(conn, code, run_id)
        require(conclusion, 'CONCLUSION_REQUIRED', '超出参考区间，请先登记核查结论')
        require(conclusion['input_hash'] == run['input_hash']
                and conclusion['source_revision'] == batch['source_revision'],
                'CONCLUSION_STALE', '核查结论对应的是旧读数，不能放行当前抄表')
    result = run['result']
    row = conn.execute('''INSERT INTO load_verdict_signoffs
        (batch_code,run_id,input_hash,source_revision,verdict,reference_low_m3,reference_high_m3,
         interval_level,conclusion_id,basis,note,signed_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
        (code, run_id, run['input_hash'], batch['source_revision'], verdict,
         result['interval']['low_m3'], result['interval']['high_m3'], result['interval']['level'],
         conclusion['id'] if conclusion else None, Jsonb(basis_from(run)), form.note, actor)).fetchone()
    audit(conn, code, actor, 'verdict_signed', run_id=run_id, input_hash=run['input_hash'],
          verdict=verdict, conclusion_id=row['conclusion_id'],
          low_m3=result['interval']['low_m3'], high_m3=result['interval']['high_m3'])
    return row


def correct_meter(conn, code, form, actor):
    """Append or replace a gas meter reading after freeze; old verdicts must be retried."""
    ensure_schema()
    batch = batch_row(conn, code, lock=True)
    require(batch['status'] != BatchStatus.confirmed, 'CYCLE_CLOSED', '周期已确认')
    require(batch['opened_at'] <= form.sampled_at <= batch['closed_at'], 'METER_OUTSIDE_CYCLE',
            '抄表时间超出烧成周期')
    samples = conn.execute('SELECT * FROM gas_meter WHERE batch_code=%s ORDER BY sampled_at',
                           (code,)).fetchall()
    for sample in samples:
        require(sample['meter_serial'] == form.meter_serial, 'METER_MISMATCH', '同批次必须使用同一台燃气总表')
        if sample['sampled_at'] == form.sampled_at:
            continue
        before = sample['sampled_at'] < form.sampled_at
        valid = sample['reading_m3'] <= form.reading_m3 if before else form.reading_m3 <= sample['reading_m3']
        require(valid, 'COUNTER_ROLLBACK', '累计立方米不能倒退，请核对原始表单')
    previous = next((row for row in samples if row['sampled_at'] == form.sampled_at), None)
    archived = [row for row in conn.execute(
        'SELECT run_id,input_hash,verdict FROM load_verdict_signoffs WHERE batch_code=%s ORDER BY id',
        (code,)).fetchall()]
    if previous:
        row = conn.execute('''UPDATE gas_meter SET meter_serial=%s,reading_m3=%s,source_file=%s,recorded_by=%s
            WHERE batch_code=%s AND sampled_at=%s RETURNING *''',
            (form.meter_serial, form.reading_m3, form.source_file, actor, code, form.sampled_at)).fetchone()
        event = 'meter_corrected'
    else:
        row = conn.execute('''INSERT INTO gas_meter(batch_code,sampled_at,meter_serial,reading_m3,source_file,recorded_by)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING *''',
            (code, form.sampled_at, form.meter_serial, form.reading_m3, form.source_file, actor)).fetchone()
        event = 'meter_supplemented'
    revision = conn.execute('''UPDATE firing_batches SET source_revision=source_revision+1,
        status=CASE WHEN status='analyzed' THEN 'recording' ELSE status END
        WHERE code=%s RETURNING source_revision''', (code,)).fetchone()['source_revision']
    audit(conn, code, actor, event, sample=form.sampled_at.isoformat(), source=form.source_file,
          reason=form.reason, previous=jsonable_encoder(previous) if previous else None,
          source_revision=revision, invalidated_signoffs=archived)
    return {**row, 'source_revision': revision,
            'invalidated_signoffs': [{'run_id': row['run_id'], 'input_hash': row['input_hash'],
                                      'verdict': row['verdict']} for row in archived]}


def status_for(batch, runs, signoffs, conclusions):
    latest = next((run for run in runs if run['status'] == 'ready'), None)
    if not latest:
        return {'code': 'no_ready_run', 'label': '暂无有效试算'}
    if not (latest['result'] or {}).get('interval'):
        return {'code': 'needs_rerun', 'label': '旧版试算，待重新试算'}
    signoff = signoffs.get(latest['id'])
    stale = latest['source_revision'] != batch['source_revision']
    if signoff:
        if stale:
            return {'code': 'stale', 'label': '源数据更正，判定失效'}
        return {'code': 'signed', 'label': '判定已签核'}
    if stale:
        return {'code': 'stale', 'label': '源数据更正，请重新试算'}
    if verdict_of(latest) == 'outside' and latest['id'] not in conclusions:
        return {'code': 'needs_conclusion', 'label': '超区间，待登记核查结论'}
    return {'code': 'ready_to_sign', 'label': '待签核'}


def annotate(conn, batch, runs):
    if not runs:
        return []
    conclusions = {row['run_id']: row for row in conn.execute(
        'SELECT * FROM load_check_conclusions WHERE batch_code=%s ORDER BY id', (batch['code'],)).fetchall()}
    signoffs = {row['run_id']: row for row in conn.execute(
        'SELECT * FROM load_verdict_signoffs WHERE batch_code=%s ORDER BY id', (batch['code'],)).fetchall()}
    annotated = []
    for run in runs:
        signoff = signoffs.get(run['id'])
        conclusion = conclusions.get(run['id'])
        current = run['source_revision'] == batch['source_revision']
        annotated.append({
            **run,
            'source_stale': not current,
            'stale_reason': None if current else INVALIDATED,
            'conclusion': conclusion,
            'signoff': signoff,
            'signoff_valid': bool(signoff and current),
        })
    return annotated


def list_verdicts(conn):
    ensure_schema()
    batches = conn.execute('SELECT * FROM firing_batches ORDER BY opened_at DESC, code').fetchall()
    runs = conn.execute('SELECT * FROM load_runs ORDER BY id DESC').fetchall()
    conclusions = {}
    for row in conn.execute('SELECT * FROM load_check_conclusions ORDER BY id').fetchall():
        conclusions.setdefault(row['run_id'], row)
    signoffs = {row['run_id']: row for row in conn.execute(
        'SELECT * FROM load_verdict_signoffs ORDER BY id').fetchall()}
    grouped = {}
    for run in runs:
        grouped.setdefault(run['batch_code'], []).append(run)
    rows = []
    for batch in batches:
        batch_runs = grouped.get(batch['code'], [])
        latest = next((run for run in batch_runs if run['status'] == 'ready'), None)
        status = status_for(batch, batch_runs, signoffs, conclusions)
        result = (latest or {}).get('result') or {}
        interval = result.get('interval') or {}
        rows.append({
            'code': batch['code'], 'kiln': batch['kiln'], 'status': batch['status'],
            'verdict_status': status['code'], 'verdict_status_label': status['label'],
            'run_id': latest['id'] if latest else None,
            'verdict': verdict_of(latest) if latest else None,
            'expected_m3': result.get('expected_m3'),
            'actual_m3': result.get('actual_m3'),
            'low_m3': interval.get('low_m3'),
            'high_m3': interval.get('high_m3'),
            'signed_by': signoffs[latest['id']]['signed_by'] if latest and latest['id'] in signoffs else None,
        })
    return rows
