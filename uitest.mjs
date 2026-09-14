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

const code = ['esc', 'pct', 'cls', 'parseBriefNote', 'briefSubHTML', 'briefSummaryHTML']
  .map(grab).join('\n') + '\n' + grabConst('briefEmpty');
const M = new Function(code + '\nreturn {esc,pct,cls,parseBriefNote,briefSubHTML,briefSummaryHTML};')();

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

console.log(fail.length ? '\n失败 ' + fail.length + ' 项: ' + fail.join(' / ') : '\n前端回归全部通过');
process.exit(fail.length ? 1 : 0);
