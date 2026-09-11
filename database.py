# -*- coding: utf-8 -*-
"""数据层：本地默认 SQLite；设置环境变量 DATABASE_URL 时切换为 PostgreSQL（如 Neon）

云端免费平台（Render 等）重启会清空磁盘，因此 SQLite/配置必须外置到云数据库。
PgConn 包装层自动把 sqlite 风格的 ? 占位符转为 %s，并模拟 lastrowid，
使 app.py / rule_engine.py 无需感知后端差异。
"""
import os
import json
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'fund_monitor.db')

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
USE_PG = bool(DATABASE_URL)

DEFAULT_CONFIG = {
    'scan_interval_seconds': 60,
    'off_hours_interval_seconds': 600,
}


class PgConn:
    """psycopg2 连接包装：兼容 sqlite3 的 conn.execute 风格"""

    def __init__(self, dsn):
        import psycopg2
        import psycopg2.extras
        # connect_timeout=10 + statement_timeout=15s + keepalives：
        # Neon 免费层 compute 会自动暂停，暂停后连接被代理层接受但查询无限期挂起
        # （psycopg2 默认无 statement_timeout）。这会导致 scan_once 卡在第一个
        # DELETE/SELECT 查询、扫描锁被永久占用、所有手动 refresh 返回 -1。
        # statement_timeout 让查询 15s 后快速失败，scan_once 异常释放锁，scheduler
        # 得以继续循环（下一轮 Neon 已冷启动完成即可正常）。
        self.conn = psycopg2.connect(
            dsn,
            connect_timeout=10,
            options='-c statement_timeout=15000',
            keepalives=1, keepalives_idle=30,
            keepalives_interval=10, keepalives_count=3,
        )
        self.cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        self._lastrowid = None

    def execute(self, sql, args=()):
        s = sql.replace('?', '%s') if '?' in sql else sql
        # 普通 INSERT 自动追加 RETURNING id 以模拟 lastrowid；
        # 带 ON CONFLICT 的自定义 UPSERT（如 config 表，无 id 列）不追加
        if (s.lstrip().upper().startswith('INSERT')
                and 'RETURNING' not in s.upper()
                and 'ON CONFLICT' not in s.upper()):
            s += ' RETURNING id'
            self.cur.execute(s, args)
            row = self.cur.fetchone()
            self._lastrowid = row['id'] if row else None
            return self
        self.cur.execute(s, args)
        return self

    @property
    def lastrowid(self):
        return self._lastrowid

    def fetchone(self):
        return self.cur.fetchone()

    def fetchall(self):
        return self.cur.fetchall()

    def commit(self):
        self.conn.commit()

    def rollback(self):
        try:
            self.conn.rollback()
        except Exception:
            pass

    def close(self):
        try:
            self.cur.close()
        except Exception:
            pass
        self.conn.close()


def get_conn():
    if USE_PG:
        return PgConn(DATABASE_URL)
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


_SQLITE_DDL = [
    '''CREATE TABLE IF NOT EXISTS funds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        name TEXT,
        enabled INTEGER DEFAULT 1,
        created_at TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS nav_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL,
        nav_date TEXT,
        unit_nav REAL,
        acc_nav REAL,
        actual_change REAL,
        estimated_nav REAL,
        estimated_change REAL,
        gztime TEXT,
        fetched_at TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT,
        rule_type TEXT NOT NULL,
        direction TEXT NOT NULL,
        threshold REAL NOT NULL,
        enabled INTEGER DEFAULT 1,
        created_at TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS cumulative_state (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_id INTEGER NOT NULL,
        code TEXT NOT NULL,
        baseline_nav REAL,
        baseline_date TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS alert_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL,
        name TEXT,
        rule_type TEXT,
        direction TEXT,
        kind TEXT,
        current_change REAL,
        trigger_time TEXT,
        message TEXT,
        notify_status TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS config (
        cfg_key TEXT PRIMARY KEY,
        value TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS push_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        endpoint TEXT UNIQUE NOT NULL,
        p256dh TEXT,
        auth TEXT,
        created_at TEXT
    )''',
]

_PG_DDL = [s.replace('INTEGER PRIMARY KEY AUTOINCREMENT',
                     'INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY')
           .replace(' REAL,', ' DOUBLE PRECISION,')
           .replace(' REAL)', ' DOUBLE PRECISION)')
           .replace(' REAL NOT NULL', ' DOUBLE PRECISION NOT NULL')
           for s in _SQLITE_DDL]


def init_db():
    conn = get_conn()
    for ddl in (_PG_DDL if USE_PG else _SQLITE_DDL):
        conn.execute(ddl)
    conn.commit()
    conn.close()


# ------------------------- 配置存取（入库，防实例重启丢失） -------------------------

def get_config():
    conn = get_conn()
    row = conn.execute("SELECT value FROM config WHERE cfg_key='app'").fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row['value'])
        except Exception:
            pass
    return None


def save_config(cfg):
    conn = get_conn()
    val = json.dumps(cfg, ensure_ascii=False)
    if USE_PG:
        conn.execute(
            "INSERT INTO config (cfg_key, value) VALUES ('app', %s) "
            "ON CONFLICT (cfg_key) DO UPDATE SET value = EXCLUDED.value", (val,))
    else:
        conn.execute("INSERT OR REPLACE INTO config (cfg_key, value) VALUES ('app', ?)", (val,))
    conn.commit()
    conn.close()


# ------------------------- Push 订阅管理 -------------------------

def get_subs():
    """返回所有 push 订阅记录（list of dict）"""
    conn = get_conn()
    rows = conn.execute(
        'SELECT * FROM push_subscriptions ORDER BY id').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_sub(endpoint, p256dh, auth):
    """插入或更新一条 push 订阅（按 endpoint 去重）"""
    import datetime
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = get_conn()
    if USE_PG:
        conn.execute(
            "INSERT INTO push_subscriptions (endpoint, p256dh, auth, created_at) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT (endpoint) DO UPDATE SET "
            "p256dh=EXCLUDED.p256dh, auth=EXCLUDED.auth",
            (endpoint, p256dh, auth, now))
    else:
        conn.execute(
            "INSERT OR REPLACE INTO push_subscriptions "
            "(endpoint, p256dh, auth, created_at) VALUES (?,?,?,?)",
            (endpoint, p256dh, auth, now))
    conn.commit()
    conn.close()


def del_sub(endpoint):
    """按 endpoint 删除一条订阅"""
    conn = get_conn()
    conn.execute('DELETE FROM push_subscriptions WHERE endpoint=?', (endpoint,))
    conn.commit()
    conn.close()
