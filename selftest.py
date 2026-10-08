# -*- coding: utf-8 -*-
"""部署前自测：SQLite 业务回归 + PgConn SQL 方言转换正确性"""
import os
import sys

# ---------- 1. SQLite 模式业务回归 ----------
os.environ.pop('DATABASE_URL', None)
import database
database.init_db()

conn = database.get_conn()
# 模拟 fund + nav 写入
conn.execute('DELETE FROM funds WHERE code=?', ('999999',))
conn.execute('DELETE FROM nav_history WHERE code=?', ('999999',))
cur = conn.execute('INSERT INTO funds (code, name, enabled, created_at) VALUES (?,?,?,?)',
                   ('999999', '测试基金', 1, '2026-09-07 12:00:00'))
fid = cur.lastrowid
cur2 = conn.execute(
    'INSERT INTO nav_history (code, nav_date, unit_nav, actual_change, fetched_at) '
    'VALUES (?,?,?,?,?)', ('999999', '2026-09-04', 1.0, 1.5, '2026-09-07 12:00:00'))
assert fid and cur2.lastrowid, 'sqlite lastrowid 失败'

row = conn.execute('SELECT * FROM funds WHERE code=?', ('999999',)).fetchone()
assert row['name'] == '测试基金' and dict(row)['code'] == '999999'

# 去重查询（新版兼容写法）
rows = conn.execute(
    'SELECT h.nav_date AS date, h.unit_nav AS nav, h.actual_change AS change '
    'FROM nav_history h WHERE h.code=? AND h.unit_nav IS NOT NULL '
    'AND h.id = (SELECT MAX(h2.id) FROM nav_history h2 '
    '            WHERE h2.code=h.code AND h2.nav_date=h.nav_date) '
    'ORDER BY h.nav_date DESC LIMIT ?', ('999999', 5)).fetchall()
assert len(rows) == 1 and rows[0]['nav'] == 1.0, 'history 查询失败'

# config 入库（先释放上面的连接，SQLite 单写者）
conn.close()
database.save_config({'scan_interval_seconds': 60, 'test': True})
assert database.get_config().get('test') is True, 'config 入库失败'

conn = database.get_conn()
# 清理
conn.execute('DELETE FROM funds WHERE code=?', ('999999',))
conn.execute('DELETE FROM nav_history WHERE code=?', ('999999',))
conn.commit()
conn.close()
print('[1/8] SQLite 业务回归 OK')

# ---------- 2. PgConn SQL 方言转换（不连库，仅验证字符串转换） ----------
import importlib
pg_mod = importlib.import_module('database')
PgConn = pg_mod.PgConn

class FakePgConn(PgConn):
    """拦截 execute，只记录转换后的 SQL"""
    def __init__(self):  # 不调用父类 __init__，不真连库
        self.sqls = []
    def execute(self, sql, args=()):
        s = sql.replace('?', '%s') if '?' in sql else sql
        if (s.lstrip().upper().startswith('INSERT')
                and 'RETURNING' not in s.upper()
                and 'ON CONFLICT' not in s.upper()):
            s += ' RETURNING id'
        self.sqls.append(s)
        return self
    def fetchone(self):
        return {'id': 42}
    @property
    def lastrowid(self):
        return 42

fake = FakePgConn()
fake.execute('SELECT * FROM funds WHERE code=? AND enabled=1', ('001186',))
assert fake.sqls[-1] == 'SELECT * FROM funds WHERE code=%s AND enabled=1'
fake.execute('INSERT INTO alert_log (code) VALUES (?)', ('001186',))
assert 'RETURNING id' in fake.sqls[-1] and '%s' in fake.sqls[-1]
assert fake.lastrowid == 42
fake.execute('SELECT COUNT(*) AS c FROM alert_log WHERE trigger_time LIKE ?',
             ('2026-09-07%',))
assert fake.sqls[-1].endswith('LIKE %s')
fake.execute('UPDATE alert_log SET notify_status=? WHERE id=?', ('sent', 1))
assert fake.sqls[-1] == 'UPDATE alert_log SET notify_status=%s WHERE id=%s'
fake.execute('DELETE FROM rules WHERE id=?', (1,))
assert fake.sqls[-1] == 'DELETE FROM rules WHERE id=%s'
print('[2/8] PgConn SQL 方言转换 OK')

# ---------- 3. PG DDL 语法检查（不应残留 AUTOINCREMENT / 裸 REAL / 粘连词） ----------
import re
bad = [d for d in database._PG_DDL
       if 'AUTOINCREMENT' in d or ' REAL,' in d or ' REAL)' in d
       or ' REAL NOT NULL' in d
       or re.search(r'\wTEXT|\wDOUBLE|\wINTEGER|\wPRIMARY', d)]
assert not bad, 'PG DDL 存在 SQLite 残留/词法粘连: %s' % bad
assert all('GENERATED ALWAYS AS IDENTITY' in d for d in database._PG_DDL if 'id INTEGER' in d)
print('[3/8] PG DDL 语法 OK')

# ---------- 4. 基金规则评估不得抛异常（回归：曾导致基金提醒全程失效） ----------
# 2026-09-12 03:29 evaluate_all 里写了 `now = now()`，函数体内对 now 赋值使其
# 变成局部变量 → 每次都 UnboundLocalError → 整轮扫描失败 → 基金提醒一条都发不出去，
# 且因为异常被上层 catch 成一行日志，两天都没被发现。这里守住它。
import rule_engine
alerts = rule_engine.evaluate_all()
assert isinstance(alerts, list), 'evaluate_all 必须返回 list'
print('[4/8] evaluate_all 无异常 OK（返回 %d 条待提醒）' % len(alerts))

# ---------- 5. 行情日期解析（阈值提醒/快报共用，两种格式都要认） ----------
import fund_data
assert fund_data._quote_date('20260914133727') == '2026-09-14', 'A股紧凑格式解析失败'
assert fund_data._quote_date('2026/09/14 13:22:28') == '2026-09-14', '港股格式解析失败'
assert fund_data._quote_date('') is None and fund_data._quote_date(None) is None
assert fund_data._quote_date('abc') is None

# 5a. 行情时刻解析（美股"这场收没收盘"的唯一判据）
# 美股那个时间戳是美东时间：收盘后冻结在上一个交易日 16:00 之后，
# 开盘瞬间就变成当天 09:3x —— 只看日期会把"刚开盘"当成"昨夜收盘"。
assert fund_data._quote_hhmm('20261008153800') == 1538, 'A股时刻解析失败'
assert fund_data._quote_hhmm('2026/10/08 15:22:57') == 1522, '港股时刻解析失败'
assert fund_data._quote_hhmm('2026-10-07 17:15:59') == 1715, '美股时刻解析失败'
assert fund_data._quote_hhmm('') is None and fund_data._quote_hhmm(None) is None
assert fund_data._quote_hhmm('2026-10-07') is None

# ---------- 5b. A股交易日闸门（周末 + 法定节假日都要挡住） ----------
# 2026-10-02（周五，国庆 A股休市）港股照常开市、恒生当天有真实行情，
# 旧实现只看 weekday → 被当成交易日，恒生阈值提醒与盘中快报照发。
import datetime as _dt
def _td(y, m, d):
    return rule_engine.is_trading_day(_dt.datetime(y, m, d, 10, 0))
assert _td(2026, 10, 2) is False, '国庆休市日（周五）必须是非交易日'
assert _td(2026, 10, 1) is False, '国庆当日'
assert _td(2026, 10, 7) is False, '国庆最后一日'
assert _td(2026, 2, 17) is False, '春节休市日'
assert _td(2026, 5, 4) is False, '劳动节休市日'
assert _td(2026, 9, 27) is False, '周日'
assert _td(2026, 10, 8) is True, '国庆后首个交易日必须放行'
assert _td(2026, 9, 28) is True, '中秋后首个交易日必须放行'
assert _td(2026, 6, 22) is True, '端午后首个交易日必须放行'
print('[5/8] 行情日期解析 + A股交易日闸门 OK')

# ---------- 6. 盘中快报去重表 ----------
import database as db2
db2.init_db()
D, K, S = '1999-01-01', 'slot', '09:35'
conn = db2.get_conn()
conn.execute('DELETE FROM intraday_log WHERE stat_date=?', (D,))
conn.commit()
conn.close()
assert db2.intraday_sent(D, K, S) is False
db2.intraday_mark(D, K, S, 0.5)
assert db2.intraday_sent(D, K, S) is True, 'mark 之后必须查到'
db2.intraday_mark(D, K, S, 0.9)          # 重复 mark 不应报错（防调度重入）
conn = db2.get_conn()
n = conn.execute('SELECT COUNT(*) AS c FROM intraday_log WHERE stat_date=?',
                 (D,)).fetchone()['c']
conn.execute('DELETE FROM intraday_log WHERE stat_date=?', (D,))
conn.commit()
conn.close()
assert n == 1, '同一 (日期,类型,时点) 必须唯一，实际 %d 行' % n
print('[6/8] 盘中快报去重 OK')

# ---------- 7. 盘中快报纯函数（不联网） ----------
os.environ['FUNDWATCH_NO_SCHEDULER'] = '1'   # 只 import 纯函数，不起调度线程
import app as app_mod
assert app_mod._intraday_cfg({})['slots'] == ['09:35', '11:30', '14:30']
assert app_mod._intraday_cfg({'intraday_brief': {'enabled': True}})['enabled'] is True
assert app_mod._intraday_cfg(
    {'intraday_brief': {'slots': '09:35，11:30 , 14:30, 99:99'}})['slots'] \
    == ['09:35', '11:30', '14:30'], '脏时点应被过滤/归一'
ROWS = [{'name': '上证指数', 'change_pct': -1.2, 'price': 3888.11},
        {'name': '恒生科技', 'change_pct': 0.4, 'price': 4320.57}]
msg = app_mod._build_intraday_message(
    '09:35', ROWS, -0.4, ([('医疗服务', 3.98)], [('种植业', -4.03)]))
lines = msg.split('\n')
assert lines[0].startswith('📊 盘中快报 09:35'), '第一行必须是标题'
body = lines[1:-1]
assert len(body) == len(ROWS), '每个指数一行'
assert all(l.startswith('· ') and '%，' in l for l in body), \
    '数据行必须是 `· 名称 涨跌%，数值`，前端 parseSummaryLine 靠它解析'
note = lines[-1]
assert not note.startswith('·'), '总结行不能以 · 开头，否则会被前端当数据行'
assert '均值 -0.40%' in note and '领跌 上证指数' in note and '领涨 恒生科技' in note
assert '板块领涨 医疗服务' in note and '板块领跌 种植业' in note
# 板块接口挂掉时必须仍能出快报（降级为空列表）
msg2 = app_mod._build_intraday_message('11:30', ROWS, -0.4, ([], []))
# 标题 + 每个指数一行 + 总结一行
assert '板块' not in msg2 and msg2.count('\n') == len(ROWS) + 1, msg2

# 7b. 聚合指数提醒（阈值 1% 时各指数穿越时点分散，逐个推就是十来分钟响一次）
ixa = app_mod._index_alert_cfg({})
assert ixa['interval_min'] == 15 and ixa['threshold'] == 3.0, ixa
assert app_mod._index_alert_cfg({'index_alert': {'threshold': 1}})['threshold'] == 1.0
assert app_mod._index_alert_cfg({'index_alert': {'interval_min': 1}})['interval_min'] == 5, '间隔下限 5 分钟'
assert app_mod._index_alert_cfg({'index_alert': {'threshold': 'x'}})['threshold'] == 3.0, '脏阈值回退'
BATCH = app_mod._build_index_batch_message(
    '10:31', [{'name': '科创50', 'change_pct': -3.66, 'price': 1474.04},
              {'name': '创业板指', 'change_pct': -1.24, 'price': 3068.62}], 1.0)
bl = BATCH.split('\n')
assert bl[0] == '⚡ 指数提醒 10:31', bl[0]
assert all(l.startswith('· ') and '%，' in l for l in bl[1:-1]), BATCH
assert not bl[-1].startswith('·') and '共 2 个指数超 ±1.00%' == bl[-1], bl[-1]

# 7c. 美股汇总的两道闸门
# ① 这场得收完盘（美东 09:30-16:00 之间 = 还在交易，不能推"昨夜收盘"）
assert app_mod._us_session_closed([{'quote_hhmm': 1715}]) is True, '美东 17:15 = 已收盘'
assert app_mod._us_session_closed([{'quote_hhmm': 934}]) is False, '美东 09:34 刚开盘，绝不能再推'
assert app_mod._us_session_closed([{'quote_hhmm': 1559}]) is False
assert app_mod._us_session_closed([{'quote_hhmm': None}]) is False, 'fail-closed'
assert app_mod._us_session_closed([]) is False
# ② 北京时点必须落在"美股必然休市"的窗口（美股在北京 21:30-04:00 / 22:30-05:00 交易）
def _us_win(y, m, d, hh, mm):
    return app_mod._in_us_closed_window(_dt.datetime(y, m, d, hh, mm))
assert _us_win(2026, 10, 8, 8, 0) is True, '早上 08:00 必须放行'
assert _us_win(2026, 10, 8, 5, 0) is True
assert _us_win(2026, 10, 8, 21, 29) is True
assert _us_win(2026, 10, 7, 21, 34) is False, '21:34 是美股开盘时刻，必须挡住（10-07 那条假汇总）'
assert _us_win(2026, 10, 8, 2, 0) is False, '凌晨 02:00 美股还在交易'
# 7d. 接口不下发明文密钥
cf = app_mod._config_for_client({'serverchan_sendkey': 'SCTxxx',
                                 'pushplus_token': 'tok',
                                 'email': {'password': 'authcode', 'username': 'a@b.c'},
                                 'vapid': {'private_key': 'PRIVATE'}})
assert cf['serverchan_sendkey'] == '' and cf['pushplus_token'] == ''
assert cf['email']['password'] == '' and 'vapid' not in cf
assert cf['secrets_set'] == {'serverchan': True, 'pushplus': True,
                            'email_password': True, 'vapid': True}
assert cf['email']['username'] == 'a@b.c', '非机密字段要照常返回'
merged = {'serverchan_sendkey': 'SCTxxx', 'pushplus_token': 'tok', 'email': {'password': 'authcode'}}
app_mod._merge_secret(merged, {'serverchan_sendkey': ''}, 'serverchan_sendkey')
assert merged['serverchan_sendkey'] == 'SCTxxx', '留空必须保留原值（否则保存设置会清空密钥）'
app_mod._merge_secret(merged, {'serverchan_sendkey': 'NEW'}, 'serverchan_sendkey')
assert merged['serverchan_sendkey'] == 'NEW'
app_mod._merge_secret(merged, {'serverchan_sendkey': app_mod.SECRET_MASK}, 'serverchan_sendkey')
assert merged['serverchan_sendkey'] == 'NEW', '掩码回传也必须保留原值'
print('[7/8] 盘中快报纯函数 + 聚合提醒/美股闸门/密钥掩码 OK →\n%s' % msg)

# ---------- 8. 前端解析/排版回归（node uitest.mjs） ----------
# 后端消息格式与前端解析强耦合，改了文案没改前端就是"App 里退化成一大坨灰字"，
# 不报错、很难发现 —— 所以单独用一个 node 脚本把这条耦合钉住。
import shutil
import subprocess
node = shutil.which('node')
uitest = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uitest.mjs')
if not node:
    print('[8/8] 跳过前端回归（未找到 node）')
elif not os.path.exists(uitest):
    print('[8/8] 跳过前端回归（uitest.mjs 不存在）')
else:
    r = subprocess.run([node, uitest], capture_output=True, text=True, cwd=os.path.dirname(uitest))
    tail = (r.stdout or '').strip().splitlines()
    print('[8/8] 前端回归 %s → %s' % ('OK' if r.returncode == 0 else '失败',
                                     tail[-1] if tail else (r.stderr or '').strip()[:200]))
    assert r.returncode == 0, '前端回归失败:\n' + (r.stdout or '') + (r.stderr or '')

print('\n全部自测通过 ✓ （云端真实连接建议拿到 Neon 连接串后再跑一次冒烟测试）')
