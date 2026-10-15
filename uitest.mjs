// 前端解析/排版回归测试（node uitest.mjs）
//
// 为什么需要：后端 _build_*_message 生成的文本格式与前端解析强耦合
// （快报的总结行、汇总类的数据行）。后端改了文案而前端没跟着改，
// 表现是"App 里退化成一大坨灰字"，而且不会报错、很难发现。
// 这里用后端真实产出的消息串去跑前端函数，把这条耦合钉住。
import fs from 'node:fs';

const src = fs.readFileSync('static/app.js', 'utf8');

// 从 app.js 里抠出真实实现（不复制粘贴，保证测的和跑的是同一份代码）
function grab(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('app.js 里找不到函数 ' + name);
  let d = 0, started = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') { d++; started = true; }
    else if (src[j] === '}') { d--; if (started && d === 0) return src.slice(i, j + 1); }
  }
  throw new Error('大括号不配对: ' + name);
}

function grabConst(name) {
  const i = src.indexOf('const ' + name + ' =');
  if (i < 0) throw new Error('app.js 里找不到常量 ' + name);
  return src.slice(i, src.indexOf('\n', i));
}

const code = ['esc', 'pct', 'cls', 'parseBriefNote', 'briefSubHTML', 'briefSummaryHTML',
  'parseSummaryLine', 'parseAlert',
  'num', 'fmtHM', 'fmtQuoteTime', 'fmtAmount', 'rangePos', 'periodPct', 'ixZoneText',
  'heatMix', 'heatSquarify', 'heatTextW', 'heatFitLabel']
  .map(grab).join('\n') + '\n' + grabConst('briefEmpty') + '\n' + grabConst('SUMMARY_KINDS');
const M = new Function(code + '\nreturn {esc,pct,cls,parseBriefNote,briefSubHTML,briefSummaryHTML,'
  + 'parseSummaryLine,parseAlert,'
  + 'num,fmtHM,fmtQuoteTime,fmtAmount,rangePos,periodPct,ixZoneText,'
  + 'heatMix,heatSquarify,heatTextW,heatFitLabel};')();

const fail = [];
function ok(label, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + label + (extra ? '  → ' + extra : ''));
  if (!cond) fail.push(label);
}

// 基线样本：与 app._build_intraday_message 的输出格式一一对应
const NOTE = '均值 -0.54% · 领跌 科创50 -1.58% · 领涨 恒生指数 +0.31% · '
  + '板块领涨 医药生物 +2.21%、汽车 +0.87% · 板块领跌 通信 -2.77%、国防军工 -1.75%';
const NOTE_NOSEC = '均值 -0.54% · 领跌 科创50 -1.58% · 领涨 恒生指数 +0.31%';
const JUNK = '这是一行无法解析的说明文字';

// ---- 1. 总结行解析 ----
const b = M.parseBriefNote(NOTE);
ok('均值解析', b.avg === -0.54, String(b.avg));
ok('领跌解析', !!b.worst && b.worst.name === '科创50' && b.worst.pct === -1.58);
ok('领涨解析', !!b.best && b.best.name === '恒生指数' && b.best.pct === 0.31);
ok('板块领涨 2 项', b.lead.length === 2 && b.lead[0].name === '医药生物', JSON.stringify(b.lead));
ok('板块领跌 2 项', b.lag.length === 2 && b.lag[0].name === '通信', JSON.stringify(b.lag));

// ---- 2. 浮层/记录页的紧凑排版 ----
const sub = M.briefSubHTML(NOTE);
ok('紧凑版分两行（结论 / 板块）', (sub.match(/bn-row/g) || []).length === 2);
ok('数值带涨跌色，不是纯灰字', sub.includes('class="up"') && sub.includes('class="down"'));
ok('紧凑版含板块', sub.includes('板块领涨') && sub.includes('板块领跌'));

// ---- 3. 详情页的完整排版 ----
const html = M.briefSummaryHTML(NOTE);
ok('详情版有独立结论块', html.includes('brief-sum'));
ok('详情版含板块分组', html.includes('板块领涨') && html.includes('板块领跌'));
ok('详情版数值带涨跌类名', html.includes('bs-v down') && html.includes('bs-v up'));

// ---- 4. 降级路径（板块挂了 / 格式变了都不能崩） ----
const b2 = M.parseBriefNote(NOTE_NOSEC);
ok('无板块时仍能解析', b2.avg === -0.54 && b2.lead.length === 0 && b2.lag.length === 0);
ok('无板块时紧凑版只有一行', (M.briefSubHTML(NOTE_NOSEC).match(/bn-row/g) || []).length === 1);
ok('无法解析时紧凑版退回原文', M.briefSubHTML(JUNK) === M.esc(JUNK));
ok('无法解析时详情版退回 ad-note', M.briefSummaryHTML(JUNK).includes('ad-note'));

// ---- 5. 指数详情页的数值格式化 / 位置计算 ----
// 这几条是详情页排版的地基：时间格式化错位会把 09:30 显示成 930，
// 区间位置算错则会让"现价点"跑到轨道外面去。
ok('分时时刻补零', M.fmtHM(930) === '09:30' && M.fmtHM(1500) === '15:00', M.fmtHM(930));
ok('分时时刻单个数字', M.fmtHM(5) === '00:05', M.fmtHM(5));
ok('A股行情时间戳', M.fmtQuoteTime('20260918161402') === '2026-09-18 16:14',
  M.fmtQuoteTime('20260918161402'));
ok('港股行情时间戳', M.fmtQuoteTime('2026/09/18 18:31:31') === '2026-09-18 18:31',
  M.fmtQuoteTime('2026/09/18 18:31:31'));
ok('成交额按亿展示', M.fmtAmount(994169450166) === '9941.69 亿', M.fmtAmount(994169450166));
ok('成交额按万亿展示', M.fmtAmount(2500000000000).includes('万亿'), M.fmtAmount(2500000000000));
ok('成交额缺省值', M.fmtAmount(null) === '—' && M.fmtAmount(0) === '—');

const pos = M.rangePos(3911.87, 3888.5, 3919.67);
ok('区间位置计算', pos > 74 && pos < 75, String(pos));
ok('区间位置越界夹紧', M.rangePos(9999, 10, 20) === 100 && M.rangePos(-5, 10, 20) === 0);
ok('区间位置数据缺失返回 null', M.rangePos(null, 10, 20) === null && M.rangePos(15, 20, 10) === null);
ok('区间分位文案', M.ixZoneText(90) === '接近上沿' && M.ixZoneText(50) === '居中'
  && M.ixZoneText(5) === '接近下沿' && M.ixZoneText(null) === '');

// 日K 只取到 6 根时，"近 20 日 / 近一年"必须返回 null 而不是硬算出一个假的区间涨幅
const kl6 = [100, 101, 102, 103, 104, 105].map((c, i) => ({ date: '2026-09-0' + (i + 1), close: c }));
ok('近5日涨跌幅', Math.abs(M.periodPct(kl6, 5) - 5) < 1e-9, String(M.periodPct(kl6, 5)));
ok('历史不足时不编造长周期', M.periodPct(kl6, 20) === null && M.periodPct(kl6, 250) === null);

// ---- 6. 聚合指数提醒（index_batch）必须走"带色带列表"渲染 ----
// 与 app._build_index_batch_message 的输出格式一一对应。后端把单条提醒
// 改成聚合消息后，前端若没把 index_batch 收进 SUMMARY_KINDS，就会退化成
// "一大坨灰字"——不报错，所以在这里钉住。
const BATCH = '⚡ 指数提醒 10:31\n'
  + '· 创业板指 -1.24%，3068.62\n'
  + '· 科创50 -3.66%，1474.04\n'
  + '共 2 个指数超 ±1.00%';
const pb = M.parseAlert({ kind: 'index_batch', message: BATCH });
ok('聚合提醒走列表渲染', pb.type === 'list' && pb.rows.length === 2, JSON.stringify(pb).slice(0, 90));
ok('聚合提醒数据行解析正确',
  pb.rows[0].name === '创业板指' && pb.rows[0].pct === -1.24 && pb.rows[0].extra === '3068.62'
  && pb.rows[1].pct === -3.66, JSON.stringify(pb.rows));
ok('聚合提醒"共 N 个"行进 notes，不被当数据行',
  pb.notes.length === 1 && pb.notes[0].indexOf('共 2 个指数超') === 0, JSON.stringify(pb.notes));
ok('聚合提醒标为下跌（浮层色带用）',
  pb.rows.filter((r) => r.pct < 0).length === 2);

// ---- 7. 板块热力图：布局（squarified treemap）与颜色插值 ----
// 布局坏了会表现为"块叠在一起/跑出画布/面积失真"——不报错但图是坏的，必须钉住。
// 面积守恒 + 不越界 + 面积与权重成正比：这三条是 treemap 正确性的底线。
const ITEMS = [
  { d: { name: '电子' }, v: 100 }, { d: { name: '医药' }, v: 60 },
  { d: { name: '银行' }, v: 30 }, { d: { name: '综合' }, v: 5 },
];
const rects = M.heatSquarify(ITEMS, 300, 200);
ok('treemap 块数守恒', rects.length === ITEMS.length, String(rects.length));
const areaSum = rects.reduce((s, r) => s + r.w * r.h, 0);
ok('treemap 面积守恒（不丢不叠）', Math.abs(areaSum - 300 * 200) < 1, areaSum.toFixed(1));
ok('treemap 不越界', rects.every((r) => r.x >= -1e-6 && r.y >= -1e-6
  && r.x + r.w <= 300 + 1e-6 && r.y + r.h <= 200 + 1e-6));
const ratios = rects.map((r) => Math.max(r.w / r.h, r.h / r.w));
ok('treemap 块长宽比可控（squarified 的意义）',
  Math.max.apply(null, ratios) < 5, Math.max.apply(null, ratios).toFixed(2));
ok('treemap 面积与权重成正比',
  Math.abs(rects[0].w * rects[0].h / (rects[3].w * rects[3].h) - 100 / 5) < 0.5,
  (rects[0].w * rects[0].h / (rects[3].w * rects[3].h)).toFixed(2));
ok('treemap 单块输入不崩', M.heatSquarify([{ d: {}, v: 1 }], 100, 100).length === 1);

ok('颜色插值端点/中点', M.heatMix('#000000', '#ffffff', 0) === '#000000'
  && M.heatMix('#000000', '#ffffff', 1) === '#ffffff'
  && M.heatMix('#000000', '#ffffff', 0.5) === '#808080',
  M.heatMix('#000000', '#ffffff', 0.5));

// ---- 8. 热力图文字适配（用户要求"能放下字的尽量都放下，缩小字号没关系"）----
const f1 = M.heatFitLabel(120, 60, '电子', '-3.37%');
ok('大块两行（名称+数值）', f1 && f1.mode === 'nv' && f1.fs === 11.5, JSON.stringify(f1));
const f2 = M.heatFitLabel(40, 18, '综合', '-2.64%');
ok('中块单行名称', f2 && f2.mode === 'n', JSON.stringify(f2));
const f3 = M.heatFitLabel(26, 30, '电力设备', '-2.91%');
ok('小块名称折两行', f3 && f3.mode === 'w' && f3.a === '电力' && f3.b === '设备',
  JSON.stringify(f3));
ok('折行会缩小字号', f3.fs <= 10, String(f3.fs));
ok('极小块返回 null（留纯色）', M.heatFitLabel(12, 12, '综合', '-2.64%') === null);
ok('宽度估算：中文按字号算', Math.abs(M.heatTextW('综合', 10) - 20) < 1e-6
  && Math.abs(M.heatTextW('ab', 10) - 12.4) < 1e-6, String(M.heatTextW('ab', 10)));
ok('折行优先于缩小数值行（小块不显示数值）',
  M.heatFitLabel(30, 34, '有色金属', '-3.46%').mode === 'w');

console.log(fail.length ? '\n失败 ' + fail.length + ' 项: ' + fail.join(' / ') : '\n前端回归全部通过');
process.exit(fail.length ? 1 : 0);
