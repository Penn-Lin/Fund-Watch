# -*- coding: utf-8 -*-
"""把 Neon 上的数据完整迁移到本机 PostgreSQL。

为什么不用 pg_dump：Neon 的 PG 大版本可能比 Ubuntu 24.04 仓库里的 16 更高，
版本不匹配时 pg_dump 会直接拒绝导出（server version mismatch）。
这里用 psycopg2 逐表复制，跨版本无碍，而且能精确控制 id 和自增序列。

在服务器上跑：
    cd /opt/fund-monitor
    sudo -u fund NEON_URL='postgresql://...neon.tech/neondb?sslmode=require' \
        venv/bin/python deploy/migrate.py

幂等：重复跑会先清空目标表再重灌，不会重复插入。
"""
import os
import sys

import psycopg2
import psycopg2.extras

ENV_PATH = '/opt/fund-monitor/.env'

# 按依赖顺序排列（本项目没有外键，顺序只为可读）
TABLES = [
    'funds',
    'rules',
    'cumulative_state',
    'nav_history',
    'alert_log',
    'intraday_log',
    'config',
    'push_subscriptions',
]


def load_env(path):
    """极简 .env 解析，不引入 python-dotenv 依赖。"""
    if not os.path.exists(path):
        return
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())


def main():
    load_env(ENV_PATH)

    src_url = os.environ.get('NEON_URL', '').strip()
    dst_url = os.environ.get('DATABASE_URL', '').strip()

    if not src_url:
        sys.exit('缺少 NEON_URL 环境变量（Neon 的连接串）')
    if not dst_url:
        sys.exit('缺少 DATABASE_URL（应该在 %s 里）' % ENV_PATH)
    if 'neon.tech' not in src_url:
        print('⚠️  警告：NEON_URL 看起来不像 Neon 地址，确认一下是否搞反了源和目标')
    if 'localhost' not in dst_url and '127.0.0.1' not in dst_url:
        print('⚠️  警告：DATABASE_URL 不是本机地址，确认是否搞反了源和目标')

    print('源：%s' % src_url.split('@')[-1])
    print('目标：%s\n' % dst_url.split('@')[-1])

    # 先让应用自己建表（CREATE TABLE IF NOT EXISTS），保证目标表存在
    os.environ['DATABASE_URL'] = dst_url
    sys.path.insert(0, '/opt/fund-monitor')
    import database
    database.init_db()
    print('目标库表结构已就绪\n')

    src = psycopg2.connect(src_url, connect_timeout=20,
                           keepalives=1, keepalives_idle=30)
    dst = psycopg2.connect(dst_url, connect_timeout=10)

    total_src = total_dst = 0

    try:
        for table in TABLES:
            with src.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as sc:
                sc.execute('SELECT * FROM %s' % table)
                rows = sc.fetchall()

            with dst.cursor() as dc:
                dc.execute('TRUNCATE TABLE %s CASCADE' % table)
                if not rows:
                    dst.commit()
                    print('  %-20s 源表为空，已清空目标' % table)
                    continue

                cols = list(rows[0].keys())
                sql = 'INSERT INTO %s (%s) VALUES %%s' % (table, ', '.join(cols))
                values = [[r[c] for c in cols] for r in rows]
                psycopg2.extras.execute_values(dc, sql, values, page_size=200)

                # 显式插入了 id，自增序列不会自动前进，必须手动对齐，
                # 否则应用下一次 INSERT 会撞主键冲突。
                if 'id' in cols:
                    dc.execute(
                        "SELECT setval(pg_get_serial_sequence('%s', 'id'), "
                        "COALESCE((SELECT MAX(id) FROM %s), 1))" % (table, table))

            dst.commit()

            with dst.cursor() as dc:
                dc.execute('SELECT COUNT(*) FROM %s' % table)
                n_dst = dc.fetchone()[0]

            n_src = len(rows)
            total_src += n_src
            total_dst += n_dst
            flag = 'OK' if n_src == n_dst else '不一致！'
            print('  %-20s 源 %4d 行 → 目标 %4d 行  %s' % (table, n_src, n_dst, flag))

    finally:
        src.close()
        dst.close()

    print('\n合计：源 %d 行 → 目标 %d 行' % (total_src, total_dst))
    if total_src != total_dst:
        sys.exit('行数不一致，请检查上面的输出')
    print('迁移完成。VAPID 密钥在 config 表里，已一并带过来。')


if __name__ == '__main__':
    main()
