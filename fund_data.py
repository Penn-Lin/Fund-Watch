# -*- coding: utf-8 -*-
"""数据采集：东方财富移动端基金接口（支持批量）

返回字段说明：
- NAV         单位净值（最新确认）
- ACCNAV      累计净值
- NAVCHGRT    实际涨跌幅（当日，收盘后准确）
- GSZ         估算净值（仅盘中非空）
- GSZZL       估算涨跌幅（仅盘中非空）
- PDATE       净值日期
- GZTIME      估值时间

说明：接口对固定 deviceid 有风控，故每次请求使用随机 UUID，可稳定获取数据。
"""
import time
import uuid
import threading
import concurrent.futures
import requests

FUND_INFO_URL = 'https://fundmobapi.eastmoney.com/FundMNewApi/FundMNFInfo'
HISTORY_URL = 'https://fundmobapi.eastmoney.com/FundMNewApi/FundMNHisNetList'
INDEX_URL = 'https://qt.gtimg.cn/q='
HEADERS = {'User-Agent': 'Mozilla/5.0'}

# ---------- 外部请求保护：硬超时 + 按域名熔断 ----------
#
# 为什么需要（2026-10-15 事故）：requests 的 timeout **管不到 DNS 解析阶段**。
# qt.gtimg.cn 从 Render 解析挂起时，请求会无限期卡住 —— gunicorn 只有 8 个工作
# 线程，几个卡死的请求就把线程占光 → 全站所有接口超时（连 /api/threads 都打不开，
# 因为拿不到线程）；而且线程永远不会自己释放（DNS 阻塞无法被中断）。
# 两层保护：
#   ① 请求丢进独立线程池执行，调用方最多等 hard_timeout 秒（超时返回 None）；
#   ② 按域名熔断：连续 3 次失败后，120 秒内直接快速失败（不发网络请求），
#      避免线程池被"慢失败"慢慢耗干。
# 调用方拿到 None 必须走降级路径（缓存/空结果），绝不能当 Response 用。
_FETCH_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=6, thread_name_prefix='hg-fetch')
_BREAKERS = {}           # host -> {'fails': n, 'until': ts}
_BREAK_LOCK = threading.Lock()
BREAKER_FAILS = 3
BREAKER_COOLDOWN = 120.0


def _host_of(url):
    try:
        return url.split('//', 1)[1].split('/', 1)[0].split(':', 1)[0]
    except Exception:
        return url[:40]


def _http_get(url, headers=None, params=None, timeout=10, hard_timeout=None):
    """带硬超时的 GET；失败/熔断时返回 None（调用方按"拿不到数据"降级处理）"""
    host = _host_of(url)
    with _BREAK_LOCK:
        b = _BREAKERS.get(host)
        if b and b.get('until', 0) > time.time():
            return None
    hard = hard_timeout or (timeout + 10)

    def _do():
        return requests.get(url, params=params, headers=headers, timeout=timeout)

    try:
        fut = _FETCH_POOL.submit(_do)
        resp = fut.result(timeout=hard)
    except Exception:
        with _BREAK_LOCK:
            b = _BREAKERS.setdefault(host, {'fails': 0, 'until': 0.0})
            b['fails'] += 1
            if b['fails'] >= BREAKER_FAILS:
                b['until'] = time.time() + BREAKER_COOLDOWN
                b['fails'] = 0
        return None
    with _BREAK_LOCK:
        _BREAKERS.pop(host, None)
    return resp

# 首页指数版块（腾讯行情 secid：sh/sz=沪/深，hk=港，us=美）
# 按市场分组：前端主页按组分区渲染，/api/indices 每条带 market 字段（cn/hk/us）。
# 加指数只改这里 —— 白名单/名称映射/走势图权限/前端分组全部自动跟随。
INDEX_GROUPS = [
    {'key': 'cn', 'label': 'A股', 'items': [
        ('sh000001', '上证指数'),
        ('sz399001', '深证成指'),
        ('sz399006', '创业板指'),
        ('sh000300', '沪深300'),
        ('sh000688', '科创50'),
    ]},
    {'key': 'hk', 'label': '港股', 'items': [
        ('hkHSI', '恒生指数'),
        ('hkHSTECH', '恒生科技'),
    ]},
    {'key': 'us', 'label': '美股', 'items': [
        ('usINX', '标普500'),      # 腾讯 secid 是 usINX（= .INX），不是 usSPX
        ('usNDX', '纳斯达克100'),
        ('usDJI', '道琼斯'),
    ]},
]
INDEX_LIST = [(s, n) for g in INDEX_GROUPS for s, n in g['items']]
MARKET_BY_SECID = {s: g['key'] for g in INDEX_GROUPS for s, _ in g['items']}

_index_cache = {'ts': 0.0, 'data': []}

# 允许查询走势的指数白名单（secid 会拼进第三方 URL，必须白名单，不能直接透传参数）
ALLOWED_SECIDS = frozenset(s for s, _ in INDEX_LIST)
NAME_BY_SECID = dict(INDEX_LIST)

_last_request = 0.0
_throttle_lock = threading.Lock()
MIN_INTERVAL = 5.0


def _throttle():
    """节流：保证两次请求之间至少间隔 MIN_INTERVAL 秒，避免触发接口限流"""
    global _last_request
    with _throttle_lock:
        wait = MIN_INTERVAL - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()


def _f(v):
    if v is None or v == '' or v == '--':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_funds(codes, retries=2):
    """批量抓取基金数据，返回列表；单个失败不影响其他"""
    if not codes:
        return []
    fcodes = ','.join(str(c) for c in codes)
    for attempt in range(retries):
        try:
            _throttle()
            params = {
                'pageIndex': 1, 'pageSize': 200, 'plat': 'Android', 'appType': 'ttjj',
                'product': 'EFund', 'Version': '1', 'deviceid': str(uuid.uuid4()),
                'Fcodes': fcodes,
            }
            resp = _http_get(FUND_INFO_URL, params=params, headers=HEADERS,
                             timeout=15, hard_timeout=25)
            if resp is None:
                if attempt < retries - 1:
                    time.sleep(5)
                continue
            data = resp.json()
            if data.get('ErrCode') != 0:
                if attempt < retries - 1:
                    time.sleep(5)
                continue
            out = []
            for x in (data.get('Datas') or []):
                out.append({
                    'code': x.get('FCODE'),
                    'name': x.get('SHORTNAME'),
                    'nav_date': x.get('PDATE'),
                    'unit_nav': _f(x.get('NAV')),
                    'acc_nav': _f(x.get('ACCNAV')),
                    'actual_change': _f(x.get('NAVCHGRT')),
                    'estimated_nav': _f(x.get('GSZ')),
                    'estimated_change': _f(x.get('GSZZL')),
                    'gztime': x.get('GZTIME'),
                })
            return out
        except Exception:
            if attempt < retries - 1:
                time.sleep(5)
    return []


def fetch_fund(code):
    lst = fetch_funds([code])
    if not lst or not lst[0].get('name'):
        raise ValueError('获取基金数据失败（代码可能无效，或接口繁忙请稍后重试）')
    return lst[0]


def fetch_history(code, days=30, retries=2):
    """抓取历史净值（按日期降序返回 FSRQ/DWJZ/JZZZL/LJJZ）

    返回按日期升序排列的列表：
    [{'date': '2026-09-04', 'nav': 2.428, 'change': -0.53, 'acc_nav': 2.428}, ...]
    """
    for attempt in range(retries):
        try:
            _throttle()
            params = {
                'FCODE': code, 'pageIndex': 1, 'pageSize': days,
                'plat': 'Android', 'appType': 'ttjj', 'product': 'EFund',
                'Version': '1', 'deviceid': str(uuid.uuid4()),
            }
            resp = _http_get(HISTORY_URL, params=params, headers=HEADERS,
                             timeout=15, hard_timeout=25)
            if resp is None:
                if attempt < retries - 1:
                    time.sleep(5)
                continue
            data = resp.json()
            if data.get('ErrCode') != 0:
                if attempt < retries - 1:
                    time.sleep(5)
                continue
            out = []
            for x in (data.get('Datas') or []):
                d = x.get('FSRQ')
                nav = _f(x.get('DWJZ'))
                if not d or nav is None:
                    continue
                out.append({
                    'date': d, 'nav': nav,
                    'change': _f(x.get('JZZZL')),
                    'acc_nav': _f(x.get('LJJZ')),
                })
            out.sort(key=lambda r: r['date'])  # 升序
            return out
        except Exception:
            if attempt < retries - 1:
                time.sleep(5)
    return []


# 东财行业板块行情（push2）：fid=f3 按涨跌幅排序，po=1 降序 / 0 升序，
# fs=m:90 t:2 是行业板块。板块信息只是快报的"锦上添花"，请求失败一律降级为空。
SECTOR_URL = ('https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=%(n)d&po=%(po)d'
              '&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:2+f:!50&fields=f3,f14')
# push2 是东财的行情 CDN，和 fundmobapi 不是同一套风控：这里给常规浏览器头
# （fundmobapi 那边必须精简 UA，两者策略相反，别互相套用）
SECTOR_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'),
    'Referer': 'https://quote.eastmoney.com/center/boardlist.html',
}


# 腾讯申万一级行业（31 个）。这个接口不支持按涨跌幅排序，但一次就能取全，
# 本地排序反而更准；而且板块名是"医药生物/通信/电子"这种标准一级行业，
# 比东财的细分行业（胶黏剂及胶带…）更适合"主要涨跌板块"这个说法。
TENCENT_SECTOR_URL = ('https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank'
                      '?board_type=hy&sort_type=price&direct=down&offset=0&count=60')


def _sectors_from_tencent(n):
    """返回 (领涨列表, 领跌列表, 错误文本)"""
    try:
        _throttle()
        r = _http_get(TENCENT_SECTOR_URL, headers=HEADERS, timeout=10)
        if r is None:
            return [], [], '请求失败（网络超时或熔断中）'
        body = (r.text or '')[:180]
        try:
            j = r.json()
        except ValueError:
            return [], [], 'HTTP %s · 响应非 JSON · body=%r' % (r.status_code, body)
        rows = []
        for x in ((j.get('data') or {}).get('rank_list') or []):
            name, zdf = x.get('name'), _f(x.get('zdf'))
            if name and zdf is not None:
                rows.append((name, zdf))
        if not rows:
            return [], [], 'HTTP %s · 空列表 · body=%r' % (r.status_code, body)
        rows.sort(key=lambda r: r[1], reverse=True)
        return rows[:n], list(reversed(rows[-n:])), ''
    except Exception as e:
        return [], [], '%s: %s' % (type(e).__name__, str(e)[:200])


def _fetch_sectors_once(po, n=2):
    """东财单边榜单，返回 (rows, err)"""
    try:
        _throttle()
        r = _http_get(SECTOR_URL % {'n': n, 'po': po},
                      headers=SECTOR_HEADERS, timeout=10)
        if r is None:
            return [], '请求失败（网络超时或熔断中）'
        body = (r.text or '')[:180]
        try:
            j = r.json()
        except ValueError:
            return [], 'HTTP %s · 响应非 JSON · body=%r' % (r.status_code, body)
        rows = []
        for x in ((j.get('data') or {}).get('diff') or []):
            name, chg = x.get('f14'), _f(x.get('f3'))
            if name and chg is not None:
                rows.append((name, chg))
        if not rows:
            return [], 'HTTP %s · 空列表 · body=%r' % (r.status_code, body)
        return rows, ''
    except Exception as e:
        return [], '%s: %s' % (type(e).__name__, str(e)[:200])


def _sectors_from_eastmoney(n):
    """返回 (领涨列表, 领跌列表, 错误文本)。东财要两次请求（升降序各一次）"""
    lead, err = _fetch_sectors_once(1, n)
    if err:
        return [], [], '领涨榜 ' + err
    lag, err = _fetch_sectors_once(0, n)
    if err:
        return [], [], '领跌榜 ' + err
    return lead, lag, ''


# 数据源按顺序回退：腾讯优先（qt.gtimg.cn 这一系已证明从 Render 可达），
# 东财 push2 兜底 —— 实测 push2 对海外机房返回空 body，所以它只能当备选。
SECTOR_SOURCES = [('腾讯申万一级', _sectors_from_tencent),
                  ('东财行业板块', _sectors_from_eastmoney)]


def fetch_sectors(n=2):
    """取涨跌幅前 N 的行业板块，返回 (领涨列表, 领跌列表)，每项 (名称, 涨跌幅%)

    板块只是快报的锦上添花：所有数据源都失败就返回空列表，
    绝不能因为板块接口挂了就让快报发不出去。
    """
    for _name, fn in SECTOR_SOURCES:
        lead, lag, err = fn(n)
        if not err and (lead or lag):
            return lead, lag
    return [], []


def probe_sectors(n=3):
    """诊断用：把每个数据源的原始结果与错误都列出来（供 /api/sector_probe）"""
    out = []
    for name, fn in SECTOR_SOURCES:
        lead, lag, err = fn(n)
        out.append({'source': name, 'leading': lead, 'lagging': lag, 'error': err})
    return out


# 板块热力图：与快报同一个腾讯行业接口，但取**全量 31 个行业 + 全量字段**
# （快报只要领涨/领跌前 N）。单位统一成前端友好的"亿"：
# 净流入/成交额原始是万元 → /1e4；流通市值本来就是亿元。
_sector_heat_cache = {'ts': 0.0, 'data': []}
SECTOR_HEAT_TTL = 60.0


def fetch_sector_heat():
    """31 个申万一级行业快照（主页热力图用，纯展示）

    每项：name / chg(涨跌%) / net_in(主力净流入亿) / turnover(成交额亿) /
    mktcap(流通市值亿) / chg5 / chg20 / net_in5 / net_in20 / leader(领涨股) /
    leader_chg。按成交额降序返回，供前端 treemap 直接用。

    失败返回上次缓存（可能为空列表），绝不抛异常 —— 热力图挂了不能影响其它功能。
    """
    now = time.time()
    if _sector_heat_cache['data'] and now - _sector_heat_cache['ts'] < SECTOR_HEAT_TTL:
        return _sector_heat_cache['data']
    try:
        _throttle()
        r = _http_get(TENCENT_SECTOR_URL, headers=HEADERS, timeout=10)
        if r is None:
            return list(_sector_heat_cache['data'])
        j = r.json()
        rows = []
        for x in ((j.get('data') or {}).get('rank_list') or []):
            name, chg = x.get('name'), _f(x.get('zdf'))
            if not name or chg is None:
                continue
            net, to = _f(x.get('zljlr')), _f(x.get('turnover'))
            net5, net20 = _f(x.get('zljlr_d5')), _f(x.get('zljlr_d20'))
            leader = x.get('lzg') or {}
            rows.append({
                'name': name, 'chg': chg,
                'net_in': None if net is None else round(net / 1e4, 2),
                'turnover': None if to is None else round(to / 1e4, 1),
                'mktcap': _f(x.get('ltsz')),
                'chg5': _f(x.get('zdf_d5')),
                'chg20': _f(x.get('zdf_d20')),
                'net_in5': None if net5 is None else round(net5 / 1e4, 2),
                'net_in20': None if net20 is None else round(net20 / 1e4, 2),
                'leader': leader.get('name'),
                'leader_chg': _f(leader.get('zdf')),
            })
        if rows:
            rows.sort(key=lambda r: -(r['turnover'] or 0))
            _sector_heat_cache.update(ts=now, data=rows)
        return _sector_heat_cache['data']
    except Exception:
        return list(_sector_heat_cache['data'])


# ============ 美股行业板块（SPDR 11 个行业 ETF） ============
#
# 与 A 股板块不同：getRank 不支持美股（board_type=us 返回空），改用 SPDR 行业 ETF
# 的行情来代表板块 —— qt.gtimg.cn 直接可取，且**美股收盘后行情冻结**，
# 白天看到的天然就是"昨夜收盘"快照（用户场景：只早上看一眼，不需要实时）。
# 规模 = 份额字段 [71] × 现价 [3]（ETF 专属字段；指数上这个位置无意义，别套用）。
US_SECTOR_ETFS = [
    ('usXLK', '科技'), ('usXLF', '金融'), ('usXLV', '医疗'),
    ('usXLE', '能源'), ('usXLI', '工业'), ('usXLY', '可选消费'),
    ('usXLP', '必需消费'), ('usXLU', '公用事业'), ('usXLRE', '房地产'),
    ('usXLB', '材料'), ('usXLC', '通信'),
]

_us_sector_cache = {'ts': 0.0, 'data': []}
US_SECTOR_TTL = 600.0   # 美股白天数据不变，10 分钟缓存足够


def fetch_us_sector_heat():
    """美股 11 个行业板块快照（按规模降序），供主页热力图「美股」tab

    每项：name / chg(涨跌%) / scale(规模亿美元) / amount(成交额亿美元) /
    price / quote_date(美东日期) / quote_hhmm。
    失败返回上次缓存（可能为空），绝不抛异常。
    """
    now = time.time()
    if _us_sector_cache['data'] and now - _us_sector_cache['ts'] < US_SECTOR_TTL:
        return _us_sector_cache['data']
    try:
        codes = ','.join(s for s, _ in US_SECTOR_ETFS)
        resp = _http_get(INDEX_URL + codes, headers=HEADERS, timeout=10)
        if resp is None:
            return list(_us_sector_cache['data'])
        resp.encoding = 'gbk'
        by_secid = dict(US_SECTOR_ETFS)
        rows = []
        for line in resp.text.split(';'):
            line = line.strip()
            if not line or '=' not in line:
                continue
            key, val = line.split('=', 1)
            key = key.replace('v_', '').strip()
            name = by_secid.get(key)
            if not name:
                continue
            parts = val.strip().strip('"').split('~')
            if len(parts) < 72:
                continue
            price, chg = _f(parts[3]), _f(parts[32])
            shares, amount = _f(parts[71]), _f(parts[37])
            if price is None or chg is None:
                continue
            rows.append({
                'name': name, 'chg': chg,
                'scale': None if shares is None else round(price * shares / 1e8, 1),
                'amount': None if amount is None else round(amount / 1e8, 1),
                'price': price,
                'quote_date': _quote_date(parts[30]),
                'quote_hhmm': _quote_hhmm(parts[30]),
            })
        if rows:
            rows.sort(key=lambda r: -(r['scale'] or 0))
            _us_sector_cache.update(ts=now, data=rows)
        return _us_sector_cache['data']
    except Exception:
        return list(_us_sector_cache['data'])


def _quote_date(raw):
    """从腾讯行情的时间字段解析出 YYYY-MM-DD（静态数据判新鲜度用）

    A 股格式 '20260914133727'，港股格式 '2026/09/14 13:22:28'，
    格式不统一 → 统一"抽数字"，前 8 位即年月日。解析不出返回 None。
    """
    if not raw:
        return None
    digits = ''.join(c for c in str(raw) if c.isdigit())
    if len(digits) < 8:
        return None
    return '%s-%s-%s' % (digits[:4], digits[4:6], digits[6:8])


def _quote_hhmm(raw):
    """从腾讯行情的时间字段解析出"当地方言时间"的 HHMM 整数（如 1715）。

    跨市场含义不同（**这是美股汇总修复的关键**）：
      · A 股 '20261008153800' / 港股 '2026/10/08 15:22:57' → 北京时间；
      · 美股 '2026-10-07 17:15:59' → **美东时间**（字段日期也是美东日期）。

    美股那个字段只在**开盘(美东 09:30)时才把日期翻到当天**，因此收盘后到
    次日开盘前，它一直冻结在"上一个交易日 16:00 之后"的时间；
    而次日开盘瞬间就变成当天 09:3x —— 只看日期翻页会把"刚开盘 4 分钟的
    行情"当成"昨夜收盘"推出去（2026-10-05~10-07 连推三晚就是这么来的）。
    取到 HHMM 才能判"这一场是不是已经收了"。
    解析不出返回 None。
    """
    if not raw:
        return None
    digits = ''.join(c for c in str(raw) if c.isdigit())
    if len(digits) < 12:
        return None
    try:
        h, m = int(digits[8:10]), int(digits[10:12])
    except ValueError:
        return None
    if h > 23 or m > 59:
        return None
    return h * 100 + m


def _index_extra(secid, parts):
    """腾讯 qt 的附加字段。跨市场字段位**不统一**，只取有把握的两个：

    [37] 成交额（万元）—— 仅 A 股口径正确（港股/美股该位是原始数字，无意义）
    [48]/[49] 52 周最高/最低 —— 仅港/美有值；A 股这两位是 -1 和 1.02 之类的垃圾

    其余位（换手、均价）在指数上语义混乱，一律不取，避免把错数据展示给用户。
    """
    out = {'amount': None, 'week52_high': None, 'week52_low': None}
    if secid.startswith(('sh', 'sz')):
        if len(parts) > 37:
            a = _f(parts[37])
            if a is not None and a > 0:
                out['amount'] = a * 1e4          # 万元 → 元
    elif secid.startswith(('hk', 'us')):
        hi = _f(parts[48]) if len(parts) > 48 else None
        lo = _f(parts[49]) if len(parts) > 49 else None
        if hi and lo and hi > lo:
            out['week52_high'] = hi
            out['week52_low'] = lo
    return out


def fetch_indices(max_age=60.0):
    """抓取指数实时行情（腾讯 qt.gtimg.cn 接口，腾讯全球 CDN 海外可达性好）

    腾讯返回格式 v_xxx="字段~分隔..."；关键字段位（A 股/港/美一致）：
      [1]名称 [2]代码 [3]当前价 [4]昨收 [5]今开 [30]行情时间
      [31]涨跌额 [32]涨跌幅% [33]最高 [34]最低
    返回：[{'secid','code','name','price','change_pct','change_amt',
            'open','pre_close','high','low','quote_time','quote_date',
            'quote_hhmm'}, ...]

    quote_date 很重要：收盘后/周末/节假日接口返回的是**上一个交易日**的
    收盘值，change_pct 仍是旧值。调用方必须用它判断"这份行情是不是今天的"，
    否则半夜日期翻页后，旧行情会被当成当日行情重新触发一遍提醒。
    quote_hhmm 见 `_quote_hhmm()`：只有美股用得上（判"这场收了没有"），
    A 股/港股给的是北京时间，可用于诊断。
    """
    now = time.time()
    if _index_cache['data'] and now - _index_cache['ts'] < max_age:
        return _index_cache['data']
    try:
        codes = ','.join(s for s, _ in INDEX_LIST)
        resp = _http_get(INDEX_URL + codes, headers=HEADERS, timeout=10)
        if resp is None:
            return list(_index_cache['data'])
        resp.encoding = 'gbk'  # 腾讯行情接口用 GBK 编码
        out = []
        by_name = dict(INDEX_LIST)
        for line in resp.text.split(';'):
            line = line.strip()
            if not line or '=' not in line:
                continue
            key, val = line.split('=', 1)
            key = key.replace('v_', '').strip()
            val = val.strip().strip('"')
            if not val:
                continue
            parts = val.split('~')
            if len(parts) < 35:
                continue
            name = by_name.get(key) or parts[1] or key
            price = _f(parts[3])
            if price is None:
                continue
            pre_close = _f(parts[4])
            high = _f(parts[33])
            low = _f(parts[34])
            # 振幅自算而不取字段位：[43] 在 A/港/美都是振幅，但口径随市场浮动，
            # 用 (高-低)/昨收 自己算，公式统一、也便于在详情页解释
            amp = None
            if high is not None and low is not None and pre_close:
                amp = (high - low) / pre_close * 100.0
            out.append({
                'secid': key, 'code': parts[2], 'name': name,
                'market': MARKET_BY_SECID.get(key),
                'price': price,
                'change_pct': _f(parts[32]),
                'change_amt': _f(parts[31]),
                'open': _f(parts[5]),
                'pre_close': pre_close,
                'high': high,
                'low': low,
                'amplitude': amp,
                'quote_time': parts[30],
                'quote_date': _quote_date(parts[30]),
                'quote_hhmm': _quote_hhmm(parts[30]),
                **_index_extra(key, parts),
            })
        if out:
            _index_cache.update(ts=now, data=out)
        return out
    except Exception:
        return list(_index_cache['data'])


# ============ 指数走势（日K + 当日分时） ============
#
# 数据源选择：仍走腾讯（web.ifzq.gtimg.cn），和 qt.gtimg.cn 同属腾讯行情 CDN，
# 海外可达性好。东财 push2his 虽然本机实测能通，但它和板块用的 push2 是同一套
# CDN，在 Render 海外机房会 502，所以不启用。
#
# 这两个接口都**不参与预警**，纯展示用，失败一律降级为空数据 + 错误文本，
# 绝不能让"图没画出来"影响到指数提醒本身。
#
# 注意：不走模块级 _throttle()——那个节流是给东财（有风控）用的，
# 这里一次请求要等 5 秒反而会拖慢用户点开抽屉的体验。自带内存缓存即可。

INDEX_KLINE_URL = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
INDEX_MINUTE_URL = 'https://web.ifzq.gtimg.cn/appstock/app/minute/query'

_kline_cache = {}    # secid -> {'ts': float, 'rows': [...]}
_minute_cache = {}   # secid -> {'ts': float, 'date': str, 'rows': [...]}
KLINE_TTL = 300.0    # 日K 5 分钟（当日最后一根盘中会变，5 分钟粒度够）
MINUTE_TTL = 60.0    # 分时跟随首页 60 秒刷新节奏
_CACHE_MAX = 24      # 白名单只有 8 个指数，24 是冗余上限，防内存意外膨胀
_KLINE_DAYS = 300    # 固定按 300 个交易日拉取并缓存，调用方要多少自己切
                     # （缓存键里不带 days，否则 30 天和 300 天的请求会互相污染）


def _cache_put(cache, key, val):
    """写入并做一次裁剪：超上限时丢掉 ts 最小的条目（够用的简易 LRU）"""
    cache[key] = val
    if len(cache) > _CACHE_MAX:
        stale = sorted(cache, key=lambda k: cache[k].get('ts', 0))
        for k in stale[:len(cache) - _CACHE_MAX]:
            cache.pop(k, None)


def _norm_secid(secid):
    s = str(secid or '').strip()
    return s if s in ALLOWED_SECIDS else None


def _tail(rows, n):
    try:
        n = max(5, min(int(n), _KLINE_DAYS))
    except (TypeError, ValueError):
        n = _KLINE_DAYS
    return rows[-n:] if n < len(rows) else rows


def fetch_index_kline(secid, days=_KLINE_DAYS, force=False):
    """指数日K（按日期升序）

    返回 (rows, err)，rows = [{'date','open','close','high','low'}, ...]
    内部固定拉 300 个交易日再按 days 切尾——图只画最近 30 天，但同一份数据
    还要用来算近 5/20/250 日和"近一年区间"。**必须比 250 多**，否则前端算
    250 日涨幅时没有 251 根可用（这个坑踩过一次）。
    失败时返回上次缓存（可能为空）+ 错误文本，不抛异常。
    """
    s = _norm_secid(secid)
    if not s:
        return [], '未知的指数代码 %r' % (secid,)
    hit = _kline_cache.get(s) or {}
    now = time.time()
    if hit.get('rows') and not force and now - hit.get('ts', 0) < KLINE_TTL:
        return _tail(hit['rows'], days), None
    try:
        url = '%s?param=%s,day,,,%d,qfq' % (INDEX_KLINE_URL, s, _KLINE_DAYS)
        r = _http_get(url, headers=HEADERS, timeout=8)
        if r is None:
            return _tail(hit.get('rows') or [], days), '日K请求失败（网络超时或熔断中）'
        j = r.json()
        d = (j.get('data') or {}).get(s) or {}
        raw = d.get('qfqday') or d.get('day') or []
        rows = []
        for x in raw:
            if not isinstance(x, (list, tuple)) or len(x) < 5:
                continue
            close = _f(x[2])
            if close is None:
                continue
            # 元素顺序固定为 [日期, 开, 收, 高, 低, 量]（已实测确认，别按直觉写成 开收高低）
            rows.append({
                'date': str(x[0]), 'open': _f(x[1]), 'close': close,
                'high': _f(x[3]), 'low': _f(x[4]),
            })
        if rows:
            _cache_put(_kline_cache, s, {'ts': now, 'rows': rows})
            return _tail(rows, days), None
        # 空数据（美股指数在这接口只有 1 条）→ 保留旧缓存，附带原因
        return _tail(hit.get('rows') or [], days), '接口无日K数据（code=%s）' % j.get('code')
    except Exception as e:
        return _tail(hit.get('rows') or [], days), '日K请求失败：%s' % e


def fetch_index_minute(secid, force=False):
    """当日分时（每 1 分钟一个点）

    返回 (date, rows, err)：date 形如 '2026-09-18'（**不是**今天时说明是
    上一交易日的分时——周末/节假日接口仍返回最近一个交易日的完整分时，
    前端据此决定默认展示"分时"还是"日K"），rows = [[HHMM 整数, 价格], ...]
    """
    s = _norm_secid(secid)
    if not s:
        return '', [], '未知的指数代码 %r' % (secid,)
    hit = _minute_cache.get(s) or {}
    now = time.time()
    if hit.get('rows') and not force and now - hit.get('ts', 0) < MINUTE_TTL:
        return hit.get('date') or '', hit['rows'], None
    try:
        r = _http_get(INDEX_MINUTE_URL + '?code=' + s, headers=HEADERS, timeout=8)
        if r is None:
            return (hit.get('date') or '', list(hit.get('rows') or []),
                    '分时请求失败（网络超时或熔断中）')
        j = r.json()
        d = (j.get('data') or {}).get(s) or {}
        inner = d.get('data') or {}
        raw_date = str(inner.get('date') or '')
        iso = ('%s-%s-%s' % (raw_date[:4], raw_date[4:6], raw_date[6:8])
               if len(raw_date) >= 8 and raw_date[:8].isdigit() else '')
        rows = []
        for row in (inner.get('data') or []):
            p = str(row).split()
            if len(p) < 2 or len(p[0]) != 4 or not p[0].isdigit():
                continue
            v = _f(p[1])
            if v is None:
                continue
            rows.append([int(p[0]), v])
        if rows:
            _cache_put(_minute_cache, s, {'ts': now, 'date': iso, 'rows': rows})
            return iso, rows, None
        return iso or hit.get('date') or '', list(hit.get('rows') or []), '接口无分时数据'
    except Exception as e:
        return hit.get('date') or '', list(hit.get('rows') or []), '分时请求失败：%s' % e


def fetch_index_history(secid, days=300):
    """给详情页打包：日K + 分时 + 各自的错误（一次请求返回两个系列）

    结构：
      {'secid','name','kline':[...], 'minute':{'date','rows'}, 'errors':{'kline','minute'}}
    kline / minute 任一失败都不影响另一个，前端按"有什么画什么"降级。
    """
    s = _norm_secid(secid)
    if not s:
        raise ValueError('未知的指数代码：%s' % (secid,))
    kl, kl_err = fetch_index_kline(s, days=days)
    mdate, mrows, m_err = fetch_index_minute(s)
    return {
        'secid': s,
        'name': NAME_BY_SECID.get(s, s),
        'kline': kl,
        'minute': {'date': mdate, 'rows': mrows},
        'errors': {'kline': kl_err, 'minute': m_err},
    }


def probe_index(secid, days=30):
    """诊断用（供 /api/index_probe）：逐项列出原始结果与错误"""
    s = _norm_secid(secid)
    if not s:
        return {'secid': secid, 'error': '不在白名单内', 'allowed': sorted(ALLOWED_SECIDS)}
    kl, kl_err = fetch_index_kline(s, days=days, force=True)
    mdate, mrows, m_err = fetch_index_minute(s, force=True)
    return {
        'secid': s, 'name': NAME_BY_SECID.get(s, s),
        'kline_count': len(kl), 'kline_head': kl[:2], 'kline_tail': kl[-2:],
        'kline_error': kl_err,
        'minute_date': mdate, 'minute_count': len(mrows), 'minute_error': m_err,
    }
