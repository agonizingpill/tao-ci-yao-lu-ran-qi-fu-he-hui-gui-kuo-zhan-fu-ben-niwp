import hashlib
from datetime import datetime

import numpy as np
from sklearn.linear_model import ElasticNetCV
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import KFold, train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

SEED = 1073
RESULT_VERSION = 2
INTERVAL_LEVEL = .90
FEATURES = ['batch_kg', 'ramp_hours', 'soak_hours', 'cool_hours', 'ramp_peak_c', 'soak_peak_c', 'cool_peak_c']
FEATURE_LABELS = {
    'batch_kg': '装载量 kg',
    'ramp_hours': '升温时长 h',
    'soak_hours': '保温时长 h',
    'cool_hours': '冷却时长 h',
    'ramp_peak_c': '升温峰值 °C',
    'soak_peak_c': '保温峰值 °C',
    'cool_peak_c': '冷却峰值 °C',
}


def duration_hours(start, end):
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 3600


def historical_batches():
    rng = np.random.default_rng(SEED)
    mass = rng.uniform(700, 1600, 160)
    ramp = rng.uniform(1.5, 4.5, 160)
    soak = rng.uniform(.6, 2.8, 160)
    cool = rng.uniform(1, 4, 160)
    ramp_peak = rng.uniform(840, 1120, 160)
    soak_peak = ramp_peak + rng.uniform(40, 165, 160)
    cool_peak = soak_peak - rng.uniform(60, 220, 160)
    x = np.column_stack((mass, ramp, soak, cool, ramp_peak, soak_peak, cool_peak))
    target_m3 = (24 + .014 * mass + 11 * ramp + 16 * soak + 3.5 * cool +
                 .035 * ramp_peak + .025 * soak_peak + rng.normal(0, 2.4, len(mass)))
    return x, target_m3


def coverage(features, train_x):
    """Loading / thermal-profile inputs must sit inside the fitted training envelope.

    Both the per-feature training range and the joint (correlation-aware) distance are
    checked, so an unseen loading-and-profile combination is never quietly judged.
    """
    checks = []
    for name, value, low, high in zip(FEATURES, features, train_x.min(axis=0), train_x.max(axis=0)):
        low, high = float(low), float(high)
        inside = bool(low <= float(value) <= high)
        checks.append({'feature': name, 'label': FEATURE_LABELS[name], 'value': round(float(value), 4),
                       'min': round(low, 4), 'max': round(high, 4), 'inside': inside})
    mean, scale = train_x.mean(axis=0), train_x.std(axis=0, ddof=0)
    mean[scale == 0], scale[scale == 0] = 0.0, 1.0
    inverse = np.linalg.pinv(np.cov((train_x - mean) / scale, rowvar=False))
    distances = np.einsum('ij,jk,ik->i', (train_x - mean) / scale, inverse, (train_x - mean) / scale)
    limit = float(np.quantile(distances, .995))
    distance = float(((np.asarray(features, dtype=float) - mean) / scale) @ inverse @
                     ((np.asarray(features, dtype=float) - mean) / scale))
    joint_inside = bool(distance <= limit)
    covered = all(check['inside'] for check in checks) and joint_inside
    return {'covered': covered, 'checks': checks,
            'joint': {'distance': round(distance, 4), 'limit': round(limit, 4), 'inside': joint_inside}}


def residual_interval(prediction, residuals, level=INTERVAL_LEVEL):
    """Calibrate a two-sided interval from holdout residuals that never entered fitting."""
    alpha = (1 - level) / 2
    low_residual = float(np.quantile(residuals, alpha))
    high_residual = float(np.quantile(residuals, 1 - alpha))
    return {'level': level, 'method': 'holdout_residual_quantile',
            'calibration_count': int(len(residuals)),
            'residual_low_m3': round(low_residual, 3), 'residual_high_m3': round(high_residual, 3),
            'low_m3': round(prediction + low_residual, 3), 'high_m3': round(prediction + high_residual, 3)}


def classify(actual, interval, coverage_result):
    if not coverage_result['covered']:
        missing = [check for check in coverage_result['checks'] if not check['inside']]
        if missing:
            reason = '本次装载或温程超出训练样本覆盖范围：' + '、'.join(check['label'] for check in missing)
        else:
            reason = '装载与温程的组合超出训练样本覆盖范围'
        return {'code': 'indeterminate', 'outside_features': [check['feature'] for check in missing],
                'reason': reason}
    low, high = interval['low_m3'], interval['high_m3']
    if low <= actual <= high:
        return {'code': 'within', 'reason': '实测用量落在参考区间内'}
    side = 'above' if actual > high else 'below'
    excess = round(actual - high, 3) if side == 'above' else round(low - actual, 3)
    return {'code': 'outside', 'side': side, 'excess_m3': excess,
            'reason': ('实测高于参考区间上限' if side == 'above' else '实测低于参考区间下限')
                      + f' {excess:g} m³'}


def estimate(snapshot):
    curves, meter = snapshot['curve'], snapshot['meter']
    features = [float(snapshot['cycle']['batch_kg'])]
    features += [duration_hours(c['started_at'], c['ended_at']) for c in curves]
    features += [float(c['peak_c']) for c in curves]
    x, y = historical_batches()
    train_x, test_x, train_y, test_y = train_test_split(x, y, test_size=.25, random_state=SEED)
    cv = KFold(5, shuffle=True, random_state=SEED)
    model = make_pipeline(StandardScaler(), ElasticNetCV(l1_ratio=[.15, .5, .85, 1.0],
                          alphas=np.logspace(-3, 1, 40), cv=cv, max_iter=10000, random_state=SEED))
    model.fit(train_x, train_y)
    fitted = model.named_steps['elasticnetcv']
    penalty = float(model.predict([features])[0])
    actual = round(float(meter[-1]['reading_m3']) - float(meter[0]['reading_m3']), 3)
    interval = residual_interval(penalty, test_y - model.predict(test_x))
    envelope = coverage(features, train_x)
    verdict = classify(actual, interval, envelope)
    return {'expected_m3': round(penalty, 3), 'actual_m3': actual, 'delta_m3': round(actual - round(penalty, 3), 3),
            'mae_m3': round(float(mean_absolute_error(test_y, model.predict(test_x))), 3),
            'seed': SEED, 'train_count': len(train_y), 'holdout_count': len(test_y),
            'cv_folds': cv.n_splits, 'alpha': float(fitted.alpha_), 'l1_ratio': float(fitted.l1_ratio_),
            'training_sha256': hashlib.sha256(np.column_stack((x, y)).astype('<f8').tobytes()).hexdigest(),
            'features': dict(zip(FEATURES, features)), 'coefs_scaled': dict(zip(FEATURES, map(float, fitted.coef_))),
            'result_version': RESULT_VERSION, 'interval': interval, 'coverage': envelope,
            'verdict': verdict['code'], 'verdict_detail': verdict}
