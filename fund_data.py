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
import requests

FUND_INFO_URL = 'https://fundmobapi.eastmoney.com/FundMNewApi/FundMNFInfo'
HISTORY_URL = 'https://fundmobapi.eastmoney.com/FundMNewApi/FundMNHisNetList'
INDEX_URL = 'https://qt.gtimg.cn/q='
HEADERS = {'User-Agent': 'Mozilla/5.0'}

# 首页指数版块（腾讯行情 secid：sh/sz=沪/深，hk=港，us=美）
INDEX_LIST = [
    ('sh000001', '上证指数'),
    ('sz399001', '深证成指'),
    ('sz399006', '创业板指'),
    ('sh000300', '沪深300'),
    ('sh000688', '科创50'),
    ('hkHSI', '恒生指数'),
    ('hkHSTECH', '恒生科技'),
    ('usNDX', '纳斯达克100'),
]

_index_cache = {'ts': 0.0, 'data': []}

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
            resp = requests.get(FUND_INFO_URL, params=params, headers=HEADERS, timeout=15)
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
            resp = requests.get(HISTORY_URL, params=params, headers=HEADERS, timeout=15)
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
        r = requests.get(TENCENT_SECTOR_URL, headers=HEADERS, timeout=10)
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
        r = requests.get(SECTOR_URL % {'n': n, 'po': po},
                         headers=SECTOR_HEADERS, timeout=10)
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


def fetch_indices(max_age=60.0):
    """抓取指数实时行情（腾讯 qt.gtimg.cn 接口，腾讯全球 CDN 海外可达性好）

    腾讯返回格式 v_xxx="字段~分隔..."；关键字段位（A 股/港/美一致）：
      [1]名称 [2]代码 [3]当前价 [4]昨收 [5]今开 [30]行情时间
      [31]涨跌额 [32]涨跌幅% [33]最高 [34]最低
    返回：[{'secid','code','name','price','change_pct','change_amt',
            'open','pre_close','high','low','quote_time','quote_date'}, ...]

    quote_date 很重要：收盘后/周末/节假日接口返回的是**上一个交易日**的
    收盘值，change_pct 仍是旧值。调用方必须用它判断"这份行情是不是今天的"，
    否则半夜日期翻页后，旧行情会被当成当日行情重新触发一遍提醒。
    """
    now = time.time()
    if _index_cache['data'] and now - _index_cache['ts'] < max_age:
        return _index_cache['data']
    try:
        codes = ','.join(s for s, _ in INDEX_LIST)
        resp = requests.get(INDEX_URL + codes, headers=HEADERS, timeout=10)
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
            out.append({
                'secid': key, 'code': parts[2], 'name': name,
                'price': price,
                'change_pct': _f(parts[32]),
                'change_amt': _f(parts[31]),
                'open': _f(parts[5]),
                'pre_close': _f(parts[4]),
                'high': _f(parts[33]),
                'low': _f(parts[34]),
                'quote_time': parts[30],
                'quote_date': _quote_date(parts[30]),
            })
        if out:
            _index_cache.update(ts=now, data=out)
        return out
    except Exception:
        return list(_index_cache['data'])
