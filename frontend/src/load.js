import {state, verdictLabel} from './state.js';
import {amount, badge, clock, empty, esc, field, table, ticket} from './ui.js';
import {readingTrace} from './firing.js';

const showLoad = (result) => {
  const interval = result.interval;
  const span = Math.max(result.actual_m3, interval?.high_m3 ?? 0, interval?.low_m3 ?? 0, result.expected_m3, 1) * 1.12;
  const band = interval
    ? `<div class="gas-row reference"><span>参考区间 ${Math.round(interval.level * 100)}%</span>
        <i class="band" style="margin-left:${100 * Math.max(0, interval.low_m3) / span}%;width:${100 * Math.max(0, interval.high_m3 - interval.low_m3) / span}%"></i>
        <b>${amount(interval.low_m3)}–${amount(interval.high_m3)}</b></div>`
    : '';
  return `<figure class="gas-comparison"><figcaption>本周期燃气量 <span>m³</span></figcaption>
    <div class="gas-row"><span>模型参考</span><i style="width:${100 * Math.max(0, result.expected_m3) / span}%"></i><b>${amount(result.expected_m3)}</b></div>
    ${band}
    <div class="gas-row actual"><span>表计实测</span><i style="width:${100 * Math.max(0, result.actual_m3) / span}%"></i><b>${amount(result.actual_m3)}</b></div>
    <div class="delta"><span>实测 − 参考</span><b>${result.delta_m3 > 0 ? '+' : ''}${amount(result.delta_m3)} m³</b></div></figure>`;
};

const verdictTone = {within: 'approved', outside: 'failed', indeterminate: 'pending'};
const verdictStatusLabel = {
  within: '区间内，照常签核',
  outside: '超出区间，先登记核查结论',
  indeterminate: '训练未覆盖，标为不可判',
};
const pickStatus = {
  needs_conclusion: '待核查', needs_rerun: '待重新试算', stale: '判定失效', ready_to_sign: '待签核',
  signed: '已签核', no_ready_run: '未试算',
};

function checkQueue() {
  const actionable = new Set(['needs_conclusion', 'needs_rerun', 'ready_to_sign', 'stale']);
  const rows = (state.verdicts || []).filter(row => actionable.has(row.verdict_status));
  if (!rows.length) return '';
  return `<section class="board queue"><div class="section-title"><h3>待核查窑次</h3>
      <span class="muted">${rows.length} 个窑次需跟进</span></div>
    <div class="queue-list">${rows.map(row => `<button type="button" class="queue-item ${row.code === state.code ? 'selected' : ''}"
      data-batch="${esc(row.code)}">
      <b>${esc(row.code)}</b><span>${esc(pickStatus[row.verdict_status] || row.verdict_status_label)}</span>
      <small>${row.actual_m3 == null ? '暂无实测' : `实测 ${amount(row.actual_m3)} m³`}${row.low_m3 == null ? '' : ` · 区间 ${amount(row.low_m3)}–${amount(row.high_m3)}`}</small>
    </button>`).join('')}</div></section>`;
}

function basisRows(result, verdict) {
  const detail = result.verdict_detail || {};
  const rows = [];
  if (detail.reason) rows.push(['判定依据', `<b>${esc(detail.reason)}</b>`]);
  if (verdict) rows.push(['处理口径', verdictStatusLabel[verdict] || '待判定']);
  if (result.interval) {
    const i = result.interval;
    rows.push(['参考区间', `${amount(i.low_m3)} – ${amount(i.high_m3)} m³（${Math.round(i.level * 100)}%，留出 ${i.calibration_count} 条残差分位）`]);
    rows.push(['校准残差', `${amount(i.residual_low_m3)} – ${amount(i.residual_high_m3)} m³`]);
  }
  if (result.coverage) {
    rows.push(['训练覆盖', result.coverage.covered ? '装载与温程均在训练范围内' : '存在训练未覆盖的装载 / 温程组合']);
  }
  if (result.coverage?.joint) {
    const j = result.coverage.joint;
    rows.push(['组合距离', `${amount(j.distance)} / 上限 ${amount(j.limit)}（${j.inside ? '组合在训练范围内' : '组合越界'}）`]);
  }
  return rows;
}

function coverageTable(result) {
  if (!result.coverage) return '';
  const joint = result.coverage.joint
    ? `<p class="muted">装载与温程组合距离 ${amount(result.coverage.joint.distance)}，训练上限 ${amount(result.coverage.joint.limit)}：${result.coverage.joint.inside ? '组合在训练范围内' : '组合越界，标为不可判'}。</p>`
    : '';
  return `<details class="edit"><summary>训练覆盖核对</summary>${table(['输入','本次值','训练下限','训练上限','覆盖'],
    result.coverage.checks.map(check => [esc(check.label), amount(check.value), amount(check.min), amount(check.max),
      check.inside ? '覆盖' : '<b>超出</b>']))}${joint}</details>`;
}

function verdictDesk(batch, run, role, verdict, conclusion, signoff, stale) {
  if (!run || run.status !== 'ready') return '';
  if (!run.result.interval) {
    return `<section class="board verdict"><div class="section-title"><h3>参考区间判定</h3>${badge('pending', '待重新试算')}</div>
      <p class="muted">试算 #${run.id} 生成于参考区间启用前，请重新提交试算后再判定。</p></section>`;
  }
  const outside = verdict === 'outside';
  const canRegister = role === 'operator' && outside && !signoff && !stale;
  const canSign = role === 'supervisor' && !signoff && !stale && (!outside || conclusion);
  const basis = basisRows(run.result, verdict);
  return `<section class="board verdict"><div class="section-title">
      <h3>参考区间判定</h3>${badge(verdictTone[verdict] || 'pending', verdictLabel[verdict] || '待判定')}</div>
    ${stale ? `<p class="warn">源数据已更正，试算 #${run.id} 的判定失效，请重新试算后再签核。</p>` : ''}
    ${table(['口径','依据'], basis.map(row => [row[0], row[1]]))}
    ${coverageTable(run.result)}
    ${conclusion ? `<div class="conclusion"><h4>核查结论</h4><p>${esc(conclusion.note)}</p>
      <small>登记 ${esc(conclusion.registered_by)} · ${esc(conclusion.registered_at)}</small></div>`
      : outside && !signoff ? `<p class="muted">超出参考区间，须先由班长登记核查结论后方可签核。</p>` : ''}
    ${canRegister ? `<form data-action="runs/${run.id}/conclusion" class="conclusion-form">
      ${field('核查结论','note','minlength="4" maxlength="400"')}<button>登记核查结论</button></form>` : ''}
    ${signoff ? `<div class="signoff"><h4>${stale ? '原判定留档（已失效）' : '判定已签核'}</h4><p>${verdictLabel[signoff.verdict] || esc(signoff.verdict)} · 参考区间 ${amount(signoff.reference_low_m3)}–${amount(signoff.reference_high_m3)} m³</p>
      <small>签核 ${esc(signoff.signed_by)} · ${esc(signoff.signed_at)}${signoff.note ? ` · ${esc(signoff.note)}` : ''}</small></div>`
      : canSign ? `<form data-action="runs/${run.id}/signoff" class="signoff-form">
        <label>签核说明（可选）<input name="note" maxlength="400" placeholder="如无补充可留空"></label><button>签核判定</button></form>` : ''}
  </section>`;
}

export function load(batch) {
  if (!batch) return `${checkQueue()}${empty('先在烧成批次登记窑次。')}`;
  const run = batch.runs[0];
  const role = state.session.user.role;
  let body;
  if (run?.status === 'ready') {
    const r = run.result;
    body = `<div class="analysis-grid">${showLoad(r)}<section class="model"><h4>弹性网络校验</h4><dl>
      <dt>留出集 MAE</dt><dd>${amount(r.mae_m3)} m³</dd><dt>训练 / 留出</dt><dd>${r.train_count} / ${r.holdout_count}</dd>
      <dt>交叉验证</dt><dd>${r.cv_folds} 折</dd><dt>α / L1 比例</dt><dd>${r.alpha.toFixed(5)} / ${r.l1_ratio}</dd>
      <dt>随机种子</dt><dd>${r.seed}</dd></dl><p>MAE 来自合成历史批次。参考区间由未参与拟合的留出样本校准，配方版本与节拍签核在周期复核中登记。</p></section></div>
      <details class="edit"><summary>模型特征与缩放后系数</summary>${table(['特征','本次输入','缩放后系数'],
        Object.entries(r.features).map(([key,value]) => [esc(key), amount(value), amount(r.coefs_scaled[key])]))}
        <p class="digest">训练集 SHA-256 <code>${esc(r.training_sha256)}</code></p></details>
      ${verdictDesk(batch, run, role, run.verdict || r.verdict, run.conclusion, run.signoff, run.source_stale)}
      ${run.source_stale && role === 'operator' ? '<form data-action="runs"><button>按更正后的读数重新试算</button></form>' : ''}
      ${readingTrace(run)}`;
  } else if (run?.status === 'queued') {
    body = `<p class="empty">试算 #${run.id} 已排队，页面会更新结果。</p><button type="button" data-refresh class="secondary">重新读取试算状态</button>`;
  } else if (run?.status === 'failed') {
    body = `<p class="empty">试算 #${run.id} 失败（${esc(run.error)}）。主管可按原快照重试。</p>${role === 'supervisor' ? `<form data-action="runs/${run.id}/retry"><button>按原快照重试</button></form>` : ''}${readingTrace(run)}`;
  } else {
    body = `<div class="coverage"><div><b>温区</b><span>${batch.curves.length} / 3 阶段</span><p>升温、保温、冷却必须连续覆盖烧成周期。</p></div>
      <div><b>累计表</b><span>${batch.meters.length} 条读数</span><p>进窑、出窑必须有记录，间隔不得超过三小时。</p></div></div>
      <p class="muted">待校验周期 ${clock(batch.opened_at)}–${clock(batch.closed_at)}。提交后冻结原始台账。</p>
      ${role === 'operator' ? '<form data-action="runs"><button>校验并提交试算</button></form>' : ''}`;
  }
  const staleBanner = batch.runs.some(item => item.source_stale)
    ? '<p class="warn">抄表源数据已更正，历史判定不再有效，请重新提交试算。</p>' : '';
  return `${checkQueue()}${ticket(batch)}<section class="board"><div class="section-title"><h3>周期负荷参考</h3>${run ? badge(run.status, {ready:'试算完成',failed:'试算失败',queued:'队列等待'}[run.status]) : ''}</div>${staleBanner}${body}</section>`;
}
