/* 图表引擎：按容器实际宽度绘制（不再用 920 固定 viewBox 缩放），
 * 保证在窄卡片里字号依然可读；支持悬浮提示、横向条形图。
 * 依赖 app.js 里的 esc() / fmtNum()（在调用时已加载）。
 */

const PALETTE = ['#2563eb', '#0891b2', '#16a34a', '#d97706',
                 '#db2777', '#7c3aed', '#dc2626', '#0d9488'];

/* 散点分组着色：13 个城市也能分得开，但整体压暗一档避免刺眼 */
const SCATTER_PALETTE = ['#2563eb', '#0891b2', '#16a34a', '#ca8a04', '#dc2626',
                         '#7c3aed', '#db2777', '#0d9488', '#ea580c', '#4f46e5',
                         '#65a30d', '#c026d3', '#0284c7'];

const _chartRegistry = new Map();
let _chartSeq = 0;

/* 生成图表占位，DOM 插入后再按实际宽度绘制 */
function chartSlot(kind, columns, rows, opts) {
  const id = 'ch' + (++_chartSeq);
  _chartRegistry.set(id, { kind, columns, rows, opts });
  return `<div class="chart-slot" data-chart="${id}"></div>`;
}

function hydrateCharts(root) {
  disposeDetachedCharts();
  const scope = root || document;
  scope.querySelectorAll('[data-chart]').forEach((el) => {
    const spec = _chartRegistry.get(el.dataset.chart);
    if (spec) drawChart(el, spec.kind, spec.columns, spec.rows, spec.opts);
  });
}

/* 窗口尺寸变化：echarts 走 resize，SVG 兜底走重绘 */
let _resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(_resizeTimer);
  _resizeTimer = setTimeout(() => {
    disposeDetachedCharts();
    _echartsInstances.forEach((chart) => {
      try { chart.resize(); } catch (e) { /* ignore */ }
    });
    document.querySelectorAll('[data-chart]').forEach((el) => {
      if (el.querySelector('canvas')) return;         // echarts 自己处理
      const spec = _chartRegistry.get(el.dataset.chart);
      if (spec) drawChart(el, spec.kind, spec.columns, spec.rows);
    });
  }, 180);
});

function drawChart(el, kind, columns, rows, opts) {
  if (!el) return;
  if (!rows || !rows.length) {
    el.innerHTML = '<div class="empty">没有数据</div>';
    return;
  }
  if (kind === 'kpi') {
    el.innerHTML = kpiHTML(columns, rows, !!el.closest('.dash-card-body'));
    return;
  }
  if (kind === 'table') {
    el.innerHTML = `<div class="table-wrap">${tableHTML(columns, rows)}</div>`;
    return;
  }
  if (kind === 'map') {
    renderMap(el, columns, rows, opts || {});
    return;
  }
  if (window.echarts) {
    renderEcharts(el, kind, columns, rows, opts);
  } else {
    renderSVG(el, kind, columns, rows);   // 离线兜底：echarts 文件缺失时退回手写 SVG
  }
}

/* 手写 SVG 兜底（无 echarts 时使用） */
function renderSVG(el, kind, columns, rows) {
  el.style.display = '';
  const W = Math.max(260, Math.floor(el.clientWidth || (el.parentElement && el.parentElement.clientWidth) || 620));
  const inCard = !!el.closest('.dash-card-body');
  const legendH = (kind === 'line' || kind === 'bar' || kind === 'bar_h' || kind === 'pie') ? 26 : 0;

  let H;
  if (inCard) {
    // 卡片里必须适配卡片高度，否则底部横轴会被裁掉
    const avail = el.clientHeight || (el.parentElement && el.parentElement.clientHeight) || 0;
    H = avail > 80 ? avail - legendH : Math.round(W * 0.34);
  } else {
    H = Math.min(380, Math.max(200, Math.round(W * 0.4)));
  }
  H = Math.max(130, Math.round(H));
  if (kind === 'bar_h') H = Math.max(H, rows.length * 24 + 12);

  let html;
  switch (kind) {
    case 'pie': html = pieSVG(columns, rows, W, H); break;
    case 'bar_h': html = barHSVG(columns, rows, W, H); break;
    case 'bar': html = barSVG(columns, rows, W, H); break;
    default: html = lineSVG(columns, rows, W, H);
  }
  el.innerHTML = html;
  bindChartTips(el);
}

/* ------------------------------------------------------------------ ECharts 渲染 */
const _echartsInstances = new Set();

function disposeDetachedCharts() {
  _echartsInstances.forEach((chart) => {
    const dom = chart.getDom && chart.getDom();
    if (!dom || !document.body.contains(dom)) {
      try { chart.dispose(); } catch (e) { /* ignore */ }
      _echartsInstances.delete(chart);
    }
  });
}

function renderEcharts(el, kind, columns, rows, opts) {
  const ec = window.echarts;
  const prev = ec.getInstanceByDom(el);
  if (prev) { prev.dispose(); _echartsInstances.delete(prev); }

  el.innerHTML = '';
  el.style.display = 'block';
  const inCard = !!el.closest('.dash-card-body');
  if (!inCard) {
    const w = el.clientWidth || 620;
    el.style.height = Math.min(420, Math.max(240, Math.round(w * 0.4))) + 'px';
  }

  const chart = ec.init(el, null, { renderer: 'canvas' });
  chart.setOption(_echartsOption(kind, columns, rows, opts), true);
  _echartsInstances.add(chart);
  if (kind === 'scatter' && columns.length >= 5) {
    el._scatterCatIdx = 2;
    ensureScatterSwitch(el, chart, columns, rows, opts);
  }
}

/* 散点分组切换：按区域 / 按城市 */
function ensureScatterSwitch(el, chart, columns, rows, opts) {
  let box = el.querySelector('.chart-switch');
  if (!box) {
    box = document.createElement('div');
    box.className = 'chart-switch';
    el.appendChild(box);
  }
  const cur = el._scatterCatIdx == null ? 2 : el._scatterCatIdx;
  const modes = [{ idx: 2, label: '按区域' }, { idx: 1, label: '按城市' }];
  box.innerHTML = '';
  modes.forEach((m) => {
    const btn = document.createElement('button');
    btn.className = 'chart-switch-btn' + (m.idx === cur ? ' active' : '');
    btn.textContent = m.label;
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      el._scatterCatIdx = m.idx;
      chart.setOption(_echartsOption('scatter', columns, rows, { ...(opts || {}), catIdx: m.idx }), true);
      ensureScatterSwitch(el, chart, columns, rows, opts);
    });
    box.appendChild(btn);
  });
}

/* 过原点的平均强度参考线：斜率 = Σy/Σx。
 * 点在线上方 → 单位强度高于 13 市平均；下方 → 低于平均。 */
function refLineThroughOrigin(xs, ys) {
  const sumX = xs.reduce((a, b) => a + b, 0);
  const sumY = ys.reduce((a, b) => a + b, 0);
  const slope = sumX > 0 ? sumY / sumX : 0;
  const xMax = xs.length ? Math.max(...xs) : 1;
  return {
    silent: true, symbol: 'none', animation: false,
    lineStyle: { type: 'dashed', color: '#94a3b8', width: 1 },
    label: {
      show: true, position: 'end', fontSize: 11, color: '#94a3b8',
      distance: [0, -8],
      formatter: `平均 ${slope.toFixed(3)} 度/元`,
    },
    data: [[{ coord: [0, 0] }, { coord: [xMax, xMax * slope] }]],
    z: 1,
  };
}

const _fmtV = (v, f) => (typeof fmtNum === 'function' ? fmtNum(v, f) : String(v));

function _echartsOption(kind, columns, rows, opts) {
  const labels = rows.map((r) => String(r[0]));
  const seriesCols = columns.slice(1);
  const firstFmt = seriesCols[0] && seriesCols[0].format;
  const text = { fontSize: 13, color: '#475569' };
  const axisLabel = { fontSize: 12, color: '#94a3b8' };
  const splitLine = { lineStyle: { color: '#eef2f7' } };
  const base = {
    color: PALETTE,
    textStyle: text,
    animationDuration: 420,
  };

  if (kind === 'scatter') {
    // 5 列 = [地区, 城市, 区域, x, y]；4 列 = [地区, 分类, x, y]；3 列 = [地区, x, y]
    const n = columns.length;
    let catIdx = -1;
    let xIdx;
    let yIdx;
    if (n >= 5) {
      catIdx = (opts && opts.catIdx === 1) ? 1 : 2;
      xIdx = 3;
      yIdx = 4;
    } else if (n === 4) {
      catIdx = 1;
      xIdx = 2;
      yIdx = 3;
    } else {
      xIdx = 1;
      yIdx = 2;
    }
    const hasCat = catIdx >= 0;
    const xCol = columns[xIdx] || {};
    const yCol = columns[yIdx] || {};
    const xs = rows.map((r) => Number(r[xIdx])).filter((v) => Number.isFinite(v));
    const ys = rows.map((r) => Number(r[yIdx])).filter((v) => Number.isFinite(v));
    const avgX = xs.reduce((a, b) => a + b, 0) / (xs.length || 1);
    const avgY = ys.reduce((a, b) => a + b, 0) / (ys.length || 1);
    const tooltip = {
      trigger: 'item',
      textStyle: { fontSize: 13 },
      formatter: (p) => {
        const v = p.value || [];
        const cat = hasCat ? `<br/><span style="color:#94a3b8">${esc(p.seriesName)}</span>` : '';
        const extra = (opts && opts.ref_line && Number(v[0]) > 0)
          ? `<br/>单位 GDP 电耗：${(Number(v[1]) / Number(v[0])).toFixed(3)} 度/元`
          : '';
        return `<b>${esc(p.name)}</b>${cat}<br/>${esc(xCol.label || '')}：${esc(_fmtV(v[0], xCol.format))}`
          + `<br/>${esc(yCol.label || '')}：${esc(_fmtV(v[1], yCol.format))}${extra}`;
      },
    };
    const markLine = {
      silent: true, symbol: 'none',
      lineStyle: { type: 'dashed', color: '#cbd5e1', width: 1 },
      label: { show: false },
      data: [{ xAxis: avgX }, { yAxis: avgY }],
    };
    const baseAxis = {
      xAxis: {
        type: 'value', name: xCol.label || '',
        nameLocation: 'middle', nameGap: 26,
        nameTextStyle: { fontSize: 11, color: '#94a3b8' },
        axisLabel: { ...axisLabel, formatter: (v) => _fmtV(v, xCol.format) },
        splitLine,
      },
      yAxis: {
        type: 'value', name: yCol.label || '',
        nameGap: 8, nameLocation: 'end',
        nameTextStyle: { fontSize: 11, color: '#94a3b8' },
        axisLabel: { ...axisLabel, formatter: (v) => _fmtV(v, yCol.format) },
        splitLine,
      },
    };

    if (hasCat) {
      const groups = new Map();
      rows.forEach((r) => {
        const c = String(r[catIdx]);
        if (!groups.has(c)) groups.set(c, []);
        groups.get(c).push({ name: String(r[0]), value: [Number(r[xIdx]), Number(r[yIdx])] });
      });
      const REGION_ORDER = ['苏南', '苏中', '苏北'];
      const cats = [...groups.keys()].sort((a, b) => {
        const ia = REGION_ORDER.indexOf(a);
        const ib = REGION_ORDER.indexOf(b);
        if (ia >= 0 && ib >= 0) return ia - ib;   // 区域固定 苏南→苏中→苏北
        return a.localeCompare(b, 'zh');
      });
      const series = cats.map((c, i) => ({
        name: c,
        type: 'scatter',
        data: groups.get(c),
        symbolSize: 10,
        itemStyle: { color: SCATTER_PALETTE[i % SCATTER_PALETTE.length], opacity: 0.85 },
        label: {
          show: true, position: 'top', fontSize: 11, color: '#64748b',
          formatter: (p) => p.name,          // 全部候选，由 hideOverlap 动态决定显示哪些
        },
        labelLayout: { hideOverlap: true },
        emphasis: { scale: 1.5 },
      }));
      if (series.length) {
        series[0].markLine = (opts && opts.ref_line) ? refLineThroughOrigin(xs, ys) : markLine;
      }
      return {
        ...base,
        color: SCATTER_PALETTE,
        tooltip,
        legend: {
          type: 'plain', bottom: 0, left: 'center', width: '92%',
          itemWidth: 9, itemHeight: 9, itemGap: 10,
          textStyle: { fontSize: 10, color: '#475569' },
        },
        grid: { left: 16, right: 28, top: 56, bottom: 48, containLabel: true },
        ...baseAxis,
        series,
      };
    }

    return {
      ...base,
      tooltip,
      grid: { left: 16, right: 28, top: 34, bottom: 30, containLabel: true },
      ...baseAxis,
      series: [{
        type: 'scatter',
        data: rows.map((r) => ({ name: String(r[0]), value: [Number(r[1]), Number(r[2])] })),
        symbolSize: 11,
        itemStyle: { color: '#2563eb', opacity: 0.8 },
        label: {
          show: true, position: 'top', fontSize: 11, color: '#64748b',
          formatter: (p) => p.name,          // 全部候选，由 hideOverlap 动态决定显示哪些
        },
        labelLayout: { hideOverlap: true },
        markLine,
        emphasis: { itemStyle: { color: '#1d4ed8' }, label: { fontSize: 12 } },
      }],
    };
  }

  if (kind === 'pie') {
    return {
      ...base,
      tooltip: { trigger: 'item', textStyle: { fontSize: 13 } },
      legend: { type: 'scroll', orient: 'vertical', right: 8, top: 'middle',
                textStyle: { fontSize: 13, color: '#475569' } },
      series: [{
        type: 'pie', radius: ['32%', '64%'], center: ['32%', '52%'],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: '#fff', borderWidth: 2 },
        label: { fontSize: 12, formatter: '{b}\n{d}%' },
        data: rows.map((r) => ({ name: String(r[0]), value: Number(r[1]) || 0 })),
        tooltip: { valueFormatter: (v) => _fmtV(v, columns[1] && columns[1].format) },
      }],
    };
  }

  if (kind === 'bar_h') {
    return {
      ...base,
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, textStyle: { fontSize: 13 } },
      grid: { left: 8, right: 96, top: 24, bottom: 8, containLabel: true },
      legend: seriesCols.length > 1
        ? { top: 0, right: 0, textStyle: { fontSize: 13 } } : undefined,
      xAxis: { type: 'value', axisLabel: { ...axisLabel, formatter: (v) => _fmtV(v, firstFmt) }, splitLine },
      yAxis: { type: 'category', data: labels, inverse: true,
               axisLabel: { fontSize: 13, color: '#475569' },
               axisTick: { show: false }, axisLine: { show: false } },
      series: seriesCols.map((c, i) => ({
        name: c.label, type: 'bar', data: rows.map((r) => r[i + 1]),
        barMaxWidth: 20,
        itemStyle: { borderRadius: [0, 3, 3, 0], color: PALETTE[i % PALETTE.length] },
        label: { show: true, position: 'right', fontSize: 12, color: '#475569',
                 formatter: (p) => _fmtV(p.value, c.format) },
        tooltip: { valueFormatter: (v) => _fmtV(v, c.format) },
        emphasis: { focus: 'series' },
      })),
    };
  }

  if (kind === 'bar') {
    return {
      ...base,
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, textStyle: { fontSize: 13 } },
      grid: { left: 12, right: 20, top: 40, bottom: 12, containLabel: true },
      legend: { top: 0, right: 0, textStyle: { fontSize: 13 } },
      xAxis: {
        type: 'category', data: labels, axisTick: { show: false },
        axisLabel: { fontSize: 12, color: '#64748b', interval: 0,
                     rotate: labels.length > 7 ? 22 : 0 },
        axisLine: { lineStyle: { color: '#e2e8f0' } },
      },
      yAxis: { type: 'value', axisLabel: { ...axisLabel, formatter: (v) => _fmtV(v, firstFmt) }, splitLine },
      series: seriesCols.map((c, i) => ({
        name: c.label, type: 'bar', data: rows.map((r) => r[i + 1]),
        barMaxWidth: 40,
        itemStyle: { borderRadius: [3, 3, 0, 0], color: PALETTE[i % PALETTE.length] },
        tooltip: { valueFormatter: (v) => _fmtV(v, c.format) },
        emphasis: { focus: 'series' },
      })),
    };
  }

  // 折线
  const zoom = labels.length > 40;
  return {
    ...base,
    tooltip: { trigger: 'axis', axisPointer: { type: 'line' }, textStyle: { fontSize: 13 } },
    legend: { top: 0, right: 0, textStyle: { fontSize: 13 } },
    grid: { left: 12, right: 24, top: 40, bottom: 12, containLabel: true },
    xAxis: {
      type: 'category', data: labels, boundaryGap: false,
      axisLabel: { fontSize: 12, color: '#64748b' },
      axisTick: { show: false }, axisLine: { lineStyle: { color: '#e2e8f0' } },
    },
    yAxis: { type: 'value', axisLabel: { ...axisLabel, formatter: (v) => _fmtV(v, firstFmt) }, splitLine },
    dataZoom: zoom ? [{ type: 'inside' }] : undefined,
    series: seriesCols.map((c, i) => ({
      name: c.label, type: 'line', data: rows.map((r) => r[i + 1]),
      showSymbol: labels.length <= 40, symbol: 'circle', symbolSize: 5,
      lineStyle: { width: 2.4 },
      itemStyle: { color: PALETTE[i % PALETTE.length] },
      connectNulls: false,
      areaStyle: seriesCols.length === 1 ? { opacity: 0.07 } : undefined,
      tooltip: { valueFormatter: (v) => _fmtV(v, c.format) },
      emphasis: { focus: 'series' },
    })),
  };
}

/* ------------------------------------------------------------------ 工具 */
function tableHTML(columns, rows) {
  const head = columns.map((c) => `<th>${esc(c.label)}</th>`).join('');
  const body = rows.map((r) => `<tr>${r.map((v, i) =>
    `<td>${esc(fmtNum(v, columns[i] && columns[i].format))}</td>`).join('')}</tr>`).join('');
  return `<table class="grid"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function kpiHTML(columns, rows, compact) {
  const r = rows[0] || [];
  // 卡片里标题已经有了，避免重复显示标签
  return '<div class="kpi-row">' + columns.map((c, i) => `
    <div class="kpi">
      ${compact ? '' : `<div class="kpi-label">${esc(c.label)}</div>`}
      <div class="kpi-value">${esc(fmtNum(r[i], c.format))}</div>
    </div>`).join('') + '</div>';
}

function axisY(max, padL, padT, ph, W, format) {
  let out = '';
  for (let i = 0; i <= 4; i++) {
    const v = (max * i) / 4;
    const y = padT + ph - (v / max) * ph;
    out += `<line x1="${padL}" y1="${y}" x2="${W - 14}" y2="${y}" stroke="#eef2f7"/>`;
    out += `<text x="${padL - 10}" y="${y + 4}" text-anchor="end"
              font-size="12" fill="#94a3b8">${esc(fmtNum(v, format))}</text>`;
  }
  return out;
}

function xTickIndexes(n, want = 9) {
  if (n <= want) return [...Array(n).keys()];
  const step = Math.ceil(n / want);
  const out = [];
  for (let i = 0; i < n; i += step) out.push(i);
  if (out[out.length - 1] !== n - 1) out.push(n - 1);
  return out;
}

function legendHTML(series) {
  return '<div class="chart-legend">' + series.map((s, i) =>
    `<span><i style="background:${PALETTE[i % PALETTE.length]}"></i>${esc(s.label)}</span>`).join('') + '</div>';
}

const _num = (v) => {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};

/* ------------------------------------------------------------------ 折线 */
function lineSVG(columns, rows, W, H) {
  const padL = 66, padR = 16, padT = 18, padB = 38;
  const pw = Math.max(40, W - padL - padR);
  const ph = Math.max(60, H - padT - padB);
  const xs = rows.map((r) => String(r[0]));
  const series = columns.slice(1).map((c, i) => ({
    label: c.label, format: c.format, data: rows.map((r) => _num(r[i + 1])),
  }));
  const all = series.flatMap((s) => s.data).filter((v) => v !== null);
  const max = niceMax(Math.max(1, ...all));
  const X = (i) => (xs.length === 1 ? padL + pw / 2 : padL + (i * pw) / (xs.length - 1));
  const Y = (v) => padT + ph - (v / max) * ph;
  const withDots = xs.length <= 60;

  let body = axisY(max, padL, padT, ph, W, series[0] && series[0].format);
  series.forEach((s, si) => {
    const color = PALETTE[si % PALETTE.length];
    let seg = [];
    const flush = () => {
      if (seg.length > 1) {
        body += `<polyline points="${seg.join(' ')}" fill="none" stroke="${color}"
          stroke-width="2.6" stroke-linejoin="round" stroke-linecap="round"/>`;
      }
      seg = [];
    };
    s.data.forEach((v, i) => {
      if (v === null) { flush(); return; }
      seg.push(`${X(i)},${Y(v)}`);
      if (withDots) {
        const tip = `${xs[i]}：${fmtNum(v, s.format)}`;
        body += `<circle cx="${X(i)}" cy="${Y(v)}" r="3" fill="#fff" stroke="${color}" stroke-width="2"/>`;
        body += `<circle cx="${X(i)}" cy="${Y(v)}" r="11" fill="transparent"
                   data-tip="${esc(tip)}" style="pointer-events:all;cursor:pointer"/>`;
      }
    });
    flush();
  });

  xTickIndexes(xs.length).forEach((i) => {
    body += `<text x="${X(i)}" y="${H - 12}" text-anchor="middle"
              font-size="12" fill="#94a3b8">${esc(xs[i])}</text>`;
  });

  return `<div class="chart-wrap">${legendHTML(series)}<svg width="100%" height="${H}"
    viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${body}</svg></div>`;
}

/* ------------------------------------------------------------------ 柱状（竖向） */
function barSVG(columns, rows, W, H) {
  const padL = 66, padR = 16, padT = 18, padB = 56;
  const pw = Math.max(40, W - padL - padR);
  const ph = Math.max(60, H - padT - padB);
  const labels = rows.map((r) => String(r[0]));
  const series = columns.slice(1).map((c, i) => ({
    label: c.label, format: c.format, data: rows.map((r) => _num(r[i + 1])),
  }));
  const max = niceMax(Math.max(1, ...series.flatMap((s) => s.data).filter((v) => v !== null)));
  const n = Math.max(labels.length, 1);
  const groupW = pw / n;
  const barW = Math.max(5, Math.min(52, (groupW * 0.6) / series.length));
  const rotate = n > 6;

  let body = axisY(max, padL, padT, ph, W, series[0] && series[0].format);
  rows.forEach((_, i) => {
    const gx = padL + groupW * i + (groupW - barW * series.length) / 2;
    series.forEach((s, si) => {
      const v = s.data[i];
      if (v === null) return;
      const h = Math.max(1, (v / max) * ph);
      const tip = `${labels[i]}：${fmtNum(v, s.format)}`;
      body += `<rect x="${gx + si * barW}" y="${padT + ph - h}" width="${barW - 2}"
                 height="${h}" rx="2" fill="${PALETTE[si % PALETTE.length]}"
                 data-tip="${esc(tip)}" style="pointer-events:all"/>`;
    });
    const lx = padL + groupW * i + groupW / 2;
    const label = labels[i].length > 8 ? labels[i].slice(0, 7) + '…' : labels[i];
    if (rotate) {
      body += `<text x="${lx}" y="${H - 34}" text-anchor="end" font-size="12" fill="#64748b"
                 transform="rotate(-22 ${lx} ${H - 34})">${esc(label)}</text>`;
    } else {
      body += `<text x="${lx}" y="${H - 20}" text-anchor="middle" font-size="12"
                 fill="#64748b">${esc(label)}</text>`;
    }
  });
  return `<div class="chart-wrap">${legendHTML(series)}<svg width="100%" height="${H}"
    viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${body}</svg></div>`;
}

/* ------------------------------------------------------------------ 条形（横向，适合 13 市） */
function barHSVG(columns, rows, W, H) {
  const padL = 8, padR = 8, padT = 8, padB = 8;
  const labelW = Math.min(120, Math.max(70, W * 0.22));
  const valueW = 92;
  const barMax = Math.max(40, W - labelW - valueW - padL - padR - 12);
  const n = rows.length;
  const rowH = Math.max(22, (H - padT - padB) / n);
  const barH = Math.min(20, rowH * 0.62);
  const series = columns.slice(1).map((c, i) => ({
    label: c.label, format: c.format, data: rows.map((r) => _num(r[i + 1])),
  }));
  const max = niceMax(Math.max(1, ...series.flatMap((s) => s.data).filter((v) => v !== null)));
  const color = PALETTE[0];

  let body = '';
  rows.forEach((row, i) => {
    const cy = padT + rowH * i + rowH / 2;
    const label = String(row[0]);
    const short = label.length > 7 ? label.slice(0, 6) + '…' : label;
    const v = series[0] ? series[0].data[i] : null;
    const w = v === null ? 0 : Math.max(2, (v / max) * barMax);
    const tip = `${label}：${fmtNum(v, series[0] && series[0].format)}`;
    body += `<text x="${labelW - 8}" y="${cy + 4}" text-anchor="end" font-size="13"
               fill="#475569">${esc(short)}</text>`;
    body += `<rect x="${labelW}" y="${cy - barH / 2}" width="${barMax}" height="${barH}"
               rx="3" fill="#f1f5f9"/>`;
    body += `<rect x="${labelW}" y="${cy - barH / 2}" width="${w}" height="${barH}"
               rx="3" fill="${color}" data-tip="${esc(tip)}" style="pointer-events:all"/>`;
    body += `<text x="${labelW + barMax + 8}" y="${cy + 4}" font-size="13"
               fill="#334155">${esc(fmtNum(v, series[0] && series[0].format))}</text>`;
  });

  return `<div class="chart-wrap">${legendHTML(series)}<svg width="100%" height="${H}"
    viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${body}</svg></div>`;
}

/* ------------------------------------------------------------------ 饼图 */
function pieSVG(columns, rows, W, H) {
  const r = Math.min(H / 2 - 16, W * 0.26);
  const cx = 16 + r, cy = H / 2;
  const total = rows.reduce((s, row) => s + (Number(row[1]) || 0), 0) || 1;
  let a0 = -Math.PI / 2;
  let body = '';
  let legend = '';
  rows.forEach((row, i) => {
    const v = Number(row[1]) || 0;
    const a1 = a0 + (v / total) * Math.PI * 2;
    const large = a1 - a0 > Math.PI ? 1 : 0;
    const x0 = cx + r * Math.cos(a0), y0 = cy + r * Math.sin(a0);
    const x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1);
    const color = PALETTE[i % PALETTE.length];
    const tip = `${row[0]}：${fmtNum(v, columns[1] && columns[1].format)}（${(v / total * 100).toFixed(1)}%）`;
    body += `<path d="M ${cx} ${cy} L ${x0} ${y0} A ${r} ${r} 0 ${large} 1 ${x1} ${y1} Z"
               fill="${color}" stroke="#fff" stroke-width="2"
               data-tip="${esc(tip)}" style="pointer-events:all"/>`;
    const lx = cx + r + 28;
    const ly = H / 2 - (rows.length * 26) / 2 + i * 26 + 18;
    legend += `<g>
      <rect x="${lx}" y="${ly - 11}" width="12" height="12" rx="3" fill="${color}"/>
      <text x="${lx + 20}" y="${ly}" font-size="13" fill="#334155">${esc(row[0])}</text>
      <text x="${lx + 150}" y="${ly}" font-size="13" fill="#64748b">${(v / total * 100).toFixed(1)}%</text>
      <text x="${lx + 215}" y="${ly}" font-size="13" fill="#64748b">${esc(fmtNum(v, columns[1] && columns[1].format))}</text>
    </g>`;
  });
  return `<div class="chart-wrap"><svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}"
    preserveAspectRatio="none">${body}${legend}</svg></div>`;
}

/* ------------------------------------------------------------------ 地图（江苏：市 / 县） */
const JS_ADCODE = {
  '南京市': '320100', '无锡市': '320200', '徐州市': '320300', '常州市': '320400',
  '苏州市': '320500', '南通市': '320600', '连云港市': '320700', '淮安市': '320800',
  '盐城市': '320900', '扬州市': '321000', '镇江市': '321100', '泰州市': '321200',
  '宿迁市': '321300',
};
const JS_PROVINCE = '320000';
const _geoPromises = new Map();

function _loadGeo(url, name) {
  const ec = window.echarts;
  if (ec.getMap(name)) return Promise.resolve(name);
  if (!_geoPromises.has(name)) {
    const p = fetch(url)
      .then((r) => {
        if (!r.ok) throw new Error('地图数据加载失败');
        return r.json();
      })
      .then((geo) => { ec.registerMap(name, geo); return name; })
      .catch((e) => { _geoPromises.delete(name); throw e; });
    _geoPromises.set(name, p);
  }
  return _geoPromises.get(name);
}

function loadGeo(code) {
  return _loadGeo('/static/vendor/geo/' + code + '.json', 'geo_' + code);
}

/* 下钻底图：市辖区已合并成一块「XX市区」 */
function loadGeoDrill(code) {
  return _loadGeo('/static/vendor/geo/drill/' + code + '.json', 'drill_' + code);
}

async function renderMap(el, columns, rows, opts, metricIdx) {
  const ec = window.echarts;
  if (!ec) { el.innerHTML = '<div class="err">ECharts 未加载，无法渲染地图</div>'; return; }

  const prev = ec.getInstanceByDom(el);
  if (prev) { prev.dispose(); _echartsInstances.delete(prev); }

  el.style.display = 'block';
  if (!el.style.position || el.style.position === 'static') el.style.position = 'relative';
  const inCard = !!el.closest('.dash-card-body');
  if (!inCard) {
    const w = el.clientWidth || 620;
    el.style.height = Math.min(520, Math.max(320, Math.round(w * 0.6))) + 'px';
  }
  el.innerHTML = '<div class="chart-loading">地图加载中…</div>';

  let mapName;
  try {
    mapName = await loadGeo(JS_PROVINCE);
  } catch (e) {
    el.innerHTML = `<div class="err">${esc(e.message)}</div>`;
    return;
  }
  if (!document.body.contains(el)) return;

  el.innerHTML = '';
  el._mapOrigin = { columns, rows, opts, metricIdx: metricIdx || 0 };
  const chart = ec.init(el, null, { renderer: 'canvas' });
  _echartsInstances.add(chart);

  applyMap(chart, el, mapName, columns, rows,
    (p) => {
      const code = JS_ADCODE[p.name];
      if (!code || !opts.drill_metrics && !opts.drill) return;
      drillMap(chart, el, p.name, code, opts);
    },
    null, opts, metricIdx || 0);
}

function applyMap(chart, el, mapName, columns, rows, onDrill, onBack, opts, metricIdx) {
  const ec = window.echarts;
  opts = opts || {};
  const idx = Math.max(0, Math.min(Math.max(0, columns.length - 2), metricIdx || 0));
  const valueCol = columns[idx + 1] || columns[1];
  const label = valueCol ? valueCol.label : '数值';
  const fmt = valueCol && valueCol.format;
  const data = rows.map((r) => ({
    name: String(r[0]),
    value: (r[idx + 1] === null || r[idx + 1] === undefined) ? null : Number(r[idx + 1]),
  }));
  const values = data.map((d) => d.value).filter((v) => v !== null && !Number.isNaN(v));
  const max = Math.max(1, ...values);
  const minV = Math.min(...values);
  // 人均类指标从 0 起算会糊成一片，按数据区间自适应；总量类仍从 0 起
  const vMin = (minV > 0 && minV > max * 0.35) ? Math.floor(minV) : 0;

  el._mapView = { chart, mapName, columns, rows, onDrill, onBack, opts, metricIdx: idx };

  // 地图上有、数据里没有的区域 = 市辖区（年鉴市县篇不含区级 GDP）
  const registered = ec.getMap(mapName) || {};
  const features = (registered.geoJson || registered.geoJSON || {}).features || [];
  const hasBlankArea = features.length > data.length;

  chart.setOption({
    animationDuration: 320,
    tooltip: {
      trigger: 'item',
      textStyle: { fontSize: 13 },
      formatter: (p) => {
        const v = (p.value === null || p.value === undefined) ? null : p.value;
        const shown = v === null ? '暂无数据（市辖区，年鉴未提供区级 GDP）' : esc(fmtNum(v, fmt));
        return `<b>${esc(p.name)}</b><br/>${esc(label)}：${shown}`;
      },
    },
    graphic: [],
    visualMap: {
      min: vMin, max,
      left: 10, bottom: 10,
      calculable: true,
      itemWidth: 10, itemHeight: 70,
      text: ['高', '低'],
      textStyle: { fontSize: 11, color: '#64748b' },
      inRange: { color: ['#dbeafe', '#93c5fd', '#2563eb'] },
    },
    series: [{
      type: 'map',
      map: mapName,
      roam: true,
      zoom: 1,
      layoutCenter: ['52%', '50%'],
      layoutSize: '104%',
      scaleLimit: { min: 0.8, max: 5 },
      label: {
        show: true,
        fontSize: 11,
        color: '#1e293b',
        textBorderColor: '#ffffff',
        textBorderWidth: 2,
      },
      labelLayout: { hideOverlap: true },
      itemStyle: { areaColor: '#f8fafc', borderColor: '#ffffff', borderWidth: 1 },
      emphasis: {
        label: { show: true, fontSize: 12, color: '#0f172a' },
        itemStyle: { areaColor: '#bfdbfe' },
      },
      select: { disabled: true },
      data,
    }],
  }, true);

  chart.off('click');
  if (onDrill) chart.on('click', onDrill);
  ensureMapBack(el, !!onBack, onBack);
  ensureMapNote(el, hasBlankArea);
  ensureMapMetrics(el, columns, idx, opts.labels, (i) => {
    applyMap(chart, el, mapName, columns, rows, onDrill, onBack, opts, i);
  });
}

/* 指标切换（GDP / 人均 / 人口 / 收入） */
function ensureMapMetrics(el, columns, activeIdx, labels, onPick) {
  let box = el.querySelector('.map-metrics');
  if (columns.length <= 2) {
    if (box) box.remove();
    return;
  }
  if (!box) {
    box = document.createElement('div');
    box.className = 'map-metrics';
    el.appendChild(box);
  }
  box.innerHTML = '';
  columns.slice(1).forEach((c, i) => {
    const btn = document.createElement('button');
    btn.className = 'map-metric' + (i === activeIdx ? ' active' : '');
    btn.textContent = (labels && labels[i]) || String(c.label || '')
      .replace(/^各市/, '').replace(/^县市\/市区/, '');
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      onPick(i);
    });
    box.appendChild(btn);
  });
}

function ensureMapNote(el, visible) {
  let note = el.querySelector('.map-note');
  if (!visible) {
    if (note) note.remove();
    return;
  }
  if (!note) {
    note = document.createElement('div');
    note.className = 'map-note';
    el.appendChild(note);
  }
  note.textContent = '灰色 = 市辖区（年鉴未提供区级 GDP）';
}

async function drillMap(chart, el, cityName, code, opts) {
  let mapName;
  try {
    mapName = await loadGeoDrill(code);
  } catch (e) {
    toast('地图加载失败：' + e.message);
    return;
  }

  const spec = {
    metrics: (opts.drill_metrics && opts.drill_metrics.length)
      ? opts.drill_metrics
      : [opts.drill],
    dimensions: ['region'],
    filters: [{ type: 'dimension', dimension: 'parent_region', op: 'eq', value: cityName }],
  };
  let res;
  try {
    res = await api('/api/run-spec', {
      method: 'POST',
      body: JSON.stringify({ spec, model: opts.model }),
    });
  } catch (e) {
    toast('下钻失败：' + e.message);
    return;
  }
  if (!document.body.contains(el)) return;

  const origin = el._mapOrigin;
  const idx = el._mapView ? el._mapView.metricIdx : 0;
  const back = () => {
    if (origin) renderMap(el, origin.columns, origin.rows, origin.opts, idx);
  };
  applyMap(chart, el, mapName, res.columns, res.result.rows, null, back, opts, idx);

  const n = res.result.rows.length;
  const urban = res.result.rows.filter((r) => String(r[0]).endsWith('市区')).length;
  if (n === 0) {
    toast(cityName + ' 没有可用的县级数据', 4200);
  } else if (n === 1 && urban === 1) {
    toast(cityName + '：各市辖区已合并为 1 块「' + res.result.rows[0][0] + '」', 3400);
  } else {
    toast('已下钻到 ' + cityName + '：市区 + ' + (n - urban) + ' 个县市（共 ' + n + ' 块）', 3200);
  }
}

function ensureMapBack(el, visible, onBack) {
  let btn = el.querySelector('.map-back');
  if (!visible) {
    if (btn) btn.remove();
    return;
  }
  if (!btn) {
    btn = document.createElement('button');
    btn.className = 'map-back';
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (onBack) onBack();
    });
    el.appendChild(btn);
  }
  btn.textContent = '← 返回江苏省';
}

/* ------------------------------------------------------------------ 悬浮提示 */
function bindChartTips(root) {
  (root || document).querySelectorAll('.chart-slot').forEach((el) => {
    if (el.dataset.tipBound) return;
    el.dataset.tipBound = '1';
    el.addEventListener('mousemove', (ev) => {
      const target = ev.target;
      const text = target && target.getAttribute ? target.getAttribute('data-tip') : null;
      const tip = document.getElementById('chartTip');
      if (!tip) return;
      if (!text) { tip.classList.add('hidden'); return; }
      tip.textContent = text;
      tip.classList.remove('hidden');
      const rect = tip.getBoundingClientRect();
      let x = ev.clientX + 14;
      let y = ev.clientY - 38;
      if (x + rect.width > window.innerWidth - 10) x = ev.clientX - rect.width - 14;
      if (y < 8) y = ev.clientY + 18;
      tip.style.left = x + 'px';
      tip.style.top = y + 'px';
    });
    el.addEventListener('mouseleave', () => {
      const tip = document.getElementById('chartTip');
      if (tip) tip.classList.add('hidden');
    });
  });
}
