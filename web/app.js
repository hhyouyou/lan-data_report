/* Lan Data Report · 前端骨架
 * 无框架、无构建步骤。图表由 charts.js 渲染：
 *   优先使用本地 ECharts（web/vendor/echarts.min.js），缺失时自动退回手写 SVG。
 */

const state = {
  meta: null,
  sessionId: null,
  currentQuery: null,
  dashboards: [],
  shareToken: null,
  descDoc: null,
  descSource: null,
};

const $ = (s) => document.querySelector(s);

/* ------------------------------------------------------------------ 基础 */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
  if (!res.ok) {
    const d = data.detail;
    const msg = typeof d === 'string' ? d
      : (d && d.message) ? d.message
        : (d ? JSON.stringify(d) : `${res.status} ${res.statusText}`);
    const err = new Error(msg);
    err.payload = d;
    throw err;
  }
  return data;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function fmtNum(v, format) {
  if (v === null || v === undefined || v === '') return '-';
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  if (format === 'percent') return (n * 100).toFixed(2) + '%';
  if (format === 'currency') return '¥' + n.toLocaleString('zh-CN', { maximumFractionDigits: 0 });
  if (format === 'integer') return n.toLocaleString('zh-CN', { maximumFractionDigits: 0 });
  if (format === 'number') return n.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
  return Number.isInteger(n) ? n.toLocaleString('zh-CN')
                             : n.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
}

function toast(msg, ms = 2400) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

function niceMax(v) {
  if (!isFinite(v) || v <= 0) return 1;
  const exp = Math.floor(Math.log10(v));
  const base = Math.pow(10, exp);
  const n = v / base;
  let m = 10;
  if (n <= 1) m = 1;
  else if (n <= 2) m = 2;
  else if (n <= 2.5) m = 2.5;
  else if (n <= 5) m = 5;
  return m * base;
}

/* ------------------------------------------------------------------ 问数 */
async function doAsk() {
  const input = $('#question');
  const q = input.value.trim();
  if (!q) return;
  const btn = $('#askBtn');
  btn.disabled = true;
  $('#answer').innerHTML = '<div class="panel empty">正在识别指标、生成查询…</div>';
  try {
    const data = await api('/api/ask', {
      method: 'POST',
      body: JSON.stringify({ question: q, session_id: state.sessionId }),
    });
    if (data.type === 'clarification') {
      renderClarification(data);
    } else if (data.type === 'refuse') {
      $('#answer').innerHTML = `<div class="panel notes">${esc(data.reason)}</div>`;
    } else {
      state.sessionId = data.session_id;
      state.currentQuery = data;
      renderAnswer(data, q);
    }
  } catch (e) {
    $('#answer').innerHTML = `<div class="panel notes">出错：${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

function renderClarification(data) {
  const opts = (data.options || []).map((o) =>
    `<button class="chip" data-q="${esc(o.label)}">${esc(o.label)}</button>`).join('');
  $('#answer').innerHTML = `<div class="panel"><div>${esc(data.question)}</div>
      <div class="chips">${opts}</div></div>`;
  document.querySelectorAll('#answer .chip').forEach((b) => {
    b.onclick = () => { $('#question').value = b.dataset.q; doAsk(); };
  });
}

function explainHTML(ex) {
  const items = (ex.metrics || []).map((m) =>
    `<li><b>${esc(m.title)}</b>：<code>${esc(m.definition)}</code></li>`).join('');
  return `<ul class="explain-list">${items}
      <li>语义模型：${esc(ex.semantic_model)}</li></ul>
    <pre class="sql">${esc(ex.sql)}</pre>`;
}

function renderAnswer(data, question) {
  const cols = data.columns;
  const rows = data.result.rows;
  const kind = data.visualization.kind;
  const period = periodLabel(data);
  const notes = (data.notes || []).length
    ? `<div class="panel notes">${data.notes.map((n) => `<div>· ${esc(n)}</div>`).join('')}</div>`
    : '';

  $('#answer').innerHTML = `${notes}
    <div class="panel">
      <div class="result-head">
        <h3>${esc(question)}</h3>
        <span class="meta">${period ? `${esc(period)} · ` : ''}${rows.length} 行 · ${data.latency_ms}ms${data.cached ? ' · 缓存命中' : ''}</span>
        <span class="spacer"></span>
        <button class="ghost" id="explainBtn">📖 口径与血缘</button>
        <button class="ghost" id="pinBtn">📌 固定到看板</button>
      </div>
      <div class="chart" id="answerChart"></div>
      <div id="explainPanel" class="exp-panel hidden"></div>
      <details class="sqls" style="margin-top:14px">
        <summary>查看口径与 SQL</summary>
        ${explainHTML(data.explain)}
        ${kind === 'table' ? '' : `<div class="table-wrap" style="margin-top:10px">${tableHTML(cols, rows)}</div>`}
      </details>
    </div>`;

  const pinBtn = $('#pinBtn');
  if (pinBtn) pinBtn.onclick = () => openPin(data);

  if (typeof disposeDetachedCharts === 'function') disposeDetachedCharts();
  drawChart($('#answerChart'), kind, cols, rows);

  const expBtn = $('#explainBtn');
  if (expBtn) expBtn.onclick = async () => {
    const panel = $('#explainPanel');
    if (!panel.classList.contains('hidden')) { panel.classList.add('hidden'); return; }
    panel.classList.remove('hidden');
    if (panel.dataset.loaded) return;
    panel.textContent = '加载中…';
    try {
      const ex = await api(`/api/queries/${data.query_id}/explain`);
      panel.innerHTML = renderExplain(ex);
      panel.dataset.loaded = '1';
    } catch (e) {
      panel.innerHTML = `<div class="notes">加载失败：${esc(e.message)}</div>`;
    }
  };
}

function renderExplain(ex) {
  const blocks = (ex.metrics || []).map((m) => {
    const b = m.business || {};
    const L = m.lineage || {};
    const path = (L.path || []).map((p) => `<span class="step">${esc(p)}</span>`)
      .join('<span class="arrow">→</span>');
    const interp = (b.interpretation || []).map((x) => `<li>${esc(x)}</li>`).join('');
    const hist = (b.history || []).map((h) =>
      `<li>${esc(h.date)} · ${esc(h.change)} <span class="muted">（${esc(h.by || '')}）</span></li>`).join('');
    const faq = (b.faq || []).map((f) =>
      `<li><b>${esc(f.q)}</b><div>${esc(f.a)}</div></li>`).join('');

    return `<div class="exp-block">
      <div class="exp-title">${esc(m.title)} <span class="badge">${esc(m.type)}</span></div>
      <div class="kv"><span>计算口径</span><code>${esc(m.definition.definition)}</code></div>
      ${L.path ? `<div class="kv"><span>血缘</span><span class="lineage">${path}</span></div>` : ''}
      ${(L.filters || []).length ? `<div class="kv"><span>过滤条件</span><code>${esc(L.filters.join(' AND '))}</code></div>` : ''}
      ${b.business_meaning ? `<div class="kv"><span>业务含义</span><span>${esc(b.business_meaning)}</span></div>` : ''}
      ${b.owner ? `<div class="kv"><span>负责人</span><span>${esc(b.owner)} · 更新于 ${esc(b.updated_at || '')}</span></div>` : ''}
      ${(m.dependents || []).length ? `<div class="kv"><span>影响面</span><span>${m.dependents.map(esc).join('、')}</span></div>` : ''}
      ${interp ? `<div class="exp-sub">解读提示</div><ul class="exp-list">${interp}</ul>` : ''}
      ${hist ? `<div class="exp-sub">变更记录</div><ul class="exp-list">${hist}</ul>` : ''}
      ${faq ? `<div class="exp-sub">常见问题</div><ul class="exp-faq">${faq}</ul>` : ''}
    </div>`;
  }).join('');

  const dims = (ex.dimensions || []).map((d) => `
    <div class="exp-block">
      <div class="exp-title">维度 · ${esc(d.title)}</div>
      <div class="kv"><span>字段</span><code>${esc((d.table || '') + '.' + (d.field || ''))}</code></div>
      ${d.business && d.business.business_meaning ? `<div class="kv"><span>业务含义</span><span>${esc(d.business.business_meaning)}</span></div>` : ''}
      ${d.business && d.business.caveat ? `<div class="kv"><span>注意</span><span>${esc(d.business.caveat)}</span></div>` : ''}
    </div>`).join('');

  return `<div class="exp-head">口径与血缘 <span class="muted">${esc(ex.semantic_model)}</span></div>
    ${blocks}${dims}
    ${ex.sql ? `<details class="sqls"><summary>执行 SQL</summary><pre class="sql">${esc(ex.sql)}</pre></details>` : ''}`;
}

/* ------------------------------------------------------------------ 固定 */
function autoTitle(spec) {
  const m = (name) => {
    const hit = state.meta && state.meta.metrics.find((x) => x.name === name);
    return hit ? hit.title : name;
  };
  const d = (name) => {
    const hit = state.meta && state.meta.dimensions.find((x) => x.name === name);
    return hit ? hit.title : name;
  };
  let t = (spec.metrics || []).map(m).join('、') || '查询结果';
  if (spec.dimensions && spec.dimensions.length) {
    t += ' 按 ' + spec.dimensions.map(d).join('、');
  }
  return t;
}

async function openPin(data) {
  const list = await api('/api/dashboards');
  const sel = $('#pinDashboard');
  sel.innerHTML = '<option value="">+ 新建看板</option>' +
    list.items.map((d) => `<option value="${d.id}">${esc(d.title)}</option>`).join('');
  $('#pinTitle').value = autoTitle(data.spec);
  $('#modal').classList.remove('hidden');
  $('#pinConfirm').onclick = async () => {
    try {
      const res = await api(`/api/queries/${data.query_id}/pin`, {
        method: 'POST',
        body: JSON.stringify({
          dashboard_id: sel.value || null,
          title: $('#pinTitle').value.trim() || null,
        }),
      });
      $('#modal').classList.add('hidden');
      toast('已固定到看板');
      switchView('dashboards');
      openDashboard(res.dashboard.id);
    } catch (e) {
      toast('固定失败：' + e.message);
    }
  };
  $('#pinCancel').onclick = () => $('#modal').classList.add('hidden');
}

/* ------------------------------------------------------------------ 看板 */
async function loadDashboards() {
  try {
    const list = await api('/api/dashboards');
    state.dashboards = list.items;
    if (!state.shareToken) renderDashList(list.items);
  } catch (e) {
    $('#dashList').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

function renderDashList(items) {
  $('#dashDetail').classList.add('hidden');
  $('#dashList').classList.remove('hidden');
  if (!items.length) {
    $('#dashList').innerHTML = '<div class="empty">还没有看板。去「问数」页面查一个结果，点「📌 固定到看板」。</div>';
    return;
  }
  $('#dashList').innerHTML = items.map((d) => `
    <div class="dash-item" data-id="${d.id}">
      <h3>${esc(d.title)}<span class="badge ${d.status === 'published' ? 'published' : ''}">${d.status === 'published' ? '已发布' : '草稿'}</span></h3>
      ${d.description ? `<p class="desc">${esc(d.description)}</p>` : ''}
      <div class="sub">v${d.version} · ${esc((d.updated_at || '').slice(0, 16).replace('T', ' '))}</div>
    </div>`).join('');
  document.querySelectorAll('.dash-item').forEach((el) => {
    el.onclick = () => openDashboard(el.dataset.id);
  });
}

async function openDashboard(id) {
  try {
    const [doc, data, wf] = await Promise.all([
      api('/api/dashboards/' + id),
      api('/api/dashboards/' + id + '/data'),
      api('/api/dashboards/' + id + '/workflow').catch(() => ({ artifact: null })),
    ]);
    renderDashboard(doc, data, false, wf.artifact);
  } catch (e) {
    toast('打开看板失败：' + e.message);
  }
}

const STATUS_LABEL = {
  draft: '草稿', submitted: '审批中', approved: '已通过',
  rejected: '已驳回', published: '已发布', archived: '已归档',
};

/* 卡片实际生效的时间范围：优先后端返回，其次从时间维度的首末值推断 */
function periodLabel(d) {
  if (d && d.period && d.period.label) return d.period.label;
  const cols = (d && d.columns) || [];
  const rows = (d && d.result && d.result.rows) || [];
  if (cols.length && cols[0].type === 'time' && rows.length) {
    const first = String(rows[0][0] || '').slice(0, 4);
    const last = String(rows[rows.length - 1][0] || '').slice(0, 4);
    if (first && last) return first === last ? `${first}年` : `${first}–${last}年`;
  }
  return '';
}

function renderDashboard(doc, data, readonly, artifact) {
  state.currentDash = { doc, data, readonly, artifact };
  $('#dashList').classList.add('hidden');
  $('#dashDetail').classList.remove('hidden');

  const cards = (doc.cards || []).map((card, idx) => {
    const d = data[card.id] || {};
    const L = card.layout || { x: 0, y: 0, w: 6, h: 4 };
    const style = `grid-column:${(L.x || 0) + 1} / span ${L.w || 6};grid-row:${(L.y || 0) + 1} / span ${L.h || 4};`;
    const kind = d.visualization ? d.visualization.kind
      : ((card.visualization && card.visualization.kind) || 'table');
    const body = d.error
      ? `<div class="err">${esc(d.error)}</div>`
      : chartSlot(kind, d.columns || [], (d.result || {}).rows || [], {
          drill: (card.visualization || {}).drill_metric,
          drill_metrics: (card.visualization || {}).drill_metrics,
          labels: (card.visualization || {}).metric_labels,
          ref_line: (card.visualization || {}).ref_line,
          model: doc.model,
        });
    const period = periodLabel(d);
    return `<div class="dash-card" data-idx="${idx}" style="${style}">
        <div class="dash-card-head">
          <span class="dash-card-title">${esc(card.title)}</span>
          ${period ? `<span class="dash-card-period">${esc(period)}</span>` : ''}
          <span class="dash-card-tools">
            <button class="icon-btn" data-info="${idx}" title="指标口径">ⓘ</button>
            <button class="icon-btn" data-detail="${idx}" title="查看数据与 SQL">⤢</button>
          </span>
        </div>
        <div class="dash-card-body">${body}</div>
      </div>`;
  }).join('');

  const status = (artifact && artifact.status) || doc.status || 'draft';
  const label = STATUS_LABEL[status] || status;
  const updated = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  const descDoc = doc.description_meta || {};
  const intro = String(doc.description || '').trim();
  const introChips = [];
  if (descDoc.metrics) introChips.push(`${descDoc.metrics} 项指标`);
  if (descDoc.scope) introChips.push(descDoc.scope);
  if (descDoc.span) introChips.push(descDoc.span);
  if (descDoc.cards) introChips.push(`${descDoc.cards} 张卡片`);
  const introSource = descDoc.source === 'llm' ? '✨ AI 润色'
    : descDoc.source === 'manual' ? '已手工调整'
      : descDoc.generated_at ? '自动生成' : '';

  const actions = [];
  if (!readonly) {
    if (status === 'draft' || status === 'rejected') actions.push(['提交审批', 'submit', 'primary']);
    if (status === 'submitted') actions.push(['通过', 'approve', 'primary'], ['驳回', 'reject', 'ghost']);
    if (status === 'approved') actions.push(['发布', 'publish', 'primary']);
    if (status === 'published') actions.push(['分享', 'share', 'ghost']);
  }
  const actionBtns = actions.map(([t, a, c]) =>
    `<button class="${c}" data-act="${a}">${t}</button>`).join('');

  $('#dashDetail').innerHTML = `
    <div class="dash-head">
      ${readonly ? '' : '<button class="ghost" id="backDash">← 返回</button>'}
      <h2>${esc(doc.title)}</h2>
      <span class="badge ${status}">${label} · v${doc.version}</span>
      ${doc.model ? `<span class="meta">模型 ${esc(doc.model)}</span>` : ''}
      <span class="meta">${updated} 更新</span>
      <span class="spacer"></span>
      ${readonly ? '' : '<button class="ghost" id="dashRefresh">刷新</button>'}
      ${readonly ? '' : '<button class="ghost" id="dashGenDesc" title="根据看板卡片自动生成简介">✨ 生成简介</button>'}
      ${actionBtns}
    </div>
    ${intro ? `<div class="dash-intro">
      <div class="dash-intro-aside">
        <span class="dash-intro-tag">看板简介</span>
        ${introSource ? `<span class="dash-intro-source">${esc(introSource)}</span>` : ''}
      </div>
      <div class="dash-intro-main">
        <p class="dash-intro-text">${esc(intro)}</p>
        ${introChips.length ? `<div class="dash-intro-chips">${introChips.map((c) => `<span class="intro-chip">${esc(c)}</span>`).join('')}</div>` : ''}
      </div>
    </div>` : ''}
    <div class="dash-grid">${cards || '<div class="empty">看板还没有卡片</div>'}</div>`;

  hydrateCharts($('#dashDetail'));

  const back = $('#backDash');
  if (back) back.onclick = () => loadDashboards();
  const refreshBtn = $('#dashRefresh');
  if (refreshBtn) refreshBtn.onclick = refreshDashboard;
  const genDesc = $('#dashGenDesc');
  if (genDesc) genDesc.onclick = () => openDescribeModal(doc);
  document.querySelectorAll('#dashDetail [data-act]').forEach((btn) => {
    btn.onclick = () => runWorkflowAction(doc, artifact, btn.dataset.act);
  });
  document.querySelectorAll('#dashDetail [data-info]').forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      openMetricInfo(doc.cards[Number(btn.dataset.info)]);
    };
  });
  document.querySelectorAll('#dashDetail [data-detail]').forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      const card = doc.cards[Number(btn.dataset.detail)];
      openCardDetail(card, data[card.id] || {});
    };
  });
}

async function refreshDashboard() {
  const cur = state.currentDash;
  if (!cur) return;
  const btn = $('#dashRefresh');
  if (btn) { btn.disabled = true; btn.textContent = '刷新中…'; }
  try {
    const data = await api('/api/dashboards/' + cur.doc.id + '/data');
    renderDashboard(cur.doc, data, cur.readonly, cur.artifact);
    toast('已刷新');
  } catch (e) {
    toast('刷新失败：' + e.message);
    if (btn) { btn.disabled = false; btn.textContent = '刷新'; }
  }
}

/* ------------------------------------------------------------------ 看板简介：根据卡片自动生成 */
async function openDescribeModal(doc) {
  state.descDoc = doc;
  state.descSource = null;
  $('#descModal').classList.remove('hidden');
  $('#descText').value = '';
  await regenDescribe();
}

async function regenDescribe() {
  const doc = state.descDoc;
  if (!doc) return;
  const hint = $('#descHint');
  hint.textContent = '正在根据卡片生成…';
  $('#descText').value = '';
  try {
    const res = await api('/api/dashboards/' + doc.id + '/describe', {
      method: 'POST', body: JSON.stringify({ apply: false }),
    });
    state.descSource = res.source || null;
    $('#descText').value = res.text || '';
    if (res.source === 'llm') {
      hint.textContent = `已用 AI 润色（读取 ${res.cards} 张卡片），可修改后应用`;
    } else if (!res.llm_available) {
      hint.textContent = `已读取 ${res.cards} 张卡片生成；未配置 LLM，使用规则摘要`;
    } else {
      hint.textContent = `已读取 ${res.cards} 张卡片生成，可修改后应用`;
    }
  } catch (e) {
    hint.textContent = '生成失败：' + e.message;
  }
}

async function applyDescribe() {
  const doc = state.descDoc;
  if (!doc) return;
  const text = $('#descText').value.trim();
  if (!text) { toast('简介不能为空'); return; }
  try {
    await api('/api/dashboards/' + doc.id + '/describe', {
      method: 'POST',
      body: JSON.stringify({ apply: true, text, source: state.descSource || 'manual' }),
    });
    $('#descModal').classList.add('hidden');
    toast('简介已更新，提交审批后可重新发布', 3200);
    await openDashboard(doc.id);
  } catch (e) {
    toast('应用失败：' + e.message, 4000);
  }
}

/* ------------------------------------------------------------------ 看板卡片交互 */
function showInfoModal(title, body) {
  $('#infoTitle').textContent = title;
  $('#infoBody').innerHTML = body;
  $('#infoModal').classList.remove('hidden');
}

async function openMetricInfo(card) {
  const metric = card && card.spec && card.spec.metrics && card.spec.metrics[0];
  if (!metric) { toast('该卡片没有可用指标'); return; }
  try {
    const info = await api('/api/metrics/' + encodeURIComponent(metric));
    const L = info.lineage || {};
    const b = info.business || {};
    const parts = [
      `<div class="kv"><span>指标</span><code>${esc(metric)}</code></div>`,
      `<div class="kv"><span>口径</span><code>${esc(info.definition ? info.definition.definition : '')}</code></div>`,
      `<div class="kv"><span>类型</span><span>${esc(info.type || '')}</span></div>`,
    ];
    if (L.table) parts.push(`<div class="kv"><span>来源表</span><code>${esc(L.table)}${L.field ? '.' + esc(L.field) : ''}</code></div>`);
    if ((L.depends_on || []).length) parts.push(`<div class="kv"><span>依赖指标</span><span>${L.depends_on.map(esc).join('、')}</span></div>`);
    if (b.business_meaning) parts.push(`<div class="kv"><span>业务含义</span><span>${esc(b.business_meaning)}</span></div>`);
    if ((b.interpretation || []).length) {
      parts.push('<div class="exp-sub">解读提示</div><ul class="exp-list">'
        + b.interpretation.map((x) => `<li>${esc(x)}</li>`).join('') + '</ul>');
    }
    parts.push(`<div class="kv"><span>语义模型</span><span>${esc(info.model || '')}</span></div>`);
    showInfoModal(info.title || metric, parts.join(''));
  } catch (e) {
    toast('获取口径失败：' + e.message);
  }
}

async function openCardDetail(card, d) {
  const cols = d.columns || [];
  const rows = (d.result || {}).rows || [];
  let extra = '';
  if (card.query) {
    try {
      const ex = await api('/api/queries/' + card.query + '/explain');
      extra = `<div class="exp-sub">口径与 SQL</div>${renderExplain(ex)}`;
    } catch (e) { /* 忽略 */ }
  }
  showInfoModal(card.title || '卡片数据', `
    <div class="exp-sub" style="margin-top:0">数据（${rows.length} 行）</div>
    <div class="table-wrap" style="max-height:340px">${tableHTML(cols, rows)}</div>
    ${extra}`);
}

async function runWorkflowAction(doc, artifact, action) {
  try {
    if (action === 'submit') {
      await api(`/api/dashboards/${doc.id}/submit`, { method: 'POST' });
      toast('已提交审批');
    } else if (action === 'share') {
      const res = await api(`/api/dashboards/${doc.id}/share`, { method: 'POST' });
      const url = location.origin + res.url;
      await navigator.clipboard.writeText(url).catch(() => {});
      toast('分享链接已复制：' + url, 3600);
      return;
    } else {
      const comment = action === 'reject' ? (prompt('驳回原因', '') || '') : '';
      await api(`/api/artifacts/${artifact.id}/${action}`, {
        method: 'POST', body: JSON.stringify({ comment }),
      });
      toast(action === 'approve' ? '已通过' : action === 'reject' ? '已驳回' : '已发布');
    }
    openDashboard(doc.id);
  } catch (e) {
    if (e.payload && e.payload.lint) showLint($('#dashDetail'), e.payload.lint);
    toast(e.message, 4200);
  }
}

function showLint(container, report) {
  const box = document.createElement('div');
  box.className = 'lint-box';
  const errs = (report.errors || []).map((x) =>
    `<li><code>${esc(x.rule)}</code> ${esc(x.message)}</li>`).join('');
  const warns = (report.warnings || []).map((x) =>
    `<li class="warn"><code>${esc(x.rule)}</code> ${esc(x.message)}</li>`).join('');
  box.innerHTML = `<b>规范校验未通过</b>${errs ? `<ul>${errs}</ul>` : ''}
    ${warns ? `<div class="lint-sub">提示（不影响提交）</div><ul>${warns}</ul>` : ''}`;
  container.prepend(box);
}

async function openShared(token) {
  try {
    const doc = await api('/api/share/' + token);
    const data = {};
    for (const card of doc.cards || []) {
      try {
        data[card.id] = await api('/api/run-spec', {
          method: 'POST', body: JSON.stringify({ spec: card.spec, model: doc.model }),
        });
      } catch (e) {
        data[card.id] = { error: e.message };
      }
    }
    renderDashboard(doc, data, true);
  } catch (e) {
    $('#dashDetail').classList.remove('hidden');
    $('#dashDetail').innerHTML = `<div class="empty">分享链接无效：${esc(e.message)}</div>`;
  }
}

/* ------------------------------------------------------------------ 数据 */
async function loadTables() {
  try {
    const { items } = await api('/api/tables');
    $('#tableMeta').textContent = `${items.length} 张表`;
    $('#tableDetail').classList.add('hidden');
    $('#tableList').classList.remove('hidden');
    if (!items.length) {
      $('#tableList').innerHTML =
        '<div class="empty">还没有数据。点右上角「导入 Excel / CSV」。</div>';
      return;
    }
    $('#tableList').innerHTML = items.map((t) => `
      <div class="dash-item" data-table="${esc(t.name)}">
        <h3>${esc(t.name)}</h3>
        <div class="sub">${t.rows.toLocaleString()} 行 · ${t.columns} 列</div>
      </div>`).join('');
    document.querySelectorAll('#tableList .dash-item').forEach((el) => {
      el.onclick = () => openTable(el.dataset.table);
    });
  } catch (e) {
    $('#tableList').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

async function openTable(name) {
  try {
    const d = await api('/api/tables/' + encodeURIComponent(name));
    $('#tableList').classList.add('hidden');
    const box = $('#tableDetail');
    box.classList.remove('hidden');

    const cols = d.columns.map((c) => `<tr>
        <td>${esc(c.name)}</td>
        <td>${esc(c.source || c.name)}</td>
        <td>${esc(c.kind || '-')}</td>
        <td>${esc(c.type)}</td>
        <td>${c.nulls || 0}</td>
      </tr>`).join('');

    box.innerHTML = `
      <div class="dash-head">
        <button class="ghost" id="backTable">← 返回</button>
        <h2>${esc(d.name)}</h2>
        <span class="badge">${d.rows.toLocaleString()} 行 · ${d.columns.length} 列</span>
        <span class="spacer"></span>
        <button class="primary" id="draftBtn">🤖 生成语义模型草稿</button>
      </div>
      <div class="panel">
        <div class="exp-sub" style="margin-top:0">字段</div>
        <div class="table-wrap">
          <table class="grid">
            <thead><tr><th>字段</th><th>原始表头</th><th>语义类型</th><th>SQL 类型</th><th>空值</th></tr></thead>
            <tbody>${cols}</tbody>
          </table>
        </div>
      </div>
      <div class="panel" style="margin-top:14px">
        <div class="exp-sub" style="margin-top:0">样例数据（前 ${d.sample.rows.length} 行）</div>
        <div class="table-wrap">${tableHTML(
          d.sample.columns.map((c) => ({ name: c, label: c })), d.sample.rows)}</div>
      </div>
      <div id="draftPanel" class="panel hidden" style="margin-top:14px"></div>`;

    $('#backTable').onclick = () => {
      $('#tableDetail').classList.add('hidden');
      $('#tableList').classList.remove('hidden');
    };
    $('#draftBtn').onclick = () => generateDraft(name);
  } catch (e) {
    toast('打开表失败：' + e.message);
  }
}

async function generateDraft(table) {
  const panel = $('#draftPanel');
  panel.classList.remove('hidden');
  panel.innerHTML = '<div class="empty">分析字段、推断类型中…</div>';
  try {
    const m = await api(`/api/tables/${encodeURIComponent(table)}/draft-model`, { method: 'POST' });
    const errs = (m.lint && m.lint.errors) || [];
    const badge = m.lint && m.lint.passed
      ? '<span class="badge published">规范校验通过</span>'
      : `<span class="badge rejected">${errs.length} 项错误</span>`;
    const warns = (m.warnings || []).length
      ? `<div class="notes" style="margin:10px 0"><b>风险提示</b>
           <ul class="exp-list">${m.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul></div>`
      : '';

    panel.innerHTML = `
      <div class="result-head">
        <h3>语义模型草稿</h3>${badge}
        <span class="meta">${m.stats.metrics} 指标 / ${m.stats.dimensions} 维度</span>
        <span class="spacer"></span>
        <button class="ghost" id="saveDraft">保存草稿</button>
        <button class="primary" id="saveActivate">保存并激活</button>
      </div>
      ${warns}
      <textarea id="yamlBox" class="yaml-editor" spellcheck="false">${esc(m.yaml)}</textarea>`;

    const save = async (activate) => {
      try {
        await api('/api/semantic/drafts', {
          method: 'POST',
          body: JSON.stringify({ name: m.name, yaml: $('#yamlBox').value }),
        });
        if (activate) {
          await api(`/api/semantic/models/${encodeURIComponent(m.name)}/activate`,
                    { method: 'POST' });
          await refreshMeta();
          toast('已保存并激活，现在可以直接用自然语言问这张表');
        } else {
          toast('草稿已保存到 semantic/drafts/');
        }
      } catch (e) {
        toast('保存失败：' + e.message, 4600);
      }
    };
    $('#saveDraft').onclick = () => save(false);
    $('#saveActivate').onclick = () => save(true);
  } catch (e) {
    panel.innerHTML = `<div class="notes">生成失败：${esc(e.message)}</div>`;
  }
}

function readFileBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(',')[1] || '');
    r.onerror = reject;
    r.readAsDataURL(file);
  });
}

async function doUpload() {
  const file = $('#uploadFile').files[0];
  if (!file) { toast('请先选择文件'); return; }
  const b64 = await readFileBase64(file);
  try {
    const res = await api('/api/tables/import', {
      method: 'POST',
      body: JSON.stringify({
        filename: file.name,
        content_base64: b64,
        table_name: $('#uploadTableName').value.trim() || null,
      }),
    });
    $('#uploadModal').classList.add('hidden');
    $('#uploadFile').value = '';
    $('#uploadTableName').value = '';
    toast(`导入成功：${res.table}（${res.rows} 行）`);
    await loadTables();
    openTable(res.table);
  } catch (e) {
    toast('导入失败：' + e.message, 4600);
  }
}

async function refreshMeta() {
  state.meta = await api('/api/meta');
  const m = state.meta.model;
  const nature = m.data_nature === 'real'
    ? '<span class="data-badge real" title="真实统计数据">真实数据</span>'
    : m.data_nature === 'demo'
      ? '<span class="data-badge demo" title="合成演示数据，非真实业务">演示数据</span>'
      : '';
  $('#modelInfo').innerHTML =
    `${esc(m.title)} · ${state.meta.metrics.length} 指标 / ${state.meta.dimensions.length} 维度`
    + (nature ? ` ${nature}` : '');
  $('#modelInfo').title = m.data_note || '';
  renderExamples();
}

/* ------------------------------------------------------------------ 审批中心 */
async function loadApprovals() {
  try {
    const { items } = await api('/api/artifacts');
    const order = { submitted: 0, approved: 1, rejected: 2, published: 3, draft: 4, archived: 5 };
    items.sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9));
    $('#approvalMeta').textContent =
      `待审批 ${items.filter((i) => i.status === 'submitted').length} 项 · 共 ${items.length} 项`;

    if (!items.length) {
      $('#approvalList').innerHTML =
        '<div class="empty">还没有提交审批的产出。在看板里点「提交审批」。</div>';
      return;
    }

    $('#approvalList').innerHTML = items.map((a) => `
      <div class="dash-item" data-art="${a.id}">
        <h3>${esc(a.title || a.ref)}<span class="badge ${a.status}">${STATUS_LABEL[a.status] || a.status}</span></h3>
        <div class="sub">${esc(a.kind)} · v${a.version} · ${esc((a.updated_at || '').slice(0, 16).replace('T', ' '))}</div>
        <div class="sub">规范校验：${a.lint.passed ? '通过' : '未通过'} · 错误 ${a.lint.errors} / 提示 ${a.lint.warnings}</div>
        <div class="art-actions"></div>
      </div>`).join('');

    document.querySelectorAll('#approvalList .dash-item').forEach((el) => {
      const art = items.find((i) => i.id === el.dataset.art);
      const box = el.querySelector('.art-actions');
      const btns = [];
      if (art.status === 'submitted') btns.push(['通过', 'approve'], ['驳回', 'reject']);
      if (art.status === 'approved') btns.push(['发布', 'publish']);
      btns.forEach(([t, a]) => {
        const b = document.createElement('button');
        b.className = a === 'approve' || a === 'publish' ? 'primary' : 'ghost';
        b.textContent = t;
        b.onclick = async (ev) => {
          ev.stopPropagation();
          try {
            const comment = a === 'reject' ? (prompt('驳回原因', '') || '') : '';
            await api(`/api/artifacts/${art.id}/${a}`, {
              method: 'POST', body: JSON.stringify({ comment }),
            });
            toast(a === 'approve' ? '已通过' : a === 'reject' ? '已驳回' : '已发布');
            loadApprovals();
          } catch (e) {
            toast('失败：' + e.message, 4200);
          }
        };
        box.appendChild(b);
      });
      el.onclick = () => { if (art.kind === 'dashboard') openDashboard(art.ref); };
    });
  } catch (e) {
    $('#approvalList').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

/* ------------------------------------------------------------------ 数据开发 */
async function initBuilder() {
  if (state.builder) { renderBuilder(state.builder); return; }
  try {
    const { items } = await api('/api/tables');
    if (!items.length) {
      $('#builderMessages').innerHTML =
        '<div class="msg ai">还没有数据表。先去「数据」页导入一份 Excel / CSV，再回来开发模型。</div>';
      $('#builderModel').innerHTML = '<div class="empty">尚未开始</div>';
      return;
    }
    const opts = items.map((t) =>
      `<option value="${esc(t.name)}">${esc(t.name)}（${t.rows.toLocaleString()} 行）</option>`).join('');
    $('#builderMessages').innerHTML = `
      <div class="msg ai">先选一张表作为建模起点。之后可以继续说：「加一个客单价指标，等于订单金额/订单数量」「把订单金额改名为成交金额」「检查一下」。</div>
      <div class="start-card">
        <select id="builderTable">${opts}</select>
        <button class="primary" id="builderStart">开始建模</button>
      </div>`;
    $('#builderModel').innerHTML = '<div class="empty">尚未开始</div>';
    $('#builderLint').className = 'badge';
    $('#builderLint').textContent = '—';
    $('#builderStart').onclick = async () => {
      const table = $('#builderTable').value;
      try {
        state.builder = await api('/api/builder/sessions', {
          method: 'POST', body: JSON.stringify({ table }),
        });
        renderBuilder(state.builder);
      } catch (e) {
        toast('创建失败：' + e.message);
      }
    };
  } catch (e) {
    $('#builderMessages').innerHTML = `<div class="msg ai">加载失败：${esc(e.message)}</div>`;
  }
}

function renderBuilder(s) {
  const box = $('#builderMessages');
  box.innerHTML = (s.messages || []).map((m) =>
    `<div class="msg ${m.role === 'user' ? 'user' : 'ai'}">${esc(m.text)}</div>`).join('');
  box.scrollTop = box.scrollHeight;
  renderBuilderModel(s);
}

function renderBuilderModel(s) {
  const doc = s.doc;
  const lintEl = $('#builderLint');
  if (!doc) {
    $('#builderModel').innerHTML = '<div class="empty">尚未开始</div>';
    lintEl.className = 'badge';
    lintEl.textContent = '—';
    return;
  }
  const metrics = (doc.metrics || []).map((m) =>
    `<li>${esc(m.title || m.name)} <span class="muted">${esc(m.name)}</span></li>`).join('');
  const dims = (doc.dimensions || []).map((d) =>
    `<li>${esc(d.title || d.name)} <span class="muted">${esc(d.name)}</span></li>`).join('');

  $('#builderModel').innerHTML = `
    <div class="exp-sub" style="margin-top:0">${esc(doc.title || '')}</div>
    <div class="exp-sub">指标（${(doc.metrics || []).length}）</div>
    <ul class="model-list">${metrics || '<li class="muted">无</li>'}</ul>
    <div class="exp-sub">维度（${(doc.dimensions || []).length}）</div>
    <ul class="model-list">${dims || '<li class="muted">无</li>'}</ul>
    <details class="sqls" style="margin-top:12px">
      <summary>查看 YAML</summary>
      <pre class="sql">${esc(s.yaml || '')}</pre>
    </details>`;

  const lint = s.lint;
  if (lint && lint.passed) {
    lintEl.className = 'badge published';
    lintEl.textContent = '校验通过';
  } else {
    lintEl.className = 'badge rejected';
    lintEl.textContent = `${(lint && lint.errors ? lint.errors.length : 0)} 项错误`;
  }
}

async function builderSend() {
  const input = $('#builderInput');
  const text = input.value.trim();
  if (!text) return;
  if (!state.builder) { toast('请先选择一张表开始建模'); return; }
  input.value = '';
  input.disabled = true;
  try {
    state.builder = await api(`/api/builder/sessions/${state.builder.id}/say`, {
      method: 'POST', body: JSON.stringify({ message: text }),
    });
    renderBuilder(state.builder);
  } catch (e) {
    toast('发送失败：' + e.message, 4200);
  } finally {
    input.disabled = false;
    input.focus();
  }
}

async function builderSave(activate) {
  if (!state.builder) { toast('还没有模型'); return; }
  try {
    const res = await api(`/api/builder/sessions/${state.builder.id}/save`, {
      method: 'POST', body: JSON.stringify({ activate }),
    });
    if (activate) {
      await refreshMeta();
      toast('已保存并激活：' + res.name);
    } else {
      toast('草稿已保存：' + res.saved);
    }
  } catch (e) {
    toast('保存失败：' + e.message, 4600);
  }
}

/* ------------------------------------------------------------------ 数据源 */
async function loadSources() {
  try {
    const { items } = await api('/api/sources');
    const real = items.filter((s) => s.nature === 'real').length;
    $('#sourcesMeta').textContent =
      `${items.length} 个数据源 · 真实 ${real} / 演示 ${items.length - real}`;
    $('#sourceList').innerHTML = items.map(sourceCard).join('');
  } catch (e) {
    $('#sourceList').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

function sourceCard(s) {
  const nature = s.nature === 'real'
    ? '<span class="data-badge real">真实数据</span>'
    : '<span class="data-badge demo">演示数据</span>';
  const size = !s.size_kb ? '—'
    : s.size_kb >= 1024 ? `${(s.size_kb / 1024).toFixed(1)} MB` : `${s.size_kb} KB`;
  const tables = (s.tables || []).map((t) => `
    <tr>
      <td>${esc(t.name)}</td>
      <td class="num">${t.rows == null ? '-' : t.rows.toLocaleString('zh-CN')}</td>
      <td class="num">${t.columns == null ? '-' : t.columns}</td>
    </tr>`).join('');
  const tableHtml = tables
    ? `<table class="source-tables">
         <thead><tr><th>表</th><th class="num">行数</th><th class="num">字段</th></tr></thead>
         <tbody>${tables}</tbody>
       </table>`
    : `<p class="source-empty">数据源未就绪，运行 <code>${esc(s.setup || '')}</code> 生成</p>`;
  return `<div class="source-card">
    <div class="source-head">
      <h3>${esc(s.title)}</h3>
      ${nature}
      <span class="source-model">语义模型 ${esc(s.model)}</span>
    </div>
    <p class="source-desc">${esc(s.description)}</p>
    <div class="source-meta">
      <span><code>${esc(s.path)}</code></span>
      <span>${size}</span>
      <span>更新 ${esc((s.updated_at || '').slice(0, 16).replace('T', ' '))}</span>
    </div>
    ${tableHtml}
  </div>`;
}

/* ------------------------------------------------------------------ 从模板创建看板 */
async function openTemplatePicker() {
  try {
    const { items } = await api('/api/templates?kind=dashboard');
    if (!items.length) { toast('没有可用的看板模板'); return; }
    const brief = (t) => {
      const d = String(t.description || '');
      const i = d.indexOf('。');
      const s = i > 0 ? d.slice(0, i) : d;
      return s.length > 48 ? s.slice(0, 48) + '…' : s;
    };
    $('#tplSelect').innerHTML = items.map((t) =>
      `<option value="${esc(t.id)}">${esc(t.title)} — ${esc(brief(t))}</option>`).join('');
    $('#tplTitle').value = '经营分析看板';
    $('#tplModal').classList.remove('hidden');
  } catch (e) {
    toast('加载模板失败：' + e.message);
  }
}

async function createDashboardFromTemplate() {
  const tid = $('#tplSelect').value;
  const title = $('#tplTitle').value.trim();
  if (!title) { toast('请填写看板标题'); return; }
  try {
    const doc = await api(`/api/templates/${encodeURIComponent(tid)}/create-dashboard`, {
      method: 'POST',
      body: JSON.stringify({ params: { title } }),
    });
    $('#tplModal').classList.add('hidden');
    toast('看板已创建：' + doc.title);
    await loadDashboards();
    openDashboard(doc.id);
  } catch (e) {
    toast('创建失败：' + e.message, 5200);
  }
}

/* ------------------------------------------------------------------ 初始化 */
function switchView(name) {
  document.querySelectorAll('.nav-btn').forEach((b) =>
    b.classList.toggle('active', b.dataset.view === name));
  document.querySelectorAll('.view').forEach((v) =>
    v.classList.toggle('active', v.id === 'view-' + name));
  if (name === 'data') loadTables();
  if (name === 'sources') loadSources();
  if (name === 'builder') initBuilder();
  if (name === 'dashboards') loadDashboards();
  if (name === 'approvals') loadApprovals();
}

function renderExamples() {
  const ex = (state.meta && state.meta.examples) || [];
  $('#examples').innerHTML = ex.map((q) => `<button class="chip">${esc(q)}</button>`).join('');
  document.querySelectorAll('#examples .chip').forEach((b) => {
    b.onclick = () => { $('#question').value = b.textContent; doAsk(); };
  });
}

async function init() {
  document.querySelectorAll('.nav-btn').forEach((b) => {
    b.onclick = () => { state.shareToken = null; switchView(b.dataset.view); };
  });
  $('#askBtn').onclick = doAsk;
  $('#question').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) doAsk();
  });
  $('#newDashBtn').onclick = async () => {
    const title = prompt('看板名称', '我的分析看板');
    if (!title) return;
    try {
      const doc = await api('/api/dashboards', { method: 'POST', body: JSON.stringify({ title }) });
      await loadDashboards();
      openDashboard(doc.id);
    } catch (e) {
      toast('创建失败：' + e.message);
    }
  };

  $('#uploadBtn').onclick = () => $('#uploadModal').classList.remove('hidden');
  $('#uploadCancel').onclick = () => $('#uploadModal').classList.add('hidden');
  $('#uploadConfirm').onclick = doUpload;

  $('#builderSend').onclick = builderSend;
  $('#builderInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) builderSend();
  });
  $('#builderSave').onclick = () => builderSave(false);
  $('#builderActivate').onclick = () => builderSave(true);

  $('#infoClose').onclick = () => $('#infoModal').classList.add('hidden');

  $('#tplDashBtn').onclick = openTemplatePicker;
  $('#tplCancel').onclick = () => $('#tplModal').classList.add('hidden');
  $('#tplConfirm').onclick = createDashboardFromTemplate;

  $('#descClose').onclick = () => $('#descModal').classList.add('hidden');
  $('#descCancel').onclick = () => $('#descModal').classList.add('hidden');
  $('#descRegen').onclick = regenDescribe;
  $('#descApply').onclick = applyDescribe;

  try {
    await refreshMeta();
  } catch (e) {
    $('#modelInfo').textContent = '语义模型加载失败';
  }

  const params = new URLSearchParams(location.search);
  if (params.get('share')) {
    state.shareToken = params.get('share');
    switchView('dashboards');
    openShared(state.shareToken);
  }
}

document.addEventListener('DOMContentLoaded', init);
