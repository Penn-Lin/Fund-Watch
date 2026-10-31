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

import requests
from flask import Flask, jsonify

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


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT', 5099)))
