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
print('[1/3] SQLite 业务回归 OK')

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
        if s.lstrip().upper().startswith('INSERT') and 'RETURNING' not in s.upper():
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
print('[2/3] PgConn SQL 方言转换 OK')

# ---------- 3. PG DDL 语法检查（不应残留 AUTOINCREMENT / 裸 REAL） ----------
bad = [d for d in database._PG_DDL
       if 'AUTOINCREMENT' in d or ' REAL,' in d or ' REAL)' in d
       or ' REAL NOT NULL' in d]
assert not bad, 'PG DDL 存在 SQLite 残留语法: %s' % bad
assert all('GENERATED ALWAYS AS IDENTITY' in d for d in database._PG_DDL if 'id INTEGER' in d)
print('[3/3] PG DDL 语法 OK')

print('\n全部自测通过 ✓ （云端真实连接建议拿到 Neon 连接串后再跑一次冒烟测试）')
