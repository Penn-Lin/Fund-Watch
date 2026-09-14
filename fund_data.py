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


def fetch_sectors(n=2):
    """取行业板块涨跌幅前 N，返回 (领涨列表, 领跌列表)，每项 (名称, 涨跌幅%)

    任何异常都返回空列表 —— 快报不能因为板块接口挂了就发不出去。
    """
    out = []
    for po in (1, 0):
        got = []
        try:
            _throttle()
            r = requests.get(SECTOR_URL % {'n': n, 'po': po}, headers=HEADERS, timeout=8)
            diff = ((r.json().get('data') or {}).get('diff') or [])
            for x in diff:
                name, chg = x.get('f14'), _f(x.get('f3'))
                if name and chg is not None:
                    got.append((name, chg))
        except Exception:
            got = []
        out.append(got)
    return out[0], out[1]


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
