"""Reference-interval verdict, conclusion, sign-off and source invalidation rules."""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DATABASE_URL = os.environ.get('TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason='TEST_DATABASE_URL not configured')

SHOP_TZ = timezone(timedelta(hours=8))


@pytest.fixture(scope='session')
def client():
    os.environ['DATABASE_URL'] = DATABASE_URL
    os.environ.setdefault('JWT_SECRET', 'test-secret')
    os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:1/0')
    from kiln import app as application, ledger, reviews, verdicts

    conn = psycopg.connect(DATABASE_URL, autocommit=True)
    conn.execute('DROP SCHEMA public CASCADE')
    conn.execute('CREATE SCHEMA public')
    for path in ('001_kiln_ledger.sql', '002_load_verdicts.sql'):
        conn.execute((Path(__file__).resolve().parents[1] / 'migrations' / path).read_text())
    verdicts.ensure_schema()
    from kiln.infra import password_digest
    for username, role in [('firing', 'operator'), ('chief', 'supervisor'), ('backup', 'supervisor'), ('energy', 'viewer')]:
        conn.execute('''INSERT INTO accounts VALUES (%s,%s,%s) ON CONFLICT (username) DO NOTHING''',
                     (username, password_digest('KilnLab!1073'), role))
    conn.close()
    application.app.state.ledger = ledger
    application.app.state.reviews = reviews
    with TestClient(application.app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean(client):
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.execute('DELETE FROM load_verdict_signoffs')
        conn.execute('DELETE FROM load_check_conclusions')
        conn.execute('DELETE FROM cycle_reviews')
        conn.execute('DELETE FROM load_runs')
        conn.execute('DELETE FROM recipe_changes')
        conn.execute('DELETE FROM gas_meter')
        conn.execute('DELETE FROM kiln_curve')
        conn.execute('DELETE FROM firing_batches')
        conn.execute('DELETE FROM ledger_audit')


def token(client, username):
    response = client.post('/login', json={'username': username, 'password': 'KilnLab!1073'})
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def make_batch(client, code='K2-TEST-A', mass=1170, peak=1180, expected_m3=None):
    start = datetime(2026, 11, 21, 6, tzinfo=SHOP_TZ)
    end = start + timedelta(hours=8)
    headers = token(client, 'firing')
    response = client.post('/batches', headers=headers, json={
        'code': code, 'kiln': 'K2 辊道窑', 'recipe': '釉面砖 Y17', 'baseline_version': 3,
        'batch_kg': mass, 'opened_at': start.isoformat(), 'closed_at': end.isoformat(), 'planned_minutes': 480})
    assert response.status_code == 201, response.text
    for phase, a, b, p in [('ramp', 0, 3, 1050), ('soak', 3, 5, peak), ('cool', 5, 8, 1000)]:
        client.post(f'/batches/{code}/curve', headers=headers, json={
            'phase': phase, 'started_at': (start + timedelta(hours=a)).isoformat(),
            'ended_at': (start + timedelta(hours=b)).isoformat(), 'peak_c': p, 'source_file': f'{code}-TC.csv'})
    total = expected_m3 if expected_m3 is not None else 112.7
    for hours, fraction in [(0, 0), (2, .27), (4, .61), (6, .87), (8, 1)]:
        client.post(f'/batches/{code}/meter', headers=headers, json={
            'sampled_at': (start + timedelta(hours=hours)).isoformat(), 'meter_serial': 'GM-K2-08',
            'reading_m3': round(1250.4 + total * fraction, 3), 'source_file': f'{code}-GAS.csv'})
    return code


def predicted_m3(mass=1170, peak=1180):
    from kiln.regression import estimate
    start = datetime(2026, 11, 21, 6, tzinfo=SHOP_TZ)
    snapshot = {
        'cycle': {'batch_kg': mass},
        'curve': [
            {'started_at': start.isoformat(), 'ended_at': (start + timedelta(hours=3)).isoformat(), 'peak_c': 1050},
            {'started_at': (start + timedelta(hours=3)).isoformat(), 'ended_at': (start + timedelta(hours=5)).isoformat(), 'peak_c': peak},
            {'started_at': (start + timedelta(hours=5)).isoformat(), 'ended_at': (start + timedelta(hours=8)).isoformat(), 'peak_c': 1000},
        ],
        'meter': [{'reading_m3': 0}, {'reading_m3': 0}],
    }
    return estimate(snapshot)


def run_load(client, code):
    from kiln import ledger, worker
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        record = ledger.request_load(conn, code, 'firing')
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        worker.process_next(record['id'])
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        return conn.execute('SELECT * FROM load_runs WHERE id=%s', (record['id'],)).fetchone()


def signoff(client, code, run_id, username='chief', note='已复核原始抄表'):
    return client.post(f'/batches/{code}/runs/{run_id}/signoff',
                       headers=token(client, username), json={'note': note})


def conclusion(client, code, run_id, note='核对过燃气表与交接记录', username='firing'):
    return client.post(f'/batches/{code}/runs/{run_id}/conclusion',
                       headers=token(client, username), json={'note': note})


def test_interval_calibrated_from_holdout_and_verdict_recorded(client):
    code = make_batch(client)
    run = run_load(client, code)
    result = run['result']
    assert run['status'] == 'ready'
    assert result['interval']['calibration_count'] == 40 == result['holdout_count']
    assert result['interval']['low_m3'] < result['expected_m3'] < result['interval']['high_m3']
    assert result['coverage']['covered'] is True
    assert result['verdict'] in {'within', 'outside'}
    assert run['verdict'] == result['verdict']
    assert result['verdict_detail']['reason']


def test_outside_run_needs_conclusion_before_signoff(client):
    code = make_batch(client)
    run = run_load(client, code)
    assert run['verdict'] == 'outside'
    blocked = signoff(client, code, run['id'])
    assert blocked.status_code == 409
    assert blocked.json()['code'] == 'CONCLUSION_REQUIRED'
    missing = client.post(f'/batches/{code}/runs/{run["id"]}/signoff',
                          headers=token(client, 'chief'), json={})
    assert missing.status_code == 409
    assert conclusion(client, code, run['id']).status_code == 201
    signed = signoff(client, code, run['id'])
    assert signed.status_code == 201, signed.text
    body = signed.json()
    assert body['verdict'] == 'outside'
    assert float(body['reference_low_m3']) < float(body['reference_high_m3'])
    assert body['conclusion_id'] is not None


def test_within_or_indeterminate_signs_without_conclusion(client):
    baseline = predicted_m3()
    inside = (baseline['interval']['low_m3'] + baseline['interval']['high_m3']) / 2
    code = make_batch(client, code='K2-TEST-B', expected_m3=round(inside, 3))
    run = run_load(client, code)
    assert run['verdict'] == 'within', run['result']['verdict_detail']
    irrelevant = conclusion(client, code, run['id'])
    assert irrelevant.status_code == 409
    assert irrelevant.json()['code'] == 'CONCLUSION_NOT_REQUIRED'
    signed = signoff(client, code, run['id'])
    assert signed.status_code == 201
    assert signed.json()['conclusion_id'] is None


def test_uncovered_loading_or_profile_is_indeterminate(client):
    code = make_batch(client, code='K2-TEST-C', mass=1900, peak=1180)
    run = run_load(client, code)
    assert run['verdict'] == 'indeterminate'
    assert run['result']['coverage']['covered'] is False
    assert '超出训练样本覆盖范围' in run['result']['verdict_detail']['reason']
    signed = signoff(client, code, run['id'])
    assert signed.status_code == 201
    assert signed.json()['verdict'] == 'indeterminate'


def test_unseen_loading_profile_combination_is_indeterminate(client):
    # Each input alone is plausible, but the loading-and-profile combination is outside the
    # training correlation envelope, so it must not be silently judged.
    from kiln.regression import FEATURES, estimate, historical_batches
    train_x, _ = historical_batches()
    means = train_x.mean(axis=0)
    lows = train_x.min(axis=0)
    highs = train_x.max(axis=0)
    combination = {}
    for index, name in enumerate(FEATURES):
        value = means[index]
        while value < highs[index] - 1e-6:
            trial = dict(combination, **{name: float(value)})
            snapshot = profile_snapshot(trial)
            if estimate(snapshot)['coverage']['joint']['inside'] is False:
                break
            value += (highs[index] - means[index]) / 40
        else:
            continue
        combination[name] = float(value)
        break
    if not combination:
        pytest.skip('no single-feature extreme leaves the joint envelope with this seed')
    assert all(lows[FEATURES.index(name)] <= value <= highs[FEATURES.index(name)]
               for name, value in combination.items())
    result = estimate(profile_snapshot(combination))
    assert result['coverage']['covered'] is False
    assert result['coverage']['joint']['inside'] is False
    assert result['verdict'] == 'indeterminate'
    assert '组合超出训练样本覆盖范围' in result['verdict_detail']['reason']


def profile_snapshot(overrides=None):
    from kiln.regression import FEATURES
    values = dict(zip(FEATURES, [1170, 3, 2, 3, 1050, 1180, 1000]))
    values.update(overrides or {})
    start = datetime(2026, 11, 21, 6, tzinfo=SHOP_TZ)
    spans = [('ramp', 0, values['ramp_hours']), ('soak', values['ramp_hours'],
             values['ramp_hours'] + values['soak_hours']),
             ('cool', values['ramp_hours'] + values['soak_hours'],
              values['ramp_hours'] + values['soak_hours'] + values['cool_hours'])]
    peaks = {'ramp': values['ramp_peak_c'], 'soak': values['soak_peak_c'], 'cool': values['cool_peak_c']}
    return {'cycle': {'batch_kg': values['batch_kg']},
            'curve': [{'started_at': (start + timedelta(hours=a)).isoformat(),
                       'ended_at': (start + timedelta(hours=b)).isoformat(), 'peak_c': peaks[phase]}
                      for phase, a, b in spans],
            'meter': [{'reading_m3': 1200.0}, {'reading_m3': 1200.0 + 150.0}]}


def test_signoff_is_archived_and_not_overwritten(client):
    code = make_batch(client)
    run = run_load(client, code)
    conclusion(client, code, run['id'])
    first = signoff(client, code, run['id'], note='首轮签核')
    assert first.status_code == 201
    second = signoff(client, code, run['id'], username='backup', note='二次签核')
    assert second.status_code == 409
    assert second.json()['code'] == 'VERDICT_SIGNED'
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute('SELECT signed_by,note,verdict,basis FROM load_verdict_signoffs WHERE batch_code=%s',
                            (code,)).fetchall()
    assert len(rows) == 1
    assert rows[0]['signed_by'] == 'chief' and rows[0]['note'] == '首轮签核'
    assert rows[0]['basis']['interval']['calibration_count'] == 40


def test_source_correction_invalidates_old_signoff_and_blocks_reuse(client):
    code = make_batch(client)
    run = run_load(client, code)
    conclusion(client, code, run['id'])
    assert signoff(client, code, run['id']).status_code == 201
    headers = token(client, 'firing')
    correction = client.post(f'/batches/{code}/meters/correction', headers=headers, json={
        'sampled_at': datetime(2026, 11, 21, 6, tzinfo=SHOP_TZ).isoformat(), 'meter_serial': 'GM-K2-08',
        'reading_m3': 1251.0, 'source_file': f'{code}-GAS.csv', 'reason': '复核原始表单首点读错'})
    assert correction.status_code == 201, correction.text
    assert correction.json()['invalidated_signoffs'][0]['run_id'] == run['id']
    dossier = client.get(f'/batches/{code}', headers=headers).json()
    assert dossier['source_revision'] == 1
    stamped = next(item for item in dossier['runs'] if item['id'] == run['id'])
    assert stamped['source_stale'] is True
    assert stamped['signoff_valid'] is False
    assert stamped['stale_reason']
    stale = signoff(client, code, run['id'], note='沿用旧判定')
    assert stale.status_code == 409
    assert stale.json()['code'] == 'SOURCE_UPDATED'
    stale_conclusion = conclusion(client, code, run['id'])
    assert stale_conclusion.status_code == 409


def test_corrected_source_rerun_supersedes_and_allows_new_signoff(client):
    code = make_batch(client)
    old = run_load(client, code)
    conclusion(client, code, old['id'])
    assert signoff(client, code, old['id']).status_code == 201
    headers = token(client, 'firing')
    client.post(f'/batches/{code}/meters/correction', headers=headers, json={
        'sampled_at': datetime(2026, 11, 21, 14, tzinfo=SHOP_TZ).isoformat(), 'meter_serial': 'GM-K2-08',
        'reading_m3': 1363.5, 'source_file': f'{code}-GAS.csv', 'reason': '出窑读数按原始表单更正'})
    fresh = run_load(client, code)
    assert fresh['id'] != old['id']
    assert fresh['source_revision'] == 1
    dossier = client.get(f'/batches/{code}', headers=headers).json()
    archived = next(item for item in dossier['runs'] if item['id'] == old['id'])
    assert archived['signoff']['signed_by'] == 'chief'
    assert archived['signoff_valid'] is False
    latest = dossier['runs'][0]
    assert latest['id'] == fresh['id'] and latest['source_stale'] is False
    if latest['verdict'] == 'outside':
        conclusion(client, code, fresh['id'])
    assert signoff(client, code, fresh['id'], note='新读数复核').status_code == 201


def test_cycle_confirmation_requires_valid_signoff(client):
    code = make_batch(client)
    run = run_load(client, code)
    headers = token(client, 'firing')
    proposed = client.post(f'/batches/{code}/review', headers=headers, json={
        'run_id': run['id'], 'next_minutes': 465, 'attribution': 'observed', 'rationale': '按实测负荷调整节拍'})
    assert proposed.status_code == 200, proposed.text
    blocked = client.post(f'/batches/{code}/review/confirm', headers=token(client, 'chief'))
    assert blocked.status_code == 409
    assert blocked.json()['code'] == 'VERDICT_NOT_SIGNED'
    conclusion(client, code, run['id'])
    assert signoff(client, code, run['id']).status_code == 201
    confirmed = client.post(f'/batches/{code}/review/confirm', headers=token(client, 'chief'))
    assert confirmed.status_code == 200, confirmed.text


def test_concurrent_correction_cannot_release_old_conclusion_for_new_readings(client):
    from concurrent.futures import ThreadPoolExecutor

    code = make_batch(client)
    run = run_load(client, code)
    conclusion(client, code, run['id'])
    headers = token(client, 'firing')

    def correct():
        return client.post(f'/batches/{code}/meters/correction', headers=headers, json={
            'sampled_at': datetime(2026, 11, 21, 14, tzinfo=SHOP_TZ).isoformat(), 'meter_serial': 'GM-K2-08',
            'reading_m3': 1364.2, 'source_file': f'{code}-GAS.csv', 'reason': '出窑读数与原始表单不符'})

    def sign():
        return signoff(client, code, run['id'], note='并发签核')

    with ThreadPoolExecutor(max_workers=2) as pool:
        correction = pool.submit(correct)
        signing = pool.submit(sign)
        correction_result, sign_result = correction.result(), signing.result()

    assert correction_result.status_code == 201, correction_result.text
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        batch = conn.execute('SELECT source_revision FROM firing_batches WHERE code=%s', (code,)).fetchone()
        archived = conn.execute('SELECT * FROM load_verdict_signoffs WHERE batch_code=%s', (code,)).fetchall()
    # Either the sign-off lost the race and was rejected, or it committed first and is now stale.
    assert sign_result.status_code in {201, 409}
    if sign_result.status_code == 409:
        assert sign_result.json()['code'] in {'SOURCE_UPDATED', 'CONCLUSION_STALE'}
        assert not archived
    else:
        assert len(archived) == 1
        assert archived[0]['source_revision'] < batch['source_revision']
    dossier = client.get(f'/batches/{code}', headers=headers).json()
    assert all(item['signoff_valid'] is False for item in dossier['runs'])


def test_verdict_overview_lists_reference_and_status(client):
    code = make_batch(client)
    run = run_load(client, code)
    rows = client.get('/verdicts', headers=token(client, 'energy')).json()
    row = next(item for item in rows if item['code'] == code)
    assert row['run_id'] == run['id']
    assert row['low_m3'] == run['result']['interval']['low_m3']
    assert row['high_m3'] == run['result']['interval']['high_m3']
    assert row['verdict'] == run['verdict']
    assert row['verdict_status'] in {'needs_conclusion', 'ready_to_sign'}
