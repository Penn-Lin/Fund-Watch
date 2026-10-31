"""新浪全球指数转发代理（Render 专用，约 80 行）

存在理由
--------
阿里云 ECS 的机房 IP 被新浪 `hq.sinajs.cn` 全线 403（连 sh000001 都拿不到），
而家用宽带 / 海外节点正常 —— 这是 IP 层封锁，不是接口废弃。
所以由这个跑在 Render（美国）的极简服务取数，阿里云再回来拿。

设计约束
--------
· **绝不碰数据库**：没有 psycopg2、没有 DATABASE_URL，Neon 额度消耗恒为 0
· **不启动调度器**：没有任何后台线程，每次请求处理完即结束
· 免费实例会被回收休眠（15 分钟无请求），冷启动 5~8 秒 —— 后端定时抓取无感
· 只接受固定代码列表，不接受任何参数，避免被当成通用 HTTP 代理滥用

部署
----
Render → New Web Service → 同一个仓库，Root Directory 填 `proxy`
Build: pip install -r requirements.txt
Start: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests
from flask import Flask, jsonify, request

# 36 个市场（新浪 znb_ 前缀）。台湾是 TWJQ 不是 TWII，代码写错接口返回空。
CODES = [
    ('NKY', '日经225', 'apac'), ('KOSPI', '韩国KOSPI', 'apac'),
    ('TWJQ', '台湾加权', 'apac'), ('AS51', '澳洲标普200', 'apac'),
    ('SENSEX', '印度SENSEX', 'apac'), ('FSSTI', '新加坡海峡', 'apac'),
    ('JCI', '印尼雅加达', 'apac'), ('SET', '泰国SET', 'apac'),
    ('PCOMP', '菲律宾综合', 'apac'), ('VNINDEX', '越南胡志明', 'apac'),
    ('NZSE50FG', '新西兰50', 'apac'), ('FBMKLCI', '吉隆坡综合', 'apac'),
    ('UKX', '英国富时100', 'eu'), ('DAX', '德国DAX', 'eu'),
    ('CAC', '法国CAC40', 'eu'), ('SX5E', '欧洲斯托克50', 'eu'),
    ('AEX', '荷兰AEX', 'eu'), ('SMI', '瑞士市场', 'eu'),
    ('IBEX', '西班牙IBEX35', 'eu'), ('FTSEMIB', '意大利富时MIB', 'eu'),
    ('OMX', '瑞典OMX30', 'eu'), ('OSEAX', '挪威OSE', 'eu'),
    ('HEX', '芬兰赫尔辛基', 'eu'), ('ISEQ', '爱尔兰综合', 'eu'),
    ('INDEXCF', '俄罗斯MOEX', 'eu'), ('XU100', '土耳其BIST100', 'eu'),
    ('MADX', '马德里综合', 'eu'),
    ('SPTSX', '加拿大TSX', 'am'), ('IBOV', '巴西IBOVESPA', 'am'),
    ('MEXBOL', '墨西哥BOLSA', 'am'), ('MERVAL', '阿根廷MERVAL', 'am'),
    ('JALSH', '南非综合', 'me'), ('CASE', '埃及EGX', 'me'),
    ('SASEIDX', '沙特TADAWUL', 'me'), ('KSE100', '巴基斯坦卡拉奇', 'me'),
    ('CSEALL', '斯里兰卡科伦坡', 'me'),
]

SINA_URL = 'https://hq.sinajs.cn/list='
HEADERS = {'User-Agent': 'Mozilla/5.0',
           'Referer': 'https://finance.sina.com.cn'}
CACHE_TTL = 20          # 秒：同一时刻多个调用只打一次新浪
_cache = {'ts': 0.0, 'rows': []}

app = Flask(__name__)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _date_ok(v):
    """新浪日期格式混乱（'2026-10-30' / '9/26/2025' / 空），只认 ISO 那种"""
    return bool(v) and len(v) == 10 and v[4:5] == '-' and v[:4].isdigit()


def parse(text):
    """解析新浪 znb_ 应答

    字段（不同市场长度不一致，恒生只有 6 个且无日期）：
      [0]名称 [1]点位 [2]涨跌额 [3]涨跌幅% [4]时间 [5]时间戳 [6]日期
      [7]时间 [8]开盘 [9]昨收 [10]最高 [11]最低
    """
    rows = []
    for line in (text or '').split(';'):
        line = line.strip()
        if '="' not in line:
            continue
        code = line.split('=')[0].replace('var hq_str_znb_', '').strip()
        p = line.split('="', 1)[1].rstrip('";').split(',')
        if len(p) < 4 or not p[1]:
            continue
        price = _f(p[1])
        if price is None:
            continue
        rows.append({
            'code': code,
            'name': p[0],
            'price': price,
            'change': _f(p[2]) if len(p) > 2 else None,
            'change_pct': _f(p[3]) if len(p) > 3 else None,
            'quote_date': p[6] if len(p) > 6 and _date_ok(p[6]) else '',
            'quote_time': p[7] if len(p) > 7 else (p[4] if len(p) > 4 else ''),
            'open': _f(p[8]) if len(p) > 8 else None,
            'prev_close': _f(p[9]) if len(p) > 9 else None,
            'high': _f(p[10]) if len(p) > 10 else None,
            'low': _f(p[11]) if len(p) > 11 else None,
        })
    # 没有日期的市场（恒生等）用同批次最新日期补齐，否则会被当过期行情丢掉
    dates = [r['quote_date'] for r in rows if r['quote_date']]
    if dates:
        best = max(dates)
        for r in rows:
            if not r['quote_date']:
                r['quote_date'] = best
    name_by = {c: n for c, n, _ in CODES}
    mkt_by = {c: m for c, _, m in CODES}
    for r in rows:
        r['name'] = name_by.get(r['code']) or r['name']
        r['market'] = mkt_by.get(r['code'], 'other')
    return rows


def fetch():
    """取一次快照（带进程内缓存，避免并发请求把新浪打爆）"""
    now = time.time()
    if _cache['rows'] and now - _cache['ts'] < CACHE_TTL:
        return _cache['rows']
    url = SINA_URL + ','.join('znb_' + c for c, _, _ in CODES)
    r = requests.get(url, headers=HEADERS, timeout=12)
    r.encoding = 'gbk'
    rows = parse(r.text)
    if rows:
        _cache['rows'] = rows
        _cache['ts'] = now
    return rows


@app.route('/')
@app.route('/api/sina_global')
def sina_global():
    try:
        rows = fetch()
    except Exception as e:
        # 明确返回错误而不是空数组：让阿里云那边能区分"代理挂了"和"新浪没数据"
        return jsonify({'error': type(e).__name__, 'rows': []}), 502
    return jsonify(rows)


@app.route('/health')
def health():
    return jsonify({'ok': True, 'cached': len(_cache['rows']),
                    'age_sec': int(time.time() - _cache['ts']) if _cache['ts'] else None})


# ==================== Yahoo Finance 转发 ====================
#
# 为什么用 Yahoo 而不是只靠新浪：
#   · Yahoo 的 chart API 同时给「最新价 + 昨收 + marketState + 历史 K 线」，
#     有历史就能画走势图（新浪只有快照，详情页永远是"暂无走势数据"）
#   · 覆盖更全（欧洲各国、印度、加拿大…），字段也更规范
# 谁在调：阿里云机房访问 Yahoo 会被按 IP 拦成中文 403 页，所以只能从这里中转。
# 设计：本服务**只做裸转发**，解析和业务计算全在阿里云侧 ——
#   这样解析逻辑只有一份，不会出现"两处解析不同步"（high/low 就这么漏过一次）。
YAHOO_BASE = 'https://query1.finance.yahoo.com'
YAHOO_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'),
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
}
# 白名单：symbol 会拼进第三方 URL，不能透传任意值
YAHOO_ALLOWED = frozenset([
    '^N225', '^KS11', '^TWII', '^AXJO', '^BSESN', '^NSEI', '^STI',
    '^FTSE', '^GDAXI', '^FCHI', '^STOXX50E', '^FTSEMIB', '^IBEX', '^AEX', '^SSMI',
    '^GSPC', '^IXIC', '^DJI', '^GSPTSE', '^BVSP', '^MXX', '^MERV',
    '^HSI', '^HSCE',
])
YAHOO_RANGES = frozenset(['1d', '5d', '1mo', '3mo', '6mo', '1y', '2y', '5y', 'max'])
YAHOO_INTERVALS = frozenset(['1m', '5m', '15m', '30m', '60m', '1d', '1wk'])


def _yahoo_chart(symbol, rng='1d', itv='1d'):
    """取单个 symbol 的 chart 结果（返回 result[0]，失败 None）"""
    url = '%s/v8/finance/chart/%s?interval=%s&range=%s' % (
        YAHOO_BASE, quote(symbol, safe=''), itv, rng)
    try:
        r = requests.get(url, headers=YAHOO_HEADERS, timeout=12)
        j = r.json()
        res = (j.get('chart') or {}).get('result') or []
        return res[0] if res else None
    except Exception:
        return None


@app.route('/api/yahoo_batch')
def yahoo_batch():
    """批量取最新行情：返回 {symbol: meta}（只给 meta，不含 K 线数组，省流量）"""
    raw = (request.args.get('symbols') or '').strip()
    syms = [s for s in raw.split(',') if s]
    if not syms or len(syms) > 24 or any(s not in YAHOO_ALLOWED for s in syms):
        return jsonify({'error': 'bad symbols'}), 400
    out = {}

    def one(sym):
        r0 = _yahoo_chart(sym, '1d', '1d')
        if r0 and r0.get('meta'):
            out[sym] = r0['meta']

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(one, syms))
    if not out:
        return jsonify({'error': 'yahoo unavailable', 'data': {}}), 502
    return jsonify(out)


@app.route('/api/yahoo_history')
def yahoo_history():
    """历史 K 线（给走势图用）：返回 {symbol, range, interval, prev_close, rows:[[date, close]]}"""
    sym = request.args.get('symbol', '')
    rng = request.args.get('range', '1y')
    itv = request.args.get('interval', '1d')
    if sym not in YAHOO_ALLOWED:
        return jsonify({'error': 'symbol not allowed'}), 400
    if rng not in YAHOO_RANGES or itv not in YAHOO_INTERVALS:
        return jsonify({'error': 'bad params'}), 400
    r0 = _yahoo_chart(sym, rng, itv)
    if not r0:
        return jsonify({'error': 'yahoo unavailable', 'rows': []}), 502
    meta = r0.get('meta') or {}
    ts = r0.get('timestamp') or []
    q = ((r0.get('indicators') or {}).get('quote') or [{}])[0]
    closes = q.get('close') or []
    rows = []
    for i, t in enumerate(ts):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        rows.append([time.strftime('%Y-%m-%d', time.gmtime(t + 8 * 3600)), round(float(c), 4)])
    return jsonify({
        'symbol': sym, 'range': rng, 'interval': itv,
        'prev_close': meta.get('chartPreviousClose') or meta.get('previousClose'),
        'market_state': meta.get('marketState'),
        'rows': rows,
    })


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT', 5099)))
