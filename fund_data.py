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
INDEX_URL = 'https://push2.eastmoney.com/api/qt/ulist.np/get'
HEADERS = {'User-Agent': 'Mozilla/5.0'}

# 首页指数版块（secid 前缀：1=沪 0=深 100=港 105=纳斯达克 106=纽交所）
INDEX_LIST = [
    ('1.000001', '上证指数'),
    ('0.399001', '深证成指'),
    ('0.399006', '创业板指'),
    ('1.000300', '沪深300'),
    ('1.000688', '科创50'),
    ('100.HSI', '恒生指数'),
    ('105.NDX', '纳斯达克100'),
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


def fetch_indices(max_age=60.0):
    """批量抓取指数实时行情（push2 接口），内存缓存 max_age 秒

    返回：[{'secid','code','name','price','change_pct','change_amt',
            'open','pre_close','high','low'}, ...]
    """
    now = time.time()
    if _index_cache['data'] and now - _index_cache['ts'] < max_age:
        return _index_cache['data']
    try:
        params = {
            'fltt': 2, 'invt': 2,
            'secids': ','.join(s for s, _ in INDEX_LIST),
            'fields': 'f2,f3,f4,f12,f13,f14,f15,f16,f17,f18',
        }
        resp = requests.get(INDEX_URL, params=params, headers=HEADERS, timeout=10)
        data = resp.json()
        out = []
        by_name = dict(INDEX_LIST)
        for x in ((data.get('data') or {}).get('diff') or []):
            secid = '%s.%s' % (x.get('f13'), x.get('f12'))
            name = by_name.get(secid) or x.get('f14') or secid
            price = _f(x.get('f2'))
            if price is None:
                continue
            out.append({
                'secid': secid, 'code': x.get('f12'), 'name': name,
                'price': price,
                'change_pct': _f(x.get('f3')),
                'change_amt': _f(x.get('f4')),
                'open': _f(x.get('f17')),
                'pre_close': _f(x.get('f18')),
                'high': _f(x.get('f15')),
                'low': _f(x.get('f16')),
            })
        if out:
            _index_cache.update(ts=now, data=out)
        return out
    except Exception:
        return list(_index_cache['data'])
