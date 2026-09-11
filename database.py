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


def _connect_with_hard_timeout(connect_fn, timeout=20):
    """用子线程给 psycopg2.connect 套硬超时。

    libpq 的 connect_timeout 参数在 Neon compute 冷启动期间不完全可靠
    (TCP/SSL 握手阶段可能不触发)，connect 会永久挂起。用线程 join(timeout)
    保证必定有返回：超时→TimeoutError→scan_once finally 释放锁→
    scheduler 60s 重试→compute 冷启动完成后即自愈。

    超时后子线程仍在后台运行(daemon)，最终会因服务端关闭而结束，无泄漏风险。
    """
    import threading
    box = {}

    def _work():
        try:
            box['result'] = connect_fn()
        except BaseException as e:
            box['error'] = e

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(
            f'DB connect exceeded {timeout}s (likely Neon compute cold-start)')
    if 'error' in box:
        raise box['error']
    return box['result']


class PgConn:
    """psycopg2 连接包装：兼容 sqlite3 的 conn.execute 风格"""

    def __init__(self, dsn):
        import psycopg2
        import psycopg2.extras
        # 直接用原始 DSN(Neon pooler 端点)连接，不做直连端点转换、不设
        # statement_timeout options。理由：
        #   1) Neon pooler 拒绝 options 里的 statement_timeout(报
        #      unsupported startup parameter)，直连端点虽支持但 compute
        #      暂停时 connect 会挂起(冷启动)。
        #   2) 我们已用线程硬超时保护 connect(20s) 和 execute/fetch(20s)，
        #      statement_timeout 的"防查询挂起"作用已被 execute 硬超时替代。
        # 三层防线：connect 硬超时 + execute/fetch 硬超时 + scheduler 重试，
        # 任何一层超时都抛异常 → scan_once finally 释放锁 → 自愈。
        self.conn = _connect_with_hard_timeout(
            lambda: psycopg2.connect(dsn, connect_timeout=10, keepalives=1,
                                     keepalives_idle=30, keepalives_interval=10,
                                     keepalives_count=3), 20)
        self.cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        self._lastrowid = None

    def _run_with_timeout(self, fn, timeout=20):
        """用子线程给任意 DB 操作套硬超时（第三道防线，兜底）。

        statement_timeout(服务端) 与 connect 硬超时之外的兜底：
        Neon compute 暂停时，即使 options statement_timeout 未生效
        (如直连端点不可用 fallback 到 pooler、或 SET 被 PgBouncer 丢弃)，
        查询也会在 timeout 秒后强制放弃，抛 TimeoutError → scan_once
        finally 释放锁 → scheduler 60s 重试 → 冷启动完成后自愈。

        超时后子线程仍持连接在后台(daemon)，连接由 Neon 服务端 idle
        timeout 回收，不阻塞锁释放。
        """
        import threading
        box = {}

        def _work():
            try:
                box['result'] = fn()
            except BaseException as e:
                box['error'] = e

        t = threading.Thread(target=_work, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise TimeoutError(
                f'DB operation exceeded {timeout}s (Neon cold-start)')
        if 'error' in box:
            raise box['error']
        return box.get('result')

    def execute(self, sql, args=()):
        s = sql.replace('?', '%s') if '?' in sql else sql
        # 普通 INSERT 自动追加 RETURNING id 以模拟 lastrowid；
        # 带 ON CONFLICT 的自定义 UPSERT（如 config 表，无 id 列）不追加
        def _do():
            if (s.lstrip().upper().startswith('INSERT')
                    and 'RETURNING' not in s.upper()
                    and 'ON CONFLICT' not in s.upper()):
                s2 = s + ' RETURNING id'
                self.cur.execute(s2, args)
                row = self.cur.fetchone()
                self._lastrowid = row['id'] if row else None
            else:
                self.cur.execute(s, args)
        self._run_with_timeout(_do, 20)
        return self

    @property
    def lastrowid(self):
        return self._lastrowid

    def fetchone(self):
        return self._run_with_timeout(self.cur.fetchone, 20)

    def fetchall(self):
        return self._run_with_timeout(self.cur.fetchall, 20)

    def commit(self):
        self._run_with_timeout(self.conn.commit, 20)

    def rollback(self):
        try:
            self._run_with_timeout(self.conn.rollback, 10)
        except Exception:
            pass

    def close(self):
        # close 可能阻塞在忙连接上(查询挂起时)，用硬超时避免阻塞锁释放
        try:
            self._run_with_timeout(self.cur.close, 5)
        except Exception:
            pass
        try:
            self._run_with_timeout(self.conn.close, 5)
        except Exception:
            pass


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
