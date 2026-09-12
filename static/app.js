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
  us_index_summary: '美股汇总',
};

/* ---------------- Tab 切换（底部导航） ---------------- */
$$('.tab').forEach((t) => {
  t.addEventListener('click', () => {
    $$('.tab').forEach((x) => x.classList.remove('active'));
    $$('.tab-panel').forEach((x) => x.classList.remove('active'));
    t.classList.add('active');
    $('#tab-' + t.dataset.tab).classList.add('active');
    window.scrollTo({ top: 0 });
    if (t.dataset.tab === 'alerts') loadAlerts();
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

$('#index-strip').addEventListener('click', (e) => {
  const card = e.target.closest('.ix-card');
  if (!card) return;
  const ix = _indices[+card.dataset.ix];
  if (!ix) return;
  const rows = [
    ['今开', num(ix.open)], ['昨收', num(ix.pre_close)],
    ['最高', num(ix.high)], ['最低', num(ix.low)],
    ['涨跌额', (ix.change_amt > 0 ? '+' : '') + num(ix.change_amt)],
  ];
  $('#ix-detail').innerHTML = `
    <div class="ix-sheet-head">
      <div>
        <div class="ix-sheet-name">${esc(ix.name)}</div>
        <div class="ix-sheet-code">${esc(ix.code)}</div>
      </div>
      <div class="ix-sheet-num">
        <div class="big ${cls(ix.change_pct)}">${pct(ix.change_pct)}</div>
        <div class="sub ${cls(ix.change_pct)}">${num(ix.price)}</div>
      </div>
    </div>
    <div class="ix-sheet-grid">
      ${rows.map(([k, v]) => `<div class="cell"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('')}
    </div>`;
  $('#ix-mask').classList.add('show');
  $('#ix-sheet').classList.add('show');
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

    const box = $('#alert-list');
    if (!alerts.length) {
      box.innerHTML = '<div class="empty">暂无监控记录<br>基金触发预警后会出现在这里</div>';
      return;
    }
    box.innerHTML = alerts.map((a) => `
      <div class="alert-item ${a.direction === 'up' ? 'up' : (a.direction === 'down' ? 'down' : '')}">
        <div class="alert-top">
          <span class="alert-title">${esc(a.name || '')} <span class="fr-code">${a.code}</span></span>
          <span class="alert-time">${(a.trigger_time || '').slice(5, 16)}</span>
        </div>
        <div class="alert-msg">${esc(a.message || '')}</div>
        <div class="alert-tags">
          <span class="tag">${KIND_TEXT[a.kind] || a.kind}</span>
          ${['daily_summary', 'index_summary', 'us_index_summary'].includes(a.kind) ? '' : `
          <span class="tag ${cls(a.current_change)}">${pct(a.current_change)}</span>`}
          <span class="tag">${a.notify_status === 'sent' ? '✓ 已推送' : (a.notify_status === 'failed' ? '推送失败' : a.notify_status)}</span>
        </div>
      </div>`).join('');
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
      .filter(([k]) => !k.startsWith('_'))
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
      await subscribePush(saying);
      badge.textContent = '已订阅';
      badge.classList.add('on');
      status.textContent = '本设备已开启推送，网页关闭也能收到通知';
      toast('推送已开启');
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
setInterval(loadFunds, 60000);
setInterval(loadSummary, 60000);
setInterval(loadIndices, 60000);   // 指数 60 秒刷新（后端有 60s 缓存，不会打接口）
