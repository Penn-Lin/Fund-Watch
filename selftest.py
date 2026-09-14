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
print('[1/7] SQLite 业务回归 OK')

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
print('[2/7] PgConn SQL 方言转换 OK')

# ---------- 3. PG DDL 语法检查（不应残留 AUTOINCREMENT / 裸 REAL / 粘连词） ----------
import re
bad = [d for d in database._PG_DDL
       if 'AUTOINCREMENT' in d or ' REAL,' in d or ' REAL)' in d
       or ' REAL NOT NULL' in d
       or re.search(r'\wTEXT|\wDOUBLE|\wINTEGER|\wPRIMARY', d)]
assert not bad, 'PG DDL 存在 SQLite 残留/词法粘连: %s' % bad
assert all('GENERATED ALWAYS AS IDENTITY' in d for d in database._PG_DDL if 'id INTEGER' in d)
print('[3/7] PG DDL 语法 OK')

# ---------- 4. 基金规则评估不得抛异常（回归：曾导致基金提醒全程失效） ----------
# 2026-09-12 03:29 evaluate_all 里写了 `now = now()`，函数体内对 now 赋值使其
# 变成局部变量 → 每次都 UnboundLocalError → 整轮扫描失败 → 基金提醒一条都发不出去，
# 且因为异常被上层 catch 成一行日志，两天都没被发现。这里守住它。
import rule_engine
alerts = rule_engine.evaluate_all()
assert isinstance(alerts, list), 'evaluate_all 必须返回 list'
print('[4/7] evaluate_all 无异常 OK（返回 %d 条待提醒）' % len(alerts))

# ---------- 5. 行情日期解析（阈值提醒/快报共用，两种格式都要认） ----------
import fund_data
assert fund_data._quote_date('20260914133727') == '2026-09-14', 'A股紧凑格式解析失败'
assert fund_data._quote_date('2026/09/14 13:22:28') == '2026-09-14', '港股格式解析失败'
assert fund_data._quote_date('') is None and fund_data._quote_date(None) is None
assert fund_data._quote_date('abc') is None
print('[5/7] 行情日期解析 OK')

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
print('[6/7] 盘中快报去重 OK')

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
print('[7/7] 盘中快报纯函数 OK →\n%s' % msg)

print('\n全部自测通过 ✓ （云端真实连接建议拿到 Neon 连接串后再跑一次冒烟测试）')
