'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

async function api(url, opts) {
  const r = await fetch(url, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || '请求失败');
  return data;
}

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toast._tm);
  toast._tm = setTimeout(() => t.classList.remove('show'), 2600);
}

function num(v, digits = 2) {
  if (v === null || v === undefined || isNaN(v) || v === '') return '—';
  return Number(v).toFixed(digits);
}

function pct(v) {
  if (v === null || v === undefined || isNaN(v) || v === '') return '—';
  const n = Number(v);
  return (n > 0 ? '+' : '') + n.toFixed(2) + '%';
}

function cls(v) {
  if (v === null || v === undefined || isNaN(v) || v === '') return 'flat';
  return v > 0 ? 'up' : (v < 0 ? 'down' : 'flat');
}

const DIR_TEXT = { up: '上涨', down: '下跌', both: '涨跌双向' };
const RULE_TEXT = { daily: '当日涨跌', cumulative: '累计节点' };
const KIND_TEXT = {
  daily: '当日阈值', cum_estimate: '累计估值预警', cum_confirm: '累计净值确认',
  daily_summary: '收盘汇总', index_threshold: '指数阈值', index_summary: '指数汇总',
  us_index_summary: '美股汇总', intraday_brief: '盘中快报',
};

/* ---------------- Tab 切换（底部导航） ---------------- */
$$('.tab').forEach((t) => {
  t.addEventListener('click', () => {
    $$('.tab').forEach((x) => x.classList.remove('active'));
    $$('.tab-panel').forEach((x) => x.classList.remove('active'));
    t.classList.add('active');
    $('#tab-' + t.dataset.tab).classList.add('active');
    window.scrollTo({ top: 0 });
    if (t.dataset.tab === 'alerts') { loadAlerts(); setUnread(0); }
    if (t.dataset.tab === 'rules') loadRules();
    if (t.dataset.tab === 'notify') loadConfig();
  });
});

/* ---------------- 概览统计 ---------------- */
async function loadSummary() {
  try {
    const s = await api('/api/summary');
    $('#sum-funds').textContent = s.fund_count;
    $('#sum-up').textContent = s.up_count;
    $('#sum-down').textContent = s.down_count;
    $('#sum-alerts').textContent = s.today_alerts;
    $('#last-scan').textContent = s.last_scan
      ? '最近更新 ' + s.last_scan.slice(5, 16)
      : '等待数据…';
  } catch (e) { /* 静默 */ }
}

/* ---------------- 指数行情 ---------------- */
let _indices = [];

async function loadIndices() {
  try {
    _indices = await api('/api/indices');
    renderIndices();
    // 指数详情抽屉若开着，跟着刷新实时数字（不重拉历史图：图只跟 min 级变化）
    if (_ixState && $('#ix-sheet').classList.contains('show')) {
      const nx = _indices.find((x) => x.secid === _ixState.ix.secid);
      if (nx) { _ixState.ix = nx; $('#ix-detail').innerHTML = ixDetailHTML(); }
    }
  } catch (e) { /* 静默，保留上次数据 */ }
}

function renderIndices() {
  const box = $('#index-strip');
  if (!_indices.length) {
    box.innerHTML = '<span class="ix-empty">指数加载中…</span>';
    return;
  }
  box.innerHTML = _indices.map((ix, i) => `
    <button class="ix-card" data-ix="${i}">
      <div class="ix-name">${esc(ix.name)}</div>
      <div class="ix-val">${num(ix.price)}</div>
      <div class="ix-chg ${cls(ix.change_pct)}">${pct(ix.change_pct)}</div>
    </button>`).join('');
}

/* ---------------- 指数详情（行情 + 走势图） ----------------
 *
 * 信息层级（重要度自上而下，这是这块代码唯一的设计目标）：
 *   1. 涨跌幅 + 点位          → 唯一的主信息，大字 + 涨跌色
 *   2. 走势图（分时 / 30 日）  → 主视觉，当天有分时数据时默认展示分时
 *   3. 区间位置条             → 「现价处在什么位置」：今日区间 / 近一年区间
 *   4. 今开 / 昨收 / 最高 / 最低 → 常规数值，只有相对昨收才有意义的才上色
 *   5. 振幅 / 成交额          → 盘面特性，统一琥珀色，与涨跌红绿彻底分开
 *   6. 近 5 日 / 20 日 / 一年  → 时间维度对比，数值仍用涨跌色
 *
 * 颜色只承担三件事：涨红跌绿（值）、琥珀（特性）、其余全是中性灰阶。
 * 千万不要把所有格子都涂成同一个颜色——那就又回到「一眼看不出重点」了。
 */
let _ixState = null;        // { ix, data, mode }
const _ixHistCache = {};    // secid -> { ts, data }

function fmtHM(t) {
  const n = Number(t);
  if (!isFinite(n) || n < 0) return '';
  const h = Math.floor(n / 100), m = n % 100;
  return (h < 10 ? '0' + h : '' + h) + ':' + (m < 10 ? '0' + m : '' + m);
}

/** 行情时间戳统一：A股 '20260918161402'、港/美 '2026/09/18 18:31:31' → '2026-09-18 16:14' */
function fmtQuoteTime(raw) {
  const d = String(raw == null ? '' : raw).replace(/\D/g, '');
  if (d.length < 12) return '';
  return d.slice(0, 4) + '-' + d.slice(4, 6) + '-' + d.slice(6, 8) +
    ' ' + d.slice(8, 10) + ':' + d.slice(10, 12);
}

/** 成交额（元）→ 亿 / 万亿 */
function fmtAmount(v) {
  if (v === null || v === undefined || !isFinite(v) || v <= 0) return '—';
  if (v >= 1e12) return (v / 1e12).toFixed(2) + ' 万亿';
  if (v >= 1e8) return (v / 1e8).toFixed(2) + ' 亿';
  return (v / 1e4).toFixed(0) + ' 万';
}

/** 当前值在 [low, high] 中的位置（0~100，越界夹紧；数据不全返回 null） */
function rangePos(cur, low, high) {
  const ok = [cur, low, high].every((x) => typeof x === 'number' && isFinite(x));
  if (!ok || high <= low) return null;
  return Math.max(0, Math.min(100, (cur - low) / (high - low) * 100));
}

/** N 个交易日前的收盘到最新的涨跌幅；历史不够 N 天时返回 null（不编造"近一年"） */
function periodPct(rows, n) {
  if (!rows || rows.length < n + 1 || n < 1) return null;
  const last = rows[rows.length - 1].close;
  const base = rows[rows.length - 1 - n].close;
  if (!base) return null;
  return (last - base) / base * 100;
}

function ixZoneText(pos) {
  if (pos === null) return '';
  if (pos >= 80) return '接近上沿';
  if (pos >= 60) return '偏上';
  if (pos >= 40) return '居中';
  if (pos >= 20) return '偏下';
  return '接近下沿';
}

function ixHeroHTML(ix) {
  const c = cls(ix.change_pct);
  const amt = (ix.change_amt === null || ix.change_amt === undefined)
    ? '' : (ix.change_amt > 0 ? '+' : '') + num(ix.change_amt);
  return `
  <div class="ixs-hero">
    <div class="ixs-hero-l">
      <div class="ixs-name">${esc(ix.name)}<span class="ixs-secid">${esc(ix.secid)}</span></div>
      <div class="ixs-price">${num(ix.price)}</div>
      <div class="ixs-time">${esc(fmtQuoteTime(ix.quote_time))} 行情</div>
    </div>
    <div class="ixs-hero-r">
      <div class="ixs-big ${c}">${pct(ix.change_pct)}</div>
      <div class="ixs-chg-amt ${c}">${amt}</div>
    </div>
  </div>`;
}

/** 区间位置条：灰轨 + 中性填充 + 涨跌色焦点，右侧标注现价所处分位 */
function ixRangeHTML(title, low, high, cur, chg, unit) {
  const pos = rangePos(cur, low, high);
  const p = pos === null ? 50 : pos;
  const bits = unit ? [unit] : [];
  if (low && cur && high) {
    bits.push('距最高 ' + pct((cur - high) / high * 100));
    bits.push('距最低 ' + pct((cur - low) / low * 100));
  }
  return `
  <div class="ixs-block">
    <div class="ixs-block-h">
      <span>${esc(title)}</span>
      <span class="ixs-zone">${ixZoneText(pos)}</span>
    </div>
    <div class="ixs-range">
      <i class="ixs-range-track"></i>
      <i class="ixs-range-fill" style="width:${p.toFixed(1)}%"></i>
      <i class="ixs-range-dot ${cls(chg)}" style="left:${p.toFixed(1)}%"></i>
    </div>
    <div class="ixs-legend">
      <span>最低 <b>${num(low)}</b></span>
      <span class="ixs-now">现价 <b class="${cls(chg)}">${num(cur)}</b></span>
      <span>最高 <b>${num(high)}</b></span>
    </div>
    <div class="ixs-range-meta">${bits.join(' · ')}</div>
  </div>`;
}

function ixStatsHTML(ix) {
  const rel = (v) => ((v === null || v === undefined || !ix.pre_close) ? 'flat' : cls(v - ix.pre_close));
  const cell = (k, v, c) =>
    `<div class="ixs-cell"><span class="k">${k}</span><b class="v ${c || ''}">${num(v)}</b></div>`;
  return `
  <div class="ixs-grid">
    ${cell('今开', ix.open, rel(ix.open))}
    ${cell('昨收', ix.pre_close, '')}
    ${cell('最高', ix.high, rel(ix.high))}
    ${cell('最低', ix.low, rel(ix.low))}
  </div>`;
}

function ixFactsHTML(ix) {
  const items = [];
  if (ix.amplitude !== null && ix.amplitude !== undefined) {
    items.push(`<div class="ixs-fact"><span class="k">振幅</span>`
      + `<b class="v">${Number(ix.amplitude).toFixed(2)}%</b></div>`);
  }
  if (ix.amount) {
    items.push(`<div class="ixs-fact"><span class="k">成交额</span>`
      + `<b class="v">${fmtAmount(ix.amount)}</b></div>`);
  }
  if (!items.length) return '';
  return `<div class="ixs-facts">${items.join('')}</div>`;
}

function ixPeriodHTML(kl) {
  if (!kl || kl.length < 6) return '';
  const cells = [];
  [['近 5 日', 5], ['近 20 日', 20], ['近一年', 250]].forEach((pair) => {
    const v = periodPct(kl, pair[1]);
    if (v === null) return;
    cells.push(`<div class="ixs-period"><span class="k">${pair[0]}</span>`
      + `<b class="v ${cls(v)}">${pct(v)}</b></div>`);
  });
  if (!cells.length) return '';
  return `<div class="ixs-periods">${cells.join('')}</div>`;
}

/** 日K 走势（尾 30 根收盘线）：面积渐变 + 起点基准虚线 + 期间最高/最低点标注 */
function ixDaySVG(rows) {
  if (!rows || rows.length < 2) return `<div class="ixs-empty">暂无走势数据</div>`;
  const W = 460, H = 168, PL = 8, PR = 12, PT = 18, PB = 22;
  const vals = rows.map((r) => r.close);
  let lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
  if (hi - lo < 1e-9) { lo -= 0.01; hi += 0.01; }
  const pad = (hi - lo) * 0.16;
  const ymin = lo - pad, ymax = hi + pad;
  const X = (i) => PL + i / (rows.length - 1) * (W - PL - PR);
  const Y = (v) => PT + (ymax - v) / (ymax - ymin) * (H - PT - PB);
  const first = vals[0], last = vals[vals.length - 1];
  const chg = (last - first) / first * 100;
  const color = chg >= 0 ? '#e0342f' : '#0a9d5c';
  const pts = vals.map((v, i) => X(i).toFixed(1) + ',' + Y(v).toFixed(1)).join(' ');
  const area = PL + ',' + Y(first).toFixed(1) + ' ' + pts + ' '
    + (W - PR) + ',' + Y(first).toFixed(1);
  const hiI = vals.indexOf(Math.max.apply(null, vals));
  const loI = vals.indexOf(Math.min.apply(null, vals));
  const dot = (i) => `<circle cx="${X(i).toFixed(1)}" cy="${Y(vals[i]).toFixed(1)}" r="3"`
    + ` fill="${color}" stroke="#fff" stroke-width="1.3"/>`;
  const yFirst = Y(first).toFixed(1);
  // 图内文字一律加描边：折线随时可能穿过标签，白描边保证任何位置都读得清
  const textOut = ' paint-order="stroke" stroke="#fafbfc" stroke-width="3" stroke-linejoin="round"';
  return `
  <svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="近 30 日走势">
    <defs>
      <linearGradient id="ixdg" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${color}" stop-opacity=".2"/>
        <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
      </linearGradient>
    </defs>
    <polygon points="${area}" fill="url(#ixdg)"/>
    <line x1="${PL}" y1="${yFirst}" x2="${W - PR}" y2="${yFirst}"
      stroke="#c8cdd6" stroke-width="1" stroke-dasharray="4 4"/>
    <text x="${W - PR - 2}" y="${Number(yFirst) - 4}" font-size="9" fill="#9ca3af"
      text-anchor="end"${textOut}>30 日前 ${num(first)}</text>
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.8"
      stroke-linejoin="round" stroke-linecap="round"/>
    ${dot(hiI)}${dot(loI)}
    <circle cx="${X(vals.length - 1).toFixed(1)}" cy="${Y(last).toFixed(1)}" r="3.6"
      fill="${color}" stroke="#fff" stroke-width="1.6"/>
    <text x="${X(hiI).toFixed(1)}" y="${(Y(vals[hiI]) - 7).toFixed(1)}" font-size="9"
      fill="${color}" text-anchor="middle"${textOut}>${num(vals[hiI])}</text>
    <text x="${X(loI).toFixed(1)}" y="${(Y(vals[loI]) + 13).toFixed(1)}" font-size="9"
      fill="${color}" text-anchor="middle"${textOut}>${num(vals[loI])}</text>
    <text x="${PL}" y="${H - 6}" font-size="9" fill="#9ca3af">${esc(rows[0].date.slice(5))}</text>
    <text x="${W - PR}" y="${H - 6}" font-size="9" fill="#9ca3af" text-anchor="end">${esc(rows[rows.length - 1].date.slice(5))}</text>
  </svg>`;
}

/** 分时图：以昨收为对称轴（涨跌幅视觉等宽），上下分别用红/绿填充，右侧标偏离幅度 */
function ixMinuteSVG(rows, preClose) {
  if (!rows || rows.length < 2 || !preClose) return `<div class="ixs-empty">暂无分时数据</div>`;
  const W = 460, H = 168, PL = 8, PR = 52, PT = 16, PB = 22;
  const vals = rows.map((r) => r[1]);
  const spread = Math.max.apply(null,
    [preClose * 0.002].concat(vals.map((v) => Math.abs(v - preClose))));
  const ymax = preClose + spread * 1.12, ymin = preClose - spread * 1.12;
  const X = (i) => PL + i / (rows.length - 1) * (W - PL - PR);
  const Y = (v) => PT + (ymax - v) / (ymax - ymin) * (H - PT - PB);
  const ypc = Y(preClose);
  const last = vals[vals.length - 1];
  const dev = (v) => (v - preClose) / preClose * 100;
  const color = dev(last) >= 0 ? '#e0342f' : '#0a9d5c';
  const line = rows.map((r, i) => X(i).toFixed(1) + ',' + Y(r[1]).toFixed(1)).join(' ');
  const area = X(0).toFixed(1) + ',' + ypc.toFixed(1) + ' ' + line + ' '
    + X(rows.length - 1).toFixed(1) + ',' + ypc.toFixed(1);
  const hiI = vals.indexOf(Math.max.apply(null, vals));
  const loI = vals.indexOf(Math.min.apply(null, vals));
  const midI = Math.floor((rows.length - 1) / 2);
  const dotOf = (i) => {
    const c = dev(vals[i]) >= 0 ? '#e0342f' : '#0a9d5c';
    return `<circle cx="${X(i).toFixed(1)}" cy="${Y(vals[i]).toFixed(1)}" r="3"`
      + ` fill="${c}" stroke="#fff" stroke-width="1.3"/>`;
  };
  const axisText = (i) => (i >= 0 && rows[i]) ? fmtHM(rows[i][0]) : '';
  const textOut = ' paint-order="stroke" stroke="#fafbfc" stroke-width="3" stroke-linejoin="round"';
  return `
  <svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="当日分时走势">
    <defs>
      <clipPath id="ixmu"><rect x="0" y="0" width="${W}" height="${ypc.toFixed(1)}"/></clipPath>
      <clipPath id="ixmd"><rect x="0" y="${ypc.toFixed(1)}" width="${W}" height="${(H - ypc).toFixed(1)}"/></clipPath>
    </defs>
    <polygon points="${area}" fill="#e0342f" opacity=".16" clip-path="url(#ixmu)"/>
    <polygon points="${area}" fill="#0a9d5c" opacity=".16" clip-path="url(#ixmd)"/>
    <line x1="${PL}" y1="${ypc.toFixed(1)}" x2="${W - PR}" y2="${ypc.toFixed(1)}"
      stroke="#c8cdd6" stroke-width="1" stroke-dasharray="4 4"/>
    <polyline points="${line}" fill="none" stroke="${color}" stroke-width="1.6"
      stroke-linejoin="round" stroke-linecap="round"/>
    ${dotOf(hiI)}${dotOf(loI)}
    <circle cx="${X(vals.length - 1).toFixed(1)}" cy="${Y(last).toFixed(1)}" r="3.6"
      fill="${color}" stroke="#fff" stroke-width="1.6"/>
    <text x="${W - PR - 2}" y="${(Number(ypc) - 4).toFixed(1)}" font-size="9" fill="#9ca3af"
      text-anchor="end"${textOut}>昨收 ${num(preClose)}</text>
    <text x="${W - PR + 4}" y="${PT + 3}" font-size="9" fill="#e0342f">+${(spread * 1.12 / preClose * 100).toFixed(2)}%</text>
    <text x="${W - PR + 4}" y="${(Number(ypc) + 3).toFixed(1)}" font-size="9" fill="#9ca3af">0.00%</text>
    <text x="${W - PR + 4}" y="${H - PB}" font-size="9" fill="#0a9d5c">-${(spread * 1.12 / preClose * 100).toFixed(2)}%</text>
    <text x="${PL}" y="${H - 6}" font-size="9" fill="#9ca3af">${axisText(0)}</text>
    <text x="${X(midI).toFixed(1)}" y="${H - 6}" font-size="9" fill="#9ca3af"
      text-anchor="middle">${axisText(midI)}</text>
    <text x="${W - PR}" y="${H - 6}" font-size="9" fill="#9ca3af"
      text-anchor="end">${axisText(rows.length - 1)}</text>
  </svg>`;
}

function ixChartHTML() {
  const st = _ixState;
  if (!st) return '';
  const d = st.data;
  if (!d) return `<div class="ixs-chart"><div class="ixs-empty">走势加载中…</div></div>`;
  const kl = d.kline || [];
  const mp = (d.minute && d.minute.rows) || [];
  const mdate = (d.minute && d.minute.date) || '';
  const hasMinute = mp.length >= 2 && !!mdate;
  const mode = st.mode;
  let stat = '', inner = '';
  if (mode === 'minute' && hasMinute) {
    const chg = dev0(mp[mp.length - 1][1], st.ix.pre_close);
    stat = `${esc(mdate.slice(5))} 全天 <b class="${cls(chg)}">${pct(chg)}</b>`;
    inner = ixMinuteSVG(mp, st.ix.pre_close);
  } else if (kl.length >= 2) {
    const rows = kl.slice(-30);
    const chg = (rows[rows.length - 1].close - rows[0].close) / rows[0].close * 100;
    stat = `${esc(rows[0].date.slice(5))} ~ ${esc(rows[rows.length - 1].date.slice(5))}`
      + ` <b class="${cls(chg)}">${pct(chg)}</b>`;
    inner = ixDaySVG(rows);
  } else {
    inner = `<div class="ixs-empty">该指数暂无走势数据</div>`;
  }
  const seg = `<div class="ixs-seg">`
    + `<button data-ixmode="minute"${mode === 'minute' ? ' class="on"' : ''}`
    + `${hasMinute ? '' : ' disabled'}>分时</button>`
    + `<button data-ixmode="day"${mode === 'day' ? ' class="on"' : ''}`
    + `${kl.length >= 2 ? '' : ' disabled'}>30 日</button></div>`;
  return `
  <div class="ixs-chart">
    <div class="ixs-chart-head">${seg}<div class="ixs-chart-stat">${stat}</div></div>
    ${inner}
  </div>`;
}

function dev0(v, base) {
  if (v === null || v === undefined || !base) return null;
  return (v - base) / base * 100;
}

function todayStr() {
  const n = new Date();
  const p = (x) => (x < 10 ? '0' + x : '' + x);
  return n.getFullYear() + '-' + p(n.getMonth() + 1) + '-' + p(n.getDate());
}

/** 分时数据是今天的（说明今天开过盘）就默认看分时，否则看 30 日 */
function pickIxMode(d) {
  const mp = d && d.minute;
  if (mp && mp.date && mp.date === todayStr() && mp.rows && mp.rows.length >= 2) return 'minute';
  return 'day';
}

function ixDetailHTML() {
  const st = _ixState;
  if (!st) return '';
  const ix = st.ix;
  const d = st.data;
  const kl = (d && d.kline) || [];
  const out = [ixHeroHTML(ix), ixChartHTML()];
  if (ix.low !== null && ix.low !== undefined && ix.high !== null && ix.high !== undefined) {
    out.push(ixRangeHTML('今日区间', ix.low, ix.high, ix.price, ix.change_pct, '当日高低'));
  }
  if (kl.length >= 60) {
    // 近一年区间 = 尾 250 根（约一年交易日）。请求时多拿了一些冗余，
    // 这里必须自己切窗口，否则"近一年"会随请求天数漂移
    const yr = kl.slice(-250);
    let lo = Infinity, hi = -Infinity;
    yr.forEach((r) => {
      if (r.low !== null && r.low !== undefined && r.low < lo) lo = r.low;
      if (r.high !== null && r.high !== undefined && r.high > hi) hi = r.high;
    });
    if (isFinite(lo) && isFinite(hi) && hi > lo) {
      out.push(ixRangeHTML('近一年区间', lo, hi, ix.price, ix.change_pct,
        yr.length + ' 个交易日高低'));
    }
  }
  out.push(ixStatsHTML(ix));
  out.push(ixFactsHTML(ix));
  out.push(ixPeriodHTML(kl));
  if (d && d.errors && (d.errors.kline || d.errors.minute)) {
    const errs = [];
    if (d.errors.kline) errs.push('日K ' + d.errors.kline);
    if (d.errors.minute) errs.push('分时 ' + d.errors.minute);
    out.push(`<div class="ixs-err">${esc(errs.join('；'))}</div>`);
  }
  return out.join('');
}

async function openIxDetail(ix) {
  const hit = _ixHistCache[ix.secid];
  const cached = hit && (Date.now() - hit.ts < 60000);
  _ixState = { ix, data: hit ? hit.data : null, mode: 'day' };
  if (_ixState.data) _ixState.mode = pickIxMode(_ixState.data);
  $('#ix-detail').innerHTML = ixDetailHTML();
  $('#ix-mask').classList.add('show');
  $('#ix-sheet').classList.add('show');
  if (cached) return;   // 60 秒内的缓存直接用，不重复打接口
  try {
    const d = await api('/api/index_history?secid=' + encodeURIComponent(ix.secid) + '&days=300');
    _ixHistCache[ix.secid] = { ts: Date.now(), data: d };
    if (!_ixState || _ixState.ix.secid !== ix.secid) return;   // 期间切到别的指数了
    if (!_ixState.data) { _ixState.data = d; _ixState.mode = pickIxMode(d); }
    else { _ixState.data = d; }
    $('#ix-detail').innerHTML = ixDetailHTML();
  } catch (e) {
    if (!_ixState || _ixState.ix.secid !== ix.secid) return;
    if (_ixState.data) return;   // 已有图就不覆盖成错误态
    _ixState.data = { kline: [], minute: { date: '', rows: [] },
      errors: { kline: e.message, minute: '' } };
    $('#ix-detail').innerHTML = ixDetailHTML();
  }
}

$('#index-strip').addEventListener('click', (e) => {
  const card = e.target.closest('.ix-card');
  if (!card) return;
  const ix = _indices[+card.dataset.ix];
  if (!ix) return;
  openIxDetail(ix);
});

$('#ix-detail').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-ixmode]');
  if (!btn || btn.disabled || !_ixState) return;
  if (btn.dataset.ixmode === _ixState.mode) return;
  _ixState.mode = btn.dataset.ixmode;
  $('#ix-detail').innerHTML = ixDetailHTML();
});

function closeIxSheet() {
  $('#ix-mask').classList.remove('show');
  $('#ix-sheet').classList.remove('show');
}
$('#ix-mask').addEventListener('click', closeIxSheet);

/* ---------------- 监控列表 ---------------- */
async function loadFunds() {
  try {
    const funds = await api('/api/funds');
    renderFunds(funds);
  } catch (e) {
    $('#fund-list').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

function cumProgressHTML(f) {
  const cum = f.cumulative_change_est ?? f.cumulative_change;
  const th = f.cum_threshold;
  if (cum === null || cum === undefined || !th) return '';
  const p = Math.min(Math.abs(cum) / th * 100, 100);
  const done = Math.abs(cum) >= th;
  const dirTxt = cum > 0 ? '涨' : (cum < 0 ? '跌' : '平');
  return `
  <div class="cum-progress">
    <div class="cum-label">
      <span>累计节点 · ${th}%（${dirTxt} ${pct(Math.abs(cum))} / ${th}%）</span>
      <span>${done ? '★ 已达节点' : '剩 ' + pct(th - Math.abs(cum))}</span>
    </div>
    <div class="bar"><div class="bar-fill ${cum < 0 ? 'g' : ''}" style="width:${p}%"></div></div>
  </div>`;
}

function renderFunds(funds) {
  const box = $('#fund-list');
  if (!funds.length) {
    box.innerHTML = '<div class="empty">暂无监控基金<br>在上方输入代码添加吧</div>';
    return;
  }
  box.innerHTML = funds.map((f) => {
    const on = !!f.enabled;
    const cum = f.cumulative_change_est ?? f.cumulative_change;
    const big = f.daily_change;
    const bigCap = f.estimated_change !== null && f.estimated_change !== undefined
      ? '当日涨跌 · 估值' : '当日涨跌';
    return `
    <div class="fund-card ${on ? '' : 'disabled'}" data-code="${f.code}" data-id="${f.id}" data-baseline="${f.baseline_nav ?? ''}">
      <div class="fund-main">
        <div class="fund-head">
          <div>
            <div class="fund-name">${on ? '' : '⏸ '}${esc(f.name || '')}</div>
            <div class="fund-code">${f.code}</div>
          </div>
          <div class="fund-big">
            <div class="num ${cls(big)}">${pct(big)}</div>
            <div class="cap">${bigCap}</div>
          </div>
        </div>
        <div class="fund-grid">
          <div class="cell">
            <div class="k">最新净值</div>
            <div class="v">${num(f.unit_nav, 4)}</div>
          </div>
          <div class="cell">
            <div class="k">${f.estimated_nav ? '估算净值' : '累计净值'}</div>
            <div class="v">${num(f.estimated_nav || f.acc_nav, 4)}</div>
          </div>
          <div class="cell">
            <div class="k">累计涨跌</div>
            <div class="v ${cls(cum)}">${pct(cum)}</div>
          </div>
        </div>
        ${cumProgressHTML(f)}
      </div>
      <div class="fund-detail">
        <div class="chart-box">
          <div class="chart-title"><span>近 30 天净值走势</span><span class="chart-stat"></span></div>
          <div class="chart-holder"></div>
        </div>
        <div class="detail-rows">
          <div class="d-row"><span class="dk">最新净值</span><span class="dv">${num(f.unit_nav, 4)}（${f.nav_date || '—'}）</span></div>
          <div class="d-row"><span class="dk">估算净值</span><span class="dv">${f.estimated_nav ? num(f.estimated_nav, 4) + '（' + (f.gztime || '') + '）' : '—'}</span></div>
          <div class="d-row"><span class="dk">累计涨跌（净值口径）</span><span class="dv ${cls(f.cumulative_change)}">${pct(f.cumulative_change)}</span></div>
          <div class="d-row"><span class="dk">累计涨跌（估值口径）</span><span class="dv ${cls(f.cumulative_change_est)}">${pct(f.cumulative_change_est)}</span></div>
          <div class="d-row"><span class="dk">节点基准</span><span class="dv">${f.baseline_nav ? num(f.baseline_nav, 4) + '（' + (f.baseline_date || '') + '）' : '待初始化'}</span></div>
          <div class="d-row"><span class="dk">节点步长</span><span class="dv">${f.cum_threshold ? f.cum_threshold + '%' : '未设置'}</span></div>
        </div>
        <div class="fund-ops">
          <button data-act="toggle" data-id="${f.id}" data-on="${on ? 1 : 0}">${on ? '暂停监控' : '恢复监控'}</button>
          <button data-act="open-bl" data-code="${f.code}" data-name="${esc(f.name || '')}" data-nav="${f.unit_nav ?? ''}">设置基准</button>
          <button class="warn" data-act="del" data-id="${f.id}">删除</button>
        </div>
      </div>
    </div>`;
  }).join('');
}

/* ---------------- 卡片展开 + 走势图 ---------------- */
const chartCache = {};

$('#fund-list').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-act]');
  if (btn) {
    e.stopPropagation();
    try {
      const id = btn.dataset.id;
      if (btn.dataset.act === 'del') {
        if (!confirm('确认删除该基金？相关监控记录将保留。')) return;
        await api('/api/funds/' + id, { method: 'DELETE' });
        toast('已删除');
      } else if (btn.dataset.act === 'toggle') {
        await api('/api/funds/' + id, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: btn.dataset.on !== '1' }),
        });
        toast('已更新');
      } else if (btn.dataset.act === 'open-bl') {
        // 打开设置基准弹窗，预填基金信息
        openBlSheet(btn.dataset.code, btn.dataset.name, btn.dataset.nav);
        return;  // 不触发 loadFunds 刷新
      }
      // 统一刷新（del/toggle 成功后刷新列表）
      loadFunds(); loadSummary();
    } catch (err) { toast(err.message); }
    return;
  }

  const main = e.target.closest('.fund-main');
  if (!main) return;
  const card = main.closest('.fund-card');
  const code = card.dataset.code;
  const willOpen = !card.classList.contains('open');
  $$('.fund-card.open').forEach((c) => c.classList.remove('open'));
  if (willOpen) {
    card.classList.add('open');
    renderChart(card, code);
  }
});

async function renderChart(card, code) {
  const holder = card.querySelector('.chart-holder');
  const stat = card.querySelector('.chart-stat');
  holder.innerHTML = '<div class="empty" style="padding:20px">加载走势…</div>';
  let hist = chartCache[code];
  if (!hist) {
    try {
      hist = await api(`/api/history?code=${code}&days=30`);
      chartCache[code] = hist;
    } catch (e) {
      holder.innerHTML = `<div class="empty" style="padding:20px">走势加载失败</div>`;
      return;
    }
  }
  if (!hist || hist.length < 2) {
    holder.innerHTML = '<div class="empty" style="padding:20px">暂无足够历史数据</div>';
    return;
  }
  const first = hist[0], last = hist[hist.length - 1];
  const chg = (last.nav - first.nav) / first.nav * 100;
  stat.innerHTML = `<span class="${cls(chg)}">${pct(chg)}</span> · ${hist[0].date.slice(5)} ~ ${hist[hist.length - 1].date.slice(5)}`;
  holder.innerHTML = sparkline(hist, card.dataset.baseline ? parseFloat(card.dataset.baseline) : null);
}

/* SVG 走势图 */
function sparkline(hist, baseline) {
  const W = 420, H = 150, PL = 10, PR = 10, PT = 16, PB = 20;
  const navs = hist.map((h) => h.nav);
  let min = Math.min(...navs), max = Math.max(...navs);
  if (baseline !== null && baseline !== undefined && !isNaN(baseline)) {
    min = Math.min(min, baseline); max = Math.max(max, baseline);
  }
  if (max - min < 1e-9) { min -= 0.01; max += 0.01; }
  const pad = (max - min) * 0.12;
  min -= pad; max += pad;
  const X = (i) => PL + i / (hist.length - 1) * (W - PL - PR);
  const Y = (v) => PT + (max - v) / (max - min) * (H - PT - PB);
  const up = navs[navs.length - 1] >= navs[0];
  const color = up ? '#e0342f' : '#0a9d5c';
  const pts = hist.map((h, i) => `${X(i).toFixed(1)},${Y(h.nav).toFixed(1)}`).join(' ');
  const areaPts = `${PL},${H - PB} ${pts} ${W - PR},${H - PB}`;
  const baseLine = (baseline !== null && baseline !== undefined && !isNaN(baseline))
    ? `<line x1="${PL}" y1="${Y(baseline).toFixed(1)}" x2="${W - PR}" y2="${Y(baseline).toFixed(1)}"
         stroke="#f59e0b" stroke-width="1.2" stroke-dasharray="5 4" opacity=".8"/>
       <text x="${W - PR}" y="${Y(baseline).toFixed(1) - 4}" font-size="9" fill="#f59e0b" text-anchor="end">基准 ${baseline.toFixed(4)}</text>`
    : '';
  const hi = navs.indexOf(Math.max(...navs));
  const lo = navs.indexOf(Math.min(...navs));
  const dot = (i, c) => `<circle cx="${X(i).toFixed(1)}" cy="${Y(navs[i]).toFixed(1)}" r="3" fill="${c}" stroke="#fff" stroke-width="1.2"/>`;
  return `
  <svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <linearGradient id="g${up ? 'u' : 'd'}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${color}" stop-opacity=".22"/>
        <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
      </linearGradient>
    </defs>
    <polygon points="${areaPts}" fill="url(#g${up ? 'u' : 'd'})"/>
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    ${baseLine}
    ${dot(hi, color)}${dot(lo, color)}
    <text x="${X(hi).toFixed(1)}" y="${(Y(navs[hi]) - 7).toFixed(1)}" font-size="9" fill="${color}" text-anchor="middle">${navs[hi].toFixed(4)}</text>
    <text x="${X(lo).toFixed(1)}" y="${(Y(navs[lo]) + 13).toFixed(1)}" font-size="9" fill="${color}" text-anchor="middle">${navs[lo].toFixed(4)}</text>
    <text x="${PL}" y="${H - 6}" font-size="9" fill="#9ca3af">${hist[0].date.slice(5)}</text>
    <text x="${W - PR}" y="${H - 6}" font-size="9" fill="#9ca3af" text-anchor="end">${hist[hist.length - 1].date.slice(5)}</text>
  </svg>`;
}

/* ---------------- 添加基金（弹窗） ---------------- */
function openSheet(id) {
  $('#' + id + '-mask').classList.add('show');
  $('#' + id + '-sheet').classList.add('show');
}
function closeSheet(id) {
  $('#' + id + '-mask').classList.remove('show');
  $('#' + id + '-sheet').classList.remove('show');
}
$('#btn-add-open').addEventListener('click', () => {
  $('#add-code').value = '';
  openSheet('add');
  setTimeout(() => $('#add-code').focus(), 320);
});
$$('[data-sheet]').forEach((btn) => {
  btn.addEventListener('click', () => closeSheet(btn.dataset.sheet));
});
$('#add-mask').addEventListener('click', () => closeSheet('add'));
$('#bl-mask').addEventListener('click', () => closeSheet('bl'));

$('#btn-add').addEventListener('click', async () => {
  const code = $('#add-code').value.trim();
  if (!code) { toast('请输入基金代码'); return; }
  if (!/^\d{6}$/.test(code)) { toast('基金代码为 6 位数字'); return; }
  const btn = $('#btn-add');
  btn.disabled = true; btn.textContent = '添加中…';
  try {
    const r = await api('/api/funds', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    });
    toast(`已添加 ${r.name}（${r.code}），历史走势稍后自动补全`);
    delete chartCache[code];
    closeSheet('add');
    loadFunds(); loadSummary();
  } catch (e) { toast(e.message); }
  finally { btn.disabled = false; btn.textContent = '添加'; }
});
$('#add-code').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') $('#btn-add').click();
});

/* ---------------- 设置基准（弹窗） ---------------- */
function openBlSheet(code, name, curNav) {
  $('#bl-fund-name').textContent = name + '（' + code + '）';
  $('#bl-cur-nav').textContent = curNav ? Number(curNav).toFixed(4) : '—';
  $('#bl-nav').value = curNav || '';
  $('#bl-nav').dataset.code = code;
  openSheet('bl');
  setTimeout(() => $('#bl-nav').focus(), 320);
}

$('#btn-bl-confirm').addEventListener('click', async () => {
  const code = $('#bl-nav').dataset.code;
  const nav = parseFloat($('#bl-nav').value);
  if (!code) { toast('基金信息丢失，请重新打开'); return; }
  if (!nav || nav <= 0) { toast('请输入有效的基准净值'); return; }
  const btn = $('#btn-bl-confirm');
  btn.disabled = true; btn.textContent = '设置中…';
  try {
    const r = await api('/api/baseline', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code, baseline_nav: nav }),
    });
    if (r.triggered > 0) {
      toast('当前已穿越阈值——已触发提醒并重置为当前净值');
    } else {
      toast('基准已设为 ' + nav.toFixed(4) + '，从该点开始累计');
    }
    delete chartCache[code];
    closeSheet('bl');
    loadFunds(); loadSummary();
  } catch (e) { toast(e.message); }
  finally { btn.disabled = false; btn.textContent = '设置并评估'; }
});
$('#bl-nav').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') $('#btn-bl-confirm').click();
});

/* ---------------- 刷新 ---------------- */
$('#btn-refresh').addEventListener('click', async () => {
  const b = $('#btn-refresh');
  b.classList.add('spinning');
  try {
    const r = await api('/api/refresh', { method: 'POST' });
    if (r.fetched < 0) {
      const stageTxt = { fetching: '正在抓取行情', db_cleanup: '正在清理历史',
        http_fetch: '正在抓取行情', inserting: '正在写入数据', evaluating: '正在评估规则',
        alert_insert: '正在记录提醒', notifying: '正在发送通知' }[r.stage] || r.stage || '进行中';
      toast(`上一轮扫描${stageTxt}，请稍候再刷新`);
    } else {
      toast(`已刷新 ${r.fetched} 只基金，触发 ${r.alerts} 条提醒`);
      Object.keys(chartCache).forEach((k) => delete chartCache[k]);
      loadFunds(); loadSummary();
    }
  } catch (e) { toast(e.message); }
  finally { setTimeout(() => b.classList.remove('spinning'), 600); }
});

/* ---------------- 规则设置 ---------------- */
async function loadRules() {
  const rules = await api('/api/rules');
  const gDaily = rules.find((r) => r.scope === 'global' && r.rule_type === 'daily');
  const gCum = rules.find((r) => r.scope === 'global' && r.rule_type === 'cumulative');
  if (gDaily) {
    $('#g-daily-th').value = gDaily.threshold;
    $('#g-daily-on').checked = !!gDaily.enabled;
  }
  if (gCum) {
    $('#g-cum-th').value = gCum.threshold;
    $('#g-cum-dir').value = gCum.direction;
    $('#g-cum-on').checked = !!gCum.enabled;
  }
  // 基金独立规则
  const funds = await api('/api/funds');
  const box = $('#fund-rules');
  if (!funds.length) {
    box.innerHTML = '<div class="empty">请先在「监控」页添加基金</div>';
    return;
  }
  box.innerHTML = funds.map((f) => {
    const fd = rules.filter((r) => r.scope === 'fund' && r.code === f.code && r.rule_type === 'daily');
    const fc = rules.filter((r) => r.scope === 'fund' && r.code === f.code && r.rule_type === 'cumulative');
    const d = fd[0]; const c = fc[0];
    return `
    <div class="fr-item" data-code="${f.code}">
      <div class="fr-head">
        <div><span class="fr-name">${esc(f.name || '')}</span> <span class="fr-code">${f.code}</span></div>
        ${(d || c) ? '<span class="fr-badge">独立规则生效中</span>' : ''}
      </div>
      <div class="fr-row">
        <span class="form-label">当日 ±%</span>
        <input type="number" step="0.1" min="0.1" data-r="daily" value="${d ? d.threshold : ''}" placeholder="跟随全局">
      </div>
      <div class="fr-row">
        <span class="form-label">节点 %</span>
        <input type="number" step="0.1" min="0.1" data-r="cumulative" value="${c ? c.threshold : ''}" placeholder="跟随全局">
      </div>
      <div class="fr-row">
        <span class="form-label">方向</span>
        <select data-r="cum-dir">
          <option value="both" ${c && c.direction === 'both' ? 'selected' : ''}>涨跌双向</option>
          <option value="up" ${c && c.direction === 'up' ? 'selected' : ''}>仅上涨</option>
          <option value="down" ${c && c.direction === 'down' ? 'selected' : ''}>仅下跌</option>
        </select>
      </div>
      <div class="fr-ops">
        <button class="save" data-save-fund>保存</button>
        ${(d || c) ? '<button class="clear" data-clear-fund>清空独立规则</button>' : ''}
      </div>
    </div>`;
  }).join('');
}

$('#btn-save-global').addEventListener('click', async () => {
  try {
    const dailyTh = parseFloat($('#g-daily-th').value);
    const cumTh = parseFloat($('#g-cum-th').value);
    if (!(dailyTh > 0) || !(cumTh > 0)) { toast('阈值需大于 0'); return; }
    await api('/api/rules', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        code: 'GLOBAL', rule_type: 'daily', direction: 'both',
        threshold: dailyTh, enabled: $('#g-daily-on').checked,
      }),
    });
    await api('/api/rules', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        code: 'GLOBAL', rule_type: 'cumulative', direction: $('#g-cum-dir').value,
        threshold: cumTh, enabled: $('#g-cum-on').checked,
      }),
    });
    toast('全局规则已保存');
  } catch (e) { toast(e.message); }
});

$('#fund-rules').addEventListener('click', async (e) => {
  const item = e.target.closest('.fr-item');
  if (!item) return;
  const code = item.dataset.code;
  try {
    if (e.target.closest('[data-save-fund]')) {
      const th = item.querySelector('[data-r="daily"]').value;
      const ct = item.querySelector('[data-r="cumulative"]').value;
      const dir = item.querySelector('[data-r="cum-dir"]').value;
      let saved = 0;
      if (th !== '') {
        await api('/api/rules', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ code, rule_type: 'daily', direction: 'both', threshold: parseFloat(th), enabled: true }),
        });
        saved++;
      }
      if (ct !== '') {
        await api('/api/rules', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ code, rule_type: 'cumulative', direction: dir, threshold: parseFloat(ct), enabled: true }),
        });
        saved++;
      }
      toast(saved ? '基金规则已保存' : '未填写阈值，无改动');
    } else if (e.target.closest('[data-clear-fund]')) {
      const rules = await api('/api/rules');
      const mine = rules.filter((r) => r.scope === 'fund' && r.code === code);
      for (const r of mine) await api('/api/rules/' + r.id, { method: 'DELETE' });
      toast('已清空，恢复跟随全局');
    }
    loadRules();
  } catch (err) { toast(err.message); }
});

/* ---------------- 监控记录 ---------------- */
let alertCode = '';

async function loadAlerts() {
  try {
    const [alerts, funds] = await Promise.all([
      api('/api/alerts?limit=300' + (alertCode ? '&code=' + alertCode : '')),
      api('/api/funds'),
    ]);
    // 筛选 chips
    const row = $('#alert-filter-row');
    const chips = ['<button class="chip' + (alertCode === '' ? ' active' : '') + '" data-code="">全部</button>']
      .concat(funds.map((f) =>
        `<button class="chip${f.code === alertCode ? ' active' : ''}" data-code="${f.code}">${esc(f.name || f.code)}</button>`)).join('');
    row.innerHTML = chips;

    _alertsCache = alerts;
    const box = $('#alert-list');
    if (!alerts.length) {
      box.innerHTML = '<div class="empty">暂无监控记录<br>基金触发预警后会出现在这里</div>';
      return;
    }
    // 类型做成小标签，重点内容放大上色——层级与原设计相反，让人先看到"发生了什么"
    box.innerHTML = alerts.map((a) => {
      const { lead, sub } = alertParts(a);
      return `
      <div class="alert-item ${a.direction === 'up' ? 'up' : (a.direction === 'down' ? 'down' : '')}" data-alert-id="${a.id}">
        <div class="alert-top">
          <span class="alert-kind">${esc(KIND_TEXT[a.kind] || a.kind)}</span>
          <span class="alert-time">${(a.trigger_time || '').slice(5, 16)}</span>
        </div>
        <div class="alert-lead">${lead}</div>
        <div class="alert-sub">${sub}</div>
        <div class="alert-tags">
          <span class="tag">${a.notify_status === 'sent' ? '✓ 已推送' : (a.notify_status === 'failed' ? '推送失败' : a.notify_status)}</span>
        </div>
        <span class="alert-more">查看详情 →</span>
      </div>`;
    }).join('');
  } catch (e) {
    $('#alert-list').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

$('#alert-filter-row').addEventListener('click', (e) => {
  const chip = e.target.closest('.chip');
  if (!chip) return;
  alertCode = chip.dataset.code;
  loadAlerts();
});

/* ---------------- 通知设置 ---------------- */
async function loadConfig() {
  const cfg = await api('/api/config');
  $('#cf-sk').value = cfg.serverchan_sendkey || '';
  $('#cf-pt').value = cfg.pushplus_token || '';
  const e = cfg.email || {};
  $('#cf-host').value = e.smtp_host || '';
  $('#cf-port').value = e.smtp_port || 465;
  $('#cf-ssl').checked = e.use_ssl !== false;
  $('#cf-user').value = e.username || '';
  $('#cf-pass').value = e.password || '';
  $('#cf-to').value = (e.to_addrs || []).join(',');
  $('#cf-in').value = cfg.scan_interval_seconds || 60;
  $('#cf-off').value = cfg.off_hours_interval_seconds || 600;
  const ds = cfg.daily_summary || {};
  $('#cf-sum-time').value = ds.time || '20:00';
  $('#cf-sum-on').checked = !!ds.enabled;
  const ixa = cfg.index_alert || {};
  $('#cf-ixa-th').value = ixa.threshold || 3;
  $('#cf-ixa-on').checked = !!ixa.enabled;
  const ixs = cfg.index_summary || {};
  $('#cf-ixs-time').value = ixs.time || '20:00';
  $('#cf-ixs-on').checked = !!ixs.enabled;
  const usix = cfg.us_index_summary || {};
  $('#cf-usix-time').value = usix.time || '08:00';
  $('#cf-usix-on').checked = !!usix.enabled;
  const brief = cfg.intraday_brief || {};
  $('#cf-brief-slots').value = (brief.slots || ['09:35', '11:30', '14:30']).join(',');
  $('#cf-brief-on').checked = !!brief.enabled;
}

$('#btn-save-cfg').addEventListener('click', async () => {
  try {
    const body = {
      serverchan_sendkey: $('#cf-sk').value.trim(),
      pushplus_token: $('#cf-pt').value.trim(),
      scan_interval_seconds: parseInt($('#cf-in').value) || 60,
      off_hours_interval_seconds: parseInt($('#cf-off').value) || 600,
      daily_summary: {
        enabled: $('#cf-sum-on').checked,
        time: $('#cf-sum-time').value || '20:00',
      },
      index_alert: {
        enabled: $('#cf-ixa-on').checked,
        threshold: parseFloat($('#cf-ixa-th').value) || 3,
      },
      index_summary: {
        enabled: $('#cf-ixs-on').checked,
        time: $('#cf-ixs-time').value || '20:00',
      },
      us_index_summary: {
        enabled: $('#cf-usix-on').checked,
        time: $('#cf-usix-time').value || '08:00',
      },
      intraday_brief: {
        enabled: $('#cf-brief-on').checked,
        slots: $('#cf-brief-slots').value.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean),
      },
      email: {
        smtp_host: $('#cf-host').value.trim(),
        smtp_port: parseInt($('#cf-port').value) || 465,
        use_ssl: $('#cf-ssl').checked,
        username: $('#cf-user').value.trim(),
        password: $('#cf-pass').value,
        from_addr: $('#cf-user').value.trim(),
        to_addrs: $('#cf-to').value.split(',').map((s) => s.trim()).filter(Boolean),
      },
    };
    await api('/api/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    toast('设置已保存');
  } catch (e) { toast(e.message); }
});

$('#btn-test-notify').addEventListener('click', async () => {
  try {
    const b = $('#btn-test-notify');
    b.textContent = '发送中…'; b.disabled = true;
    const r = await api('/api/test_notify', { method: 'POST' });
    let detail = Object.entries(r.channels || {})
      .filter(([k]) => !k.endsWith('_error'))
      .map(([k, v]) => `${k}:${v ? '成功' : '失败'}`).join('，');
    if (r.webpush_detail) {
      detail += `（推送${r.webpush_detail.sent}台${r.webpush_detail.failed ? '·失败' + r.webpush_detail.failed : ''}）`;
    }
    const wpErr = r.webpush_detail && r.webpush_detail.errors && r.webpush_detail.errors[0];
    if (wpErr) {
      // 失败原因持久显示在推送卡片上，避免 toast 一闪而过看不清
      $('#push-status').textContent = '上次推送失败：' + wpErr.error;
      console.warn('[push] 测试推送失败明细', r.webpush_detail);
    }
    // 分段耗时：直接回答「为什么过了很久才收到」是哪个渠道拖的
    if (r.timing) {
      console.info('[push] 各渠道耗时(ms)', r.timing, '合计', r.total_ms);
      const wpSec = ((r.timing.webpush_ms || 0) / 1000).toFixed(1);
      const slow = Object.entries(r.timing)
        .filter(([k, v]) => k !== 'webpush_ms' && v >= 3000)
        .sort((a, b) => b[1] - a[1])[0];
      if (slow && !wpErr) {
        const name = slow[0].replace('_ms', '');
        const errKey = name + '_error';
        const why = r.channels && r.channels[errKey] ? `（${String(r.channels[errKey]).slice(0, 60)}）` : '';
        $('#push-status').textContent =
          `浏览器推送已在 ${wpSec}s 发出。${name} 渠道耗时 ${(slow[1] / 1000).toFixed(1)}s${why}` +
          '，是它拖慢了整体返回，但不影响推送到达时间。';
      }
    }
    toast(r.ok ? '测试成功：' + detail : '发送失败：' + detail);
  } catch (e) { toast(e.message); }
  finally {
    $('#btn-test-notify').textContent = '发送测试提醒';
    $('#btn-test-notify').disabled = false;
  }
});

/* ---------------- 浏览器推送（Web Push） ---------------- */
function urlB64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  const arr = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
  return arr;
}

const PUSH_TIMEOUT = 20000;

function withTimeout(p, ms, label) {
  return Promise.race([
    p,
    new Promise((_, rej) => setTimeout(
      () => rej(new Error((label || '操作') + '超时（' + Math.round(ms / 1000) + ' 秒）')), ms)),
  ]);
}

async function getSwReg() {
  // 关键：sw.js 放在 /static/ 下，默认作用域只有 /static/*，
  // 页面（/）里的 serviceWorker.ready 会永远等待。必须显式指定 scope:'/'，
  // 后端已为 sw.js 返回 Service-Worker-Allowed: / 放行。
  await navigator.serviceWorker.register('/static/sw.js', { scope: '/' });
  return withTimeout(navigator.serviceWorker.ready, PUSH_TIMEOUT, 'Service Worker 就绪');
}

async function subscribePush(onStatus) {
  const say = (t) => { if (onStatus) onStatus(t); };
  say('读取推送密钥…');
  const r = await api('/api/vapid_public_key');
  const reg = await getSwReg();
  say('正在向浏览器推送服务注册…');
  const sub = await withTimeout(
    reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlB64ToUint8Array(r.public_key),
    }), PUSH_TIMEOUT, '订阅推送服务');
  say('保存订阅到服务器…');
  await api('/api/subscribe', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ subscription: sub }),
  });
  return sub;
}

async function unsubscribePush() {
  const reg = await getSwReg();
  const sub = await reg.pushManager.getSubscription();
  if (!sub) return;
  const endpoint = sub.endpoint;
  await sub.unsubscribe();
  await api('/api/unsubscribe', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ endpoint }),
  });
}

/**
 * 填充推送诊断信息。
 * 关键认知：后端 `sent` 只代表 FCM 接收入队，**不代表手机收到了**。
 * 只有 /api/push_status 的 last_ack_at 才是"设备真的收到并弹出"的证据。
 */
async function showPushDiag(endpoint) {
  const status = $('#push-status');
  let info;
  try { info = await api('/api/push_status'); } catch (e) { return; }
  const items = info.items || [];
  const me = items.find((it) => endpoint && endpoint.slice(-10) === it.tail);
  const parts = [`已订阅 ${info.count} 台设备`];
  if (me) {
    parts.push(me.last_ack_at
      ? `本设备最近送达 ${me.last_ack_at.slice(5, 16)}`
      : '本设备尚无送达回执（点「发送测试提醒」验证）');
  }
  status.textContent = parts.join(' · ');
}

async function initPush() {
  const toggle = $('#cf-push');
  const badge = $('#push-badge');
  const status = $('#push-status');
  // 预览面板/iframe 内 Web Push 不可靠（订阅会失败或刷新后丢失），
  // 直接禁用并引导用户用独立浏览器标签页打开。
  if (window.self !== window.top) {
    toggle.disabled = true;
    status.textContent = '当前在预览窗口内，Web Push 不可用。请用 Chrome/Edge 独立打开本页面再开启推送。';
    return;
  }
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    toggle.disabled = true;
    status.textContent = '当前浏览器不支持 Web Push';
    return;
  }
  status.textContent = '正在检查推送状态…';
  try {
    const reg = await getSwReg();
    const sub = await withTimeout(reg.pushManager.getSubscription(), 10000, '读取订阅');
    if (sub) {
      toggle.checked = true;
      badge.textContent = '已订阅';
      badge.classList.add('on');
      status.textContent = '本设备已开启推送，网页关闭也能收到通知';
      showPushDiag(sub.endpoint);
    } else {
      status.textContent = Notification.permission === 'granted'
        ? '通知权限已允许，打开开关即可订阅'
        : '点开关开启系统通知推送';
    }
  } catch (e) {
    console.warn('[push] 初始化失败', e);
    status.textContent = '推送初始化失败：' + ((e && e.message) || e) + '（可刷新页面重试）';
  }
}

$('#cf-push').addEventListener('change', async (e) => {
  const toggle = e.target;
  const badge = $('#push-badge');
  const status = $('#push-status');
  const saying = (t) => { status.textContent = '⏳ ' + t; };
  toggle.disabled = true;
  console.log('[push] 触发，当前权限：', Notification.permission);
  try {
    if (toggle.checked) {
      if (Notification.permission === 'denied') {
        toggle.checked = false;
        status.textContent = 'Chrome 已禁止本站通知。点地址栏左侧「设置/锁」图标 → 网站设置 → 通知 → 允许，然后刷新本页再打开开关。';
        toast('浏览器已禁止本站通知');
        return;
      }
      if (Notification.permission === 'default') {
        saying('请在浏览器地址栏下方弹出的通知权限框中点击【允许】');
        const perm = await Notification.requestPermission();
        console.log('[push] requestPermission ->', perm);
        if (perm !== 'granted') {
          toggle.checked = false;
          status.textContent = '通知权限被拒绝，请在地址栏左侧「网站设置 → 通知」中改为允许，再刷新本页重试';
          toast('通知权限被拒绝');
          return;
        }
      }
      saying('正在订阅推送…');
      const newSub = await subscribePush(saying);
      badge.textContent = '已订阅';
      badge.classList.add('on');
      status.textContent = '已开启。正在向本机发一条验证推送，请留意系统通知栏…';
      toast('推送已开启');
      // 稍等一下让 SW 回执落库，再显示"本设备最近送达"的真实时间
      setTimeout(() => showPushDiag(newSub.endpoint), 4000);
    } else {
      saying('正在取消订阅…');
      await unsubscribePush();
      badge.textContent = '未开启';
      badge.classList.remove('on');
      status.textContent = '点开关开启系统通知推送';
      toast('推送已关闭');
    }
  } catch (err) {
    console.error('[push] 操作失败', err);
    let sub = null;
    try {
      const reg = await withTimeout(navigator.serviceWorker.ready, 5000, '读取注册');
      sub = await withTimeout(reg.pushManager.getSubscription(), 5000, '读取订阅');
    } catch (_) { /* 忽略 */ }
    toggle.checked = !!sub;
    const msg = (err && err.message) ? err.message : '未知错误';
    toast('推送操作失败：' + msg);
    if (!sub) {
      badge.textContent = '未开启';
      badge.classList.remove('on');
      status.textContent = '订阅失败：' + msg + '（若为超时，说明本机网络到浏览器推送服务不通，可换 Edge 浏览器，或改用下方微信推送）';
    } else {
      status.textContent = '本设备已订阅，但刚才的操作失败：' + msg;
    }
  } finally {
    toggle.disabled = false;
  }
});

/* ---------------- 提醒：结构化解析 ---------------- */
let _alertsCache = [];

// 汇总类消息的数据行长这样：
//   · 上证指数 -1.18%，3888.11
//   · 易方达瑞享混合E(001438) +0.58%，净值 9.6893（2026-09-11）
const SUMMARY_KINDS = ['daily_summary', 'index_summary', 'us_index_summary', 'intraday_brief'];

function parseSummaryLine(line) {
  const m = String(line).match(/^·\s*(.+?)\s*([+-]?\d+(?:\.\d+)?)%，\s*(.*)$/);
  if (!m) return null;
  return { name: m[1].trim(), pct: parseFloat(m[2]), extra: (m[3] || '').trim() };
}

function parseAlert(a) {
  const lines = String(a.message || '').split('\n').filter((s) => s.trim());
  if (SUMMARY_KINDS.includes(a.kind)) {
    const body = lines.slice(1);
    const rows = body.map(parseSummaryLine).filter(Boolean);
    // 非数据行的补充说明（如盘中快报末尾的"均值/领跌/板块"那行）。
    // 不单独收集的话会被 filter 掉，在 App 里彻底看不到。
    const notes = body.filter((s) => !parseSummaryLine(s));
    if (rows.length) return { type: 'list', header: lines[0] || '', rows, notes };
  }
  return { type: 'text', header: lines[0] || '' };
}

/**
 * 拆成两段用于分层排版：
 *   lead = 放大上色的重点（几跌几涨 / 核心涨跌幅）
 *   sub  = 补充说明（谁最深最强、阈值多少）
 * 类型名不在这里出现——由调用方渲染成小标签，避免"类型"盖过"内容"。
 */
function alertParts(a) {
  const p = parseAlert(a);
  if (p.type === 'list') {
    const up = p.rows.filter((r) => r.pct > 0).length;
    const down = p.rows.filter((r) => r.pct < 0).length;
    const sorted = p.rows.slice().sort((x, y) => x.pct - y.pct);
    const worst = sorted[0];
    const best = sorted[sorted.length - 1];
    const lead = `<b class="down">${down}</b> 跌 · <b class="up">${up}</b> 涨`;
    // 有补充说明（快报的"均值/领跌/板块"）就直接用它当副文案——那是后端算好的总结，
    // 比前端拼的"共 N 项 · 最深…"信息更全；没有则退回原来的拼法（收盘汇总等仍走这条）
    if (p.notes && p.notes.length) {
      const note = p.notes.join(' · ');
      return { lead, sub: a.kind === 'intraday_brief' ? briefSubHTML(note) : esc(note) };
    }
    let sub = `共 ${p.rows.length} 项`;
    if (worst.pct < 0) sub += ` · 最深 ${esc(worst.name)} <b class="down">${pct(worst.pct)}</b>`;
    if (best.pct > 0) sub += ` · 最强 ${esc(best.name)} <b class="up">${pct(best.pct)}</b>`;
    return { lead, sub };
  }

  // 单条提醒：名称与涨跌幅一起作为重点大字，阈值/节点信息压成说明行
  const raw = String(a.message || '').split('\n')[0];
  const isCum = String(a.kind || '').startsWith('cum');
  let n = a.current_change;
  if (n === null || n === undefined) {
    const m = raw.match(/([+-]?\d+\.\d+)%/);
    n = m ? parseFloat(m[1]) : null;
  }
  const nameHTML = a.name ? `<span class="lead-name">${esc(a.name)}</span> ` : '';
  const lead = n === null
    ? (nameHTML || esc(p.header))
    : `${nameHTML}<b class="${cls(n)}">${pct(n)}</b>`;

  const thr = raw.match(/达到阈值\s*([\d.]+)%/);
  let sub;
  if (thr) {
    sub = `${isCum ? '累计' : '当日'}涨跌达阈值 ${thr[1]}%`;
  } else if (isCum) {
    sub = raw.indexOf('估值预警') >= 0
      ? '累计估值预警 · 收盘净值确认后定基'
      : '累计已达节点 · 基准重置为当前净值';
  } else {
    sub = esc(raw);
  }
  return { lead, sub };
}

/**
 * 拆解盘中快报的总结行。
 * 格式由后端 `app._build_intraday_message` 的最后一行产出，形如：
 *   均值 -0.54% · 领跌 科创50 -1.58% · 领涨 恒生指数 +0.31% ·
 *   板块领涨 医药生物 +2.21%、汽车 +0.87% · 板块领跌 通信 -2.77%
 * 解析失败一律返回空值，调用方会退回原文展示（不要让它抛异常）。
 */
function parseBriefNote(note) {
  const out = { avg: null, worst: null, best: null, lead: [], lag: [] };
  const N = '([+-]?\\d+(?:\\.\\d+)?)%';
  const pair = (s) => {
    const m = String(s).trim().match(new RegExp('^(.+?)\\s+' + N + '$'));
    return m ? { name: m[1].trim(), pct: parseFloat(m[2]) } : null;
  };
  String(note || '').split(' · ').forEach((seg) => {
    const s = seg.trim();
    let m;
    if ((m = s.match(new RegExp('^均值\\s+' + N + '$')))) out.avg = parseFloat(m[1]);
    else if ((m = s.match(new RegExp('^领跌\\s+(.+?)\\s+' + N + '$')))) out.worst = { name: m[1], pct: parseFloat(m[2]) };
    else if ((m = s.match(new RegExp('^领涨\\s+(.+?)\\s+' + N + '$')))) out.best = { name: m[1], pct: parseFloat(m[2]) };
    else if ((m = s.match(/^板块领涨\s+(.+)$/))) out.lead = m[1].split('、').map(pair).filter(Boolean);
    else if ((m = s.match(/^板块领跌\s+(.+)$/))) out.lag = m[1].split('、').map(pair).filter(Boolean);
  });
  return out;
}

const briefEmpty = (b) => b.avg === null && !b.worst && !b.best;

/** 浮层/记录页用的紧凑版：标签灰、名称深色、数值涨红跌绿，分两行 */
function briefSubHTML(note) {
  const b = parseBriefNote(note);
  if (briefEmpty(b)) return esc(note);
  const chip = (label, body) => `<span class="bn"><i>${label}</i>${body}</span>`;
  const val = (v) => `<b class="${cls(v)}">${pct(v)}</b>`;
  const row1 = [];
  if (b.avg !== null) row1.push(chip('均值', val(b.avg)));
  if (b.worst) row1.push(chip('领跌', `${esc(b.worst.name)}${val(b.worst.pct)}`));
  if (b.best) row1.push(chip('领涨', `${esc(b.best.name)}${val(b.best.pct)}`));
  const sec = [];
  if (b.lead.length) sec.push(chip('板块领涨', b.lead.map((x) => `${esc(x.name)}${val(x.pct)}`).join('')));
  if (b.lag.length) sec.push(chip('板块领跌', b.lag.map((x) => `${esc(x.name)}${val(x.pct)}`).join('')));
  return `<span class="bn-row">${row1.join('')}</span>`
    + (sec.length ? `<span class="bn-row bn-sec">${sec.join('')}</span>` : '');
}

/** 详情页用的完整版：分成「均值 / 领跌领涨 / 板块」三块 */
function briefSummaryHTML(note) {
  const b = parseBriefNote(note);
  if (briefEmpty(b)) return `<div class="ad-note">${esc(note)}</div>`;
  const blocks = [];
  if (b.avg !== null) {
    blocks.push(`<div class="bs-line"><span class="bs-k">均值</span>`
      + `<b class="bs-v ${cls(b.avg)}">${pct(b.avg)}</b></div>`);
  }
  const cells = [];
  if (b.worst) {
    cells.push(`<div class="bs-cell"><span class="bs-k">领跌</span>`
      + `<span class="bs-n">${esc(b.worst.name)}</span>`
      + `<b class="bs-v ${cls(b.worst.pct)}">${pct(b.worst.pct)}</b></div>`);
  }
  if (b.best) {
    cells.push(`<div class="bs-cell"><span class="bs-k">领涨</span>`
      + `<span class="bs-n">${esc(b.best.name)}</span>`
      + `<b class="bs-v ${cls(b.best.pct)}">${pct(b.best.pct)}</b></div>`);
  }
  if (cells.length) blocks.push(`<div class="bs-grid">${cells.join('')}</div>`);
  const secOf = (label, arr) => arr.length
    ? `<div class="bs-line bs-secline"><span class="bs-k">${label}</span>`
      + arr.map((x) => `<span class="bs-n">${esc(x.name)}</span>`
        + `<b class="bs-v ${cls(x.pct)}">${pct(x.pct)}</b>`).join('')
      + '</div>'
    : '';
  const sec = secOf('板块领涨', b.lead) + secOf('板块领跌', b.lag);
  if (sec) blocks.push(`<div class="bs-sec-block">${sec}</div>`);
  return `<div class="brief-sum">${blocks.join('')}</div>`;
}

/** 详情：汇总类渲染成带色带的结构化列表；单条渲染成大数字 + 原文 */
function renderAlertDetail(a) {
  const p = parseAlert(a);
  const time = (a.trigger_time || '').slice(0, 16);

  if (p.type === 'list') {
    const up = p.rows.filter((r) => r.pct > 0).length;
    const down = p.rows.filter((r) => r.pct < 0).length;
    const flat = p.rows.length - up - down;
    // 色带以 3% 为满格基准；当天若波动更大则按最大值归一，保证条与条之间可比
    const scale = Math.max(3, ...p.rows.map((r) => Math.abs(r.pct)));
    const sorted = p.rows.slice().sort((x, y) => x.pct - y.pct);
    const total = p.rows.length || 1;
    // 盘中快报把色带加粗，读起来更像柱状图
    const lg = a.kind === 'intraday_brief' ? ' bars-lg' : '';
    return `
      <div class="ad-stats">
        <div class="ad-lead"><b class="down">${down}</b> 跌 · <b class="up">${up}</b> 涨</div>
        <div class="ad-split" aria-hidden="true">
          <i class="up" style="width:${(up / total * 100).toFixed(1)}%"></i>
          <i class="flat" style="width:${(flat / total * 100).toFixed(1)}%"></i>
          <i class="down" style="width:${(down / total * 100).toFixed(1)}%"></i>
        </div>
        <div class="ad-meta">共 ${p.rows.length} 项${flat ? ` · ${flat} 平` : ''} · ${esc(time)}</div>
      </div>
      ${(p.notes && p.notes.length)
        ? (a.kind === 'intraday_brief'
          ? briefSummaryHTML(p.notes.join(' · '))
          : `<div class="ad-note">${esc(p.notes.join(' · '))}</div>`)
        : ''}
      <p class="ad-sec">按涨跌幅排序</p>
      <div class="q-list${lg}">
        ${sorted.map((r) => `
          <div class="q-row">
            <div>
              <div class="q-name">${esc(r.name)}</div>
              <div class="q-track"><i class="q-fill ${cls(r.pct)}" style="width:${Math.min(Math.abs(r.pct) / scale * 100, 100).toFixed(1)}%"></i></div>
            </div>
            <div class="q-pct ${cls(r.pct)}">${pct(r.pct)}</div>
            <div class="q-val">${esc(r.extra)}</div>
          </div>`).join('')}
      </div>`;
  }

  return `
    <div class="ad-hero">
      <div class="ad-hero-l">
        <div class="ad-hero-title">${esc(a.name || '提醒')}</div>
        <div class="ad-hero-sub">${esc(a.code || '')} · ${esc(time)}</div>
      </div>
      ${(a.current_change === null || a.current_change === undefined) ? ''
        : `<div class="ad-hero-big ${cls(a.current_change)}">${pct(a.current_change)}</div>`}
    </div>
    <div class="ad-raw">${esc(a.message || '')}</div>`;
}

function openAlertDetail(a) {
  $('#ad-title').textContent = KIND_TEXT[a.kind] || '提醒详情';
  $('#ad-body').innerHTML = renderAlertDetail(a);
  openSheet('ad');
}

$('#ad-mask').addEventListener('click', () => closeSheet('ad'));

$('#alert-list').addEventListener('click', (e) => {
  const item = e.target.closest('.alert-item');
  if (!item) return;
  const a = _alertsCache.find((x) => String(x.id) === item.dataset.alertId);
  if (a) openAlertDetail(a);
});

/* ---------------- 提醒：顶部浮层 + 新提醒轮询 ---------------- */
const MAX_POPS = 3;
const SEEN_KEY = 'fundwatch.lastAlertId';

let _seenAlertId = null;
try {
  const v = parseInt(localStorage.getItem(SEEN_KEY), 10);
  if (!isNaN(v)) _seenAlertId = v;
} catch (e) { /* 隐私模式下 localStorage 可能不可用，降级为不持久化 */ }

function persistSeen() {
  try { localStorage.setItem(SEEN_KEY, String(_seenAlertId)); } catch (e) { /* 忽略 */ }
}

function dismissPop(el) {
  el.classList.add('leaving');
  setTimeout(() => el.remove(), 260);
}

/** 新提醒从顶部滑入；不打断当前操作，点开才展开详情 */
function showAlertPop(a, delay) {
  const stack = $('#alert-stack');
  if (!stack) return;
  const el = document.createElement('div');
  el.className = 'alert-pop ' + (a.direction === 'up' || a.direction === 'down' ? a.direction : '');
  el.setAttribute('role', 'button');
  el.setAttribute('tabindex', '0');
  el.style.animationDelay = (delay || 0) + 'ms';
  const { lead, sub } = alertParts(a);
  el.innerHTML = `
    <i class="pop-edge"></i>
    <div class="pop-main">
      <div class="pop-head">
        <span class="pop-kind">${esc(KIND_TEXT[a.kind] || a.kind || '提醒')}</span>
        <span class="pop-time">${(a.trigger_time || '').slice(11, 16)}</span>
      </div>
      <div class="pop-lead">${lead}</div>
      <div class="pop-sub${a.kind === 'intraday_brief' ? ' pop-sub-brief' : ''}">${sub}</div>
    </div>
    <button class="pop-close" aria-label="关闭提醒">×</button>`;
  el.addEventListener('click', (ev) => {
    if (ev.target.closest('.pop-close')) { dismissPop(el); return; }
    openAlertDetail(a);
    dismissPop(el);
  });
  el.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' || ev.key === ' ') {
      ev.preventDefault(); openAlertDetail(a); dismissPop(el);
    }
  });
  stack.appendChild(el);
  while (stack.children.length > MAX_POPS) stack.removeChild(stack.firstElementChild);
}

let _unread = 0;
function setUnread(n) {
  _unread = n;
  const dot = $('#alert-dot');
  if (!dot) return;
  if (n > 0) { dot.hidden = false; dot.textContent = n > 99 ? '99+' : String(n); }
  else dot.hidden = true;
}

/** 轮询新提醒并主动弹出；首次访问只建立水位线，不弹历史（否则一开页面就糊一屏旧消息） */
async function pollNewAlerts() {
  let alerts;
  try { alerts = await api('/api/alerts?limit=20'); } catch (e) { return; }
  if (!Array.isArray(alerts) || !alerts.length) return;
  const maxId = Math.max(...alerts.map((a) => a.id || 0));
  if (!maxId) return;

  if (_seenAlertId === null) {          // 首次：静默记录水位线
    _seenAlertId = maxId;
    persistSeen();
    return;
  }
  const fresh = alerts
    .filter((a) => (a.id || 0) > _seenAlertId)
    .sort((x, y) => x.id - y.id);
  _seenAlertId = Math.max(_seenAlertId, maxId);
  persistSeen();
  if (!fresh.length) return;

  fresh.slice(-MAX_POPS).forEach((a, i) => showAlertPop(a, i * 130));
  if ($('#tab-alerts').classList.contains('active')) loadAlerts();
  else setUnread(Math.min(_unread + fresh.length, 99));
}

/* ---------------- 工具 ---------------- */
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/* ---------------- 启动 ---------------- */
loadFunds();
loadSummary();
loadIndices();
initPush();
pollNewAlerts();
setInterval(loadFunds, 60000);
setInterval(loadSummary, 60000);
setInterval(loadIndices, 60000);   // 指数 60 秒刷新（后端有 60s 缓存，不会打接口）
setInterval(pollNewAlerts, 60000); // 每 60 秒检查新提醒，有就从顶部滑入
