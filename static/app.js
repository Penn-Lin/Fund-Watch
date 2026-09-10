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
      }
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

/* ---------------- 添加基金 ---------------- */
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
    $('#add-code').value = '';
    delete chartCache[code];
    loadFunds(); loadSummary();
  } catch (e) { toast(e.message); }
  finally { btn.disabled = false; btn.textContent = '添加'; }
});
$('#add-code').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') $('#btn-add').click();
});

/* ---------------- 刷新 ---------------- */
$('#btn-refresh').addEventListener('click', async () => {
  const b = $('#btn-refresh');
  b.classList.add('spinning');
  try {
    const r = await api('/api/refresh', { method: 'POST' });
    toast(`已刷新 ${r.fetched} 只基金，触发 ${r.alerts} 条提醒`);
    Object.keys(chartCache).forEach((k) => delete chartCache[k]);
    loadFunds(); loadSummary();
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
          ${a.kind === 'daily_summary' ? '' : `
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
    const detail = Object.entries(r.channels || {}).map(([k, v]) => `${k}:${v ? '成功' : '失败'}`).join('，');
    toast(r.ok ? '测试成功：' + detail : '发送失败：' + detail);
  } catch (e) { toast(e.message); }
  finally {
    $('#btn-test-notify').textContent = '发送测试提醒';
    $('#btn-test-notify').disabled = false;
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
setInterval(loadFunds, 60000);
setInterval(loadSummary, 60000);
setInterval(loadIndices, 60000);   // 指数 60 秒刷新（后端有 60s 缓存，不会打接口）
