# -*- coding: utf-8 -*-
"""Flask 后端 + 后台常驻调度线程"""
import os
import json
import threading
import time
import datetime

from flask import Flask, jsonify, request, render_template

import database
import fund_data
import rule_engine
import notifier

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

app = Flask(__name__)


@app.after_request
def _sw_scope_header(resp):
    """sw.js 位于 /static/ 下，默认作用域只有 /static/*，
    会导致页面（/）里 navigator.serviceWorker.ready 永远不 resolve。
    这里放行根作用域，前端注册时显式传 scope:'/'。

    manifest.json 也必须 no-cache：Flask 给静态文件默认 Cache-Control
    max-age=12h，manifest 一旦被缓存住，即使换了图标（URL 变了）设备也
    不会重新拉取，表现就是"卸载重装还是旧图标"。
    """
    if request.path.endswith('/sw.js'):
        resp.headers['Service-Worker-Allowed'] = '/'
        resp.headers['Cache-Control'] = 'no-cache, must-revalidate'
    elif request.path.endswith('/manifest.json'):
        resp.headers['Cache-Control'] = 'no-cache, must-revalidate'
    return resp


def load_config():
    """配置存数据库（云端实例重启不丢）；首次启动以 config.json / 默认值初始化"""
    cfg = database.get_config()
    if cfg is not None:
        return cfg
    cfg = dict(database.DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    database.save_config(cfg)
    return cfg


def save_config(cfg):
    database.save_config(cfg)


_scan_lock = threading.Lock()
_scan_stage = 'idle'  # 扫描阶段：idle/fetching/evaluating/notifying，busy 时供诊断


def scan_once():
    """抓取数据 → 评估规则 → 入库(pending)；通知异步派发，不持扫描锁

    关键设计：通知发送（email/webpush 等慢渠道）移到锁释放后的独立线程，
    避免 163 邮箱 SMTP 从 Render 海外 IP 连接慢/失败时拖住扫描锁，
    导致手动刷新一直返回 -1（拿不到锁）。
    """
    global _scan_stage
    if not _scan_lock.acquire(blocking=False):
        return -1, 0, _scan_stage  # 已有扫描进行中，返回当前阶段供前端提示
    try:
        _scan_stage = 'fetching'
        fetched, pending = _scan_once_impl()
        _scan_stage = 'idle'
    finally:
        _scan_lock.release()
    # 锁已释放：异步发通知，慢 email/webpush 不阻塞调度循环与手动刷新
    if pending:
        threading.Thread(target=_dispatch_alerts, args=(pending,), daemon=True).start()
    return fetched, len(pending), 'idle'


def _scan_once_impl():
    """抓取数据 + 评估规则 + 插入 pending 提醒，返回 (fetched, [(aid, alert), ...])

    只做 DB 读写，不发通知（通知由 _dispatch_alerts 异步完成），保证锁内逻辑都快速完成。
    """
    global _scan_stage
    fetched = 0
    _scan_stage = 'db_cleanup'
    conn = database.get_conn()
    try:
        # 清理 90 天前的历史快照，控制数据量
        cutoff = (rule_engine.now() - datetime.timedelta(days=90)
                  ).strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('DELETE FROM nav_history WHERE fetched_at < ?', (cutoff,))
        conn.commit()
        funds = conn.execute(
            'SELECT * FROM funds WHERE enabled=1').fetchall()
        codes = [f['code'] for f in funds]
    finally:
        conn.close()

    if codes:
        _scan_stage = 'http_fetch'
        try:
            data_list = fund_data.fetch_funds(codes)
        except Exception:
            data_list = []
        _scan_stage = 'inserting'
        conn = database.get_conn()
        try:
            for d in data_list:
                try:
                    conn.execute(
                        'INSERT INTO nav_history (code, nav_date, unit_nav, acc_nav, '
                        'actual_change, estimated_nav, estimated_change, gztime, fetched_at) '
                        'VALUES (?,?,?,?,?,?,?,?,?)',
                        (d['code'], d['nav_date'], d['unit_nav'], d['acc_nav'],
                         d['actual_change'], d['estimated_nav'], d['estimated_change'],
                         d['gztime'], rule_engine.now_str()))
                    conn.commit()
                    fetched += 1
                except Exception:
                    conn.rollback()
        finally:
            conn.close()

    _scan_stage = 'evaluating'
    # 兜住：规则评估里任何一个 bug 都不该把整轮扫描拖垮（行情已经入库了，
    # 而且指数类提醒走的是 scheduler 里另一条独立路径）。但要大声打日志，
    # 不能静默——evaluate_all 曾经因 UnboundLocalError 全程抛异常，
    # 结果基金提醒整整两天一条都没发出去，却不留任何痕迹。
    try:
        alerts = rule_engine.evaluate_all()
    except Exception as e:
        print('evaluate_all error (基金规则评估失败，本轮跳过):', repr(e))
        alerts = []
    _scan_stage = 'alert_insert'
    pending = []
    conn = database.get_conn()
    try:
        for a in alerts:
            cur = conn.execute(
                'INSERT INTO alert_log (code, name, rule_type, direction, kind, '
                'current_change, trigger_time, message, notify_status) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (a['code'], a['name'], a['rule_type'], a['direction'], a['kind'],
                 a['current_change'], rule_engine.now_str(), a['message'], 'pending'))
            aid = cur.lastrowid
            conn.commit()
            pending.append((aid, a))
    finally:
        conn.close()
    return fetched, pending


def _dispatch_alerts(pending):
    """异步发送通知并更新状态（在锁释放后的独立线程运行）

    慢渠道（email SMTP 从海外连 163、webpush）失败/超时不会阻塞扫描锁
    与调度循环；失败记 'failed'，成功记 'sent'。
    """
    global _scan_stage
    _scan_stage = 'notifying'
    try:
        cfg = load_config()
        for aid, alert in pending:
            try:
                result = notifier.send_alert(cfg, '基金涨跌提醒', alert['message'])
                status = 'sent' if result['ok'] else 'failed'
            except Exception:
                status = 'failed'
            try:
                conn = database.get_conn()
                conn.execute('UPDATE alert_log SET notify_status=? WHERE id=?',
                             (status, aid))
                conn.commit()
                conn.close()
            except Exception:
                pass
    finally:
        _scan_stage = 'idle'


def _interval():
    now = rule_engine.now()
    if rule_engine.is_trading_day(now) and rule_engine.is_market_hours(now):
        return int(load_config().get('scan_interval_seconds', 60))
    return int(load_config().get('off_hours_interval_seconds', 600))


# ------------------------- 收盘汇总推送 -------------------------

def _build_summary_message():
    """生成当日收盘汇总消息，返回 (msg, 条数)；无启用基金返回 (None, 0)"""
    conn = database.get_conn()
    funds = conn.execute('SELECT * FROM funds WHERE enabled=1').fetchall()
    lines = []
    today = rule_engine.today_str()
    for f in funds:
        last = conn.execute(
            'SELECT * FROM nav_history WHERE code=? ORDER BY id DESC LIMIT 1',
            (f['code'],)).fetchone()
        if not last:
            continue
        # 晚间净值已确认用实际值，未确认则回退估值
        chg = (last['actual_change'] if last['nav_date'] == today
               else (last['estimated_change']
                     if last['estimated_change'] is not None
                     else last['actual_change']))
        if chg is None:
            continue
        sign = '+' if chg > 0 else ''
        nav = last['unit_nav']
        date_tag = '' if last['nav_date'] == today else '（%s）' % (last['nav_date'] or '')
        lines.append('· %s(%s) %s%s%%，净值 %s%s' % (
            f['name'], f['code'], sign, '%.2f' % chg,
            '%.4f' % nav if nav else '—', date_tag))
    conn.close()
    if not lines:
        return None, 0
    weekday = '周' + '一二三四五六日'[rule_engine.now().weekday()]
    msg = '📊 收盘汇总 %s %s\n%s' % (today, weekday, '\n'.join(lines))
    return msg, len(lines)


def maybe_send_summary():
    """到达配置时间后推送当日汇总（每交易日一次，alert_log 记录去重）"""
    cfg = load_config()
    ds = cfg.get('daily_summary') or {}
    if not ds.get('enabled'):
        return False
    hhmm = str(ds.get('time') or '20:00')
    now = rule_engine.now()
    if not rule_engine.is_trading_day(now):
        return False
    try:
        h, m = (int(x) for x in hhmm.split(':'))
    except Exception:
        return False
    if (now.hour, now.minute) < (h, m):
        return False
    today = rule_engine.today_str()
    conn = database.get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM alert_log WHERE kind='daily_summary' "
        "AND trigger_time LIKE ?", (today + '%',)).fetchone()
    conn.close()
    if row['c'] > 0:
        return False  # 今天已发过

    msg, _ = _build_summary_message()
    if msg is None:
        return False
    result = notifier.send_alert(cfg, '基金监控 · 收盘汇总', msg)
    conn = database.get_conn()
    conn.execute(
        'INSERT INTO alert_log (code, name, rule_type, direction, kind, '
        'current_change, trigger_time, message, notify_status) '
        "VALUES ('SUMMARY', '收盘汇总', 'summary', NULL, 'daily_summary', "
        'NULL, ?, ?, ?)',
        (rule_engine.now_str(), msg,
         'sent' if result['ok'] else 'failed'))
    conn.commit()
    conn.close()
    return True


# ------------------------- 指数推送 -------------------------

def _build_index_summary_message():
    """生成指数收盘汇总消息，返回 (msg, 条数)"""
    indices = fund_data.fetch_indices(max_age=120)
    if not indices:
        return None, 0
    # is_trading_day 只挡周末、挡不住节假日：节假日接口返回的是上一交易日的
    # 收官数据。用行情自带日期兜底——没有任何一条是今天的，就不是收盘汇总。
    today = rule_engine.today_str()
    if not any((ix.get('quote_date') or '') == today for ix in indices):
        return None, 0
    lines = []
    for ix in indices:
        chg = ix.get('change_pct')
        if chg is None:
            continue
        sign = '+' if chg > 0 else ''
        price = ix['price']
        price_str = '%.2f' % price if price else '—'
        lines.append('· %s %s%.2f%%，%s' % (
            ix['name'], sign, chg, price_str))
    if not lines:
        return None, 0
    weekday = '周' + '一二三四五六日'[rule_engine.now().weekday()]
    msg = '📈 指数收盘汇总 %s %s\n%s' % (
        rule_engine.today_str(), weekday, '\n'.join(lines))
    return msg, len(lines)


def maybe_send_index_summary():
    """到点推送指数汇总（每交易日一次，alert_log 去重）"""
    cfg = load_config()
    ds = cfg.get('index_summary') or {}
    if not ds.get('enabled'):
        return False
    hhmm = str(ds.get('time') or '20:00')
    now = rule_engine.now()
    if not rule_engine.is_trading_day(now):
        return False
    try:
        h, m = (int(x) for x in hhmm.split(':'))
    except Exception:
        return False
    if (now.hour, now.minute) < (h, m):
        return False
    today = rule_engine.today_str()
    conn = database.get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM alert_log WHERE kind='index_summary' "
        "AND trigger_time LIKE ?", (today + '%',)).fetchone()
    conn.close()
    if row['c'] > 0:
        return False

    msg, _ = _build_index_summary_message()
    if msg is None:
        return False
    result = notifier.send_alert(cfg, '基金监控 · 指数收盘汇总', msg)
    conn = database.get_conn()
    conn.execute(
        'INSERT INTO alert_log (code, name, rule_type, direction, kind, '
        'current_change, trigger_time, message, notify_status) '
        "VALUES ('IX_SUMMARY', '指数汇总', 'index', NULL, 'index_summary', "
        'NULL, ?, ?, ?)',
        (rule_engine.now_str(), msg,
         'sent' if result['ok'] else 'failed'))
    conn.commit()
    conn.close()
    return True


def _build_us_index_summary_message():
    """生成美股指数昨夜收盘汇总消息（secid 以 us 开头，如纳指100），返回 (msg, 条数)"""
    indices = fund_data.fetch_indices(max_age=120)
    if not indices:
        return None, 0
    lines = []
    for ix in indices:
        if not (ix.get('secid') or '').startswith('us'):
            continue
        chg = ix.get('change_pct')
        if chg is None:
            continue
        sign = '+' if chg > 0 else ''
        price = ix.get('price')
        lines.append('· %s %s%.2f%%，%s' % (
            ix['name'], sign, chg, ('%.2f' % price) if price else '—'))
    if not lines:
        return None, 0
    weekday = '周' + '一二三四五六日'[rule_engine.now().weekday()]
    msg = '🌙 美股昨夜收盘 %s %s\n%s' % (
        rule_engine.today_str(), weekday, '\n'.join(lines))
    return msg, len(lines)


def maybe_send_us_index_summary():
    """每天到点（默认 08:00）推送美股昨夜收盘汇总，alert_log 去重

    美股(纳指100)交易日与 A股错位、且在北京时间夜间交易，故不做
    is_trading_day 判断——每天早晨固定推一条昨夜收盘情况，无论涨跌。
    """
    cfg = load_config()
    us = cfg.get('us_index_summary') or {}
    if not us.get('enabled'):
        return False
    hhmm = str(us.get('time') or '08:00')
    now = rule_engine.now()
    try:
        h, m = (int(x) for x in hhmm.split(':'))
    except Exception:
        return False
    if (now.hour, now.minute) < (h, m):
        return False
    today = rule_engine.today_str()
    conn = database.get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM alert_log WHERE kind='us_index_summary' "
        "AND trigger_time LIKE ?", (today + '%',)).fetchone()
    conn.close()
    if row['c'] > 0:
        return False

    msg, _ = _build_us_index_summary_message()
    if msg is None:
        return False
    result = notifier.send_alert(cfg, '基金监控 · 美股昨夜', msg)
    conn = database.get_conn()
    conn.execute(
        'INSERT INTO alert_log (code, name, rule_type, direction, kind, '
        'current_change, trigger_time, message, notify_status) '
        "VALUES ('US_IX_SUMMARY', '美股汇总', 'index', NULL, 'us_index_summary', "
        'NULL, ?, ?, ?)',
        (rule_engine.now_str(), msg, 'sent' if result['ok'] else 'failed'))
    conn.commit()
    conn.close()
    return True


def maybe_eval_indices():
    """指数涨跌幅超阈值即时推送（每指数每方向每日一次去重）

    必须只在「当日行情」上判定，否则会出两个 bug（2026-09-14 修）：
    ① 收盘后/周末/节假日，接口返回的是上一个交易日的收盘值，change_pct 还是旧值；
       而 0 点日期一翻页，去重键（trigger_time LIKE '今天%'）就重置了，
       于是旧行情被当成"今天的行情"重新推一遍 —— 用户半夜收到一堆重复提醒。
    ② 更严重的是去重被这一次误触发占掉，当天真正跌破阈值时**不会再提醒**。
    """
    cfg = load_config()
    ia = cfg.get('index_alert') or {}
    if not ia.get('enabled'):
        return 0
    threshold = float(ia.get('threshold') or 3)
    if threshold <= 0:
        return 0
    # 不用"几点之前不算"这种时间闸门：A 股 09:25 集合竞价就出开盘价，
    # 09:26 是合法的当日行情（今天 09:26 那条创业板指提醒就是这么来的）。
    # 只靠行情自带日期判断新鲜度，两头都不会误伤。
    now = rule_engine.now()
    if not rule_engine.is_trading_day(now):
        return 0
    indices = fund_data.fetch_indices(max_age=60)
    if not indices:
        return 0
    today = rule_engine.today_str()
    sent = 0
    for ix in indices:
        if (ix.get('secid') or '').startswith('us'):
            continue  # 美股指数改走早上汇总，不参与盘中实时阈值提醒
        # 行情日期必须是今天。fail-closed：取不到日期也视为不新鲜——
        # 宁可漏一次，也不要在 0 点把上一交易日的收官数据当今日行情推一遍
        # （那种误触发还会占掉去重名额，让当天真正的下跌提醒发不出来）。
        if (ix.get('quote_date') or '') != today:
            continue
        chg = ix.get('change_pct')
        if chg is None:
            continue
        direction = 'up' if chg >= threshold else (
            'down' if chg <= -threshold else None)
        if not direction:
            continue
        code = 'IX_' + (ix.get('secid') or ix.get('code') or ix['name'])
        conn = database.get_conn()
        already = conn.execute(
            "SELECT COUNT(*) AS c FROM alert_log WHERE code=? AND kind='index_threshold' "
            "AND direction=? AND trigger_time LIKE ?",
            (code, direction, today + '%')).fetchone()['c']
        if already:
            conn.close()
            continue
        sign = '+' if chg > 0 else ''
        up_down = '上涨' if direction == 'up' else '下跌'
        msg = '【%s】指数当日%s %s%.2f%%，达到阈值 %.2f%%' % (
            ix['name'], up_down, sign, chg, threshold)
        result = notifier.send_alert(cfg, '基金监控 · 指数提醒', msg)
        conn.execute(
            'INSERT INTO alert_log (code, name, rule_type, direction, kind, '
            'current_change, trigger_time, message, notify_status) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (code, ix['name'], 'index', direction, 'index_threshold',
             chg, rule_engine.now_str(), msg,
             'sent' if result['ok'] else 'failed'))
        conn.commit()
        conn.close()
        sent += 1
    return sent


# ------------------------- 盘中快报（时点骨架） -------------------------
# 定位：**感知型**提醒 —— 盘中快速知道"大盘现在大概什么情况"。
# 用户明确要求（2026-09-14）：
#   · 到点就发，**不做任何"变动太小就不推"的抑制**（那是给噪音型提醒用的，
#     快报本身就是信息，不是告警）
#   · 只留重点（大盘均值 / 几跌几涨 / 领跌领涨），一行说完，不列全量明细
#   · 与指数阈值提醒（index_alert）**完全独立**：互不影响、互不抑制、
#     不共享状态。两者可以同时触发，也可以只触发其中一个。

DEFAULT_BRIEF_SLOTS = ['09:35', '11:30', '14:30']
# 时点过了这么久还没发成就不再发 —— 免得实例恢复后突然补推一条"09:35 快报"，
# 时间对不上反而让人误判。这是防错，不是抑制。
BRIEF_SLOT_WINDOW_MIN = 20


def _is_hhmm(s):
    try:
        h, m = (int(x) for x in str(s).split(':'))
    except Exception:
        return False
    return 0 <= h <= 23 and 0 <= m <= 59


def _intraday_cfg(cfg):
    """取盘中快报配置并补默认值（老配置里没这个 key 也能正常跑）"""
    ib = cfg.get('intraday_brief') or {}
    slots = ib.get('slots') or DEFAULT_BRIEF_SLOTS
    if isinstance(slots, str):
        slots = slots.replace('，', ',').split(',')
    slots = sorted({str(s).strip() for s in slots if _is_hhmm(str(s).strip())})
    if not slots:
        slots = list(DEFAULT_BRIEF_SLOTS)
    return {'enabled': bool(ib.get('enabled')), 'slots': slots}


def _index_panel():
    """快报用的指数面板：A 股核心指数 + 恒生指数/恒生科技（剔除美股）

    美股不进：北京时间白天美股没交易，放进均值只是噪音；美股由早上 08:00
    的昨夜收盘汇总覆盖。
    返回 (rows, avg)，rows=[{'name','change_pct','price'}]，avg = 等权平均。
    """
    indices = fund_data.fetch_indices(max_age=60)
    if not indices:
        return [], None
    today = rule_engine.today_str()
    rows = []
    for ix in indices:
        if (ix.get('secid') or '').startswith('us'):
            continue
        # 行情日期不是今天的就不进面板（节假日/接口滞后）
        if (ix.get('quote_date') or '') != today:
            continue
        chg = ix.get('change_pct')
        if chg is None:
            continue
        rows.append({'name': ix['name'], 'change_pct': chg, 'price': ix.get('price')})
    if not rows:
        return [], None
    return rows, sum(r['change_pct'] for r in rows) / len(rows)


def _build_intraday_message(slot, rows, avg, sectors):
    """快报正文：逐项列出指数 + 一行总结（含板块），没有多余的话

    格式约束（与前端 parseSummaryLine / parseAlert 强耦合）：
    · 第 1 行是标题
    · 中间每行必须是 `· 名称 涨跌%，数值`，前端才会渲染成带色带的列表
    · **最后一个非 `·` 开头的行会被前端当"补充说明"**（notes），
      展示在浮层副文案和详情页里 —— 总结就放这里
    """
    lines = ['📊 盘中快报 %s' % slot]
    for r in rows:
        sign = '+' if r['change_pct'] > 0 else ''
        price = ('%.2f' % r['price']) if r.get('price') else '—'
        lines.append('· %s %s%.2f%%，%s' % (r['name'], sign, r['change_pct'], price))

    worst = min(rows, key=lambda r: r['change_pct'])
    best = max(rows, key=lambda r: r['change_pct'])
    parts = ['均值 %s%.2f%%' % ('+' if avg > 0 else '', avg)]
    if worst['change_pct'] < 0:
        parts.append('领跌 %s %.2f%%' % (worst['name'], worst['change_pct']))
    if best['change_pct'] > 0:
        parts.append('领涨 %s +%.2f%%' % (best['name'], best['change_pct']))
    lead_sec, lag_sec = sectors or ([], [])
    if lead_sec:
        parts.append('板块领涨 ' + '、'.join('%s %+.2f%%' % s for s in lead_sec))
    if lag_sec:
        parts.append('板块领跌 ' + '、'.join('%s %.2f%%' % s for s in lag_sec))
    lines.append(' · '.join(parts))
    return '\n'.join(lines)


def _log_intraday_alert(code, name, kind, avg, msg, result):
    conn = database.get_conn()
    conn.execute(
        'INSERT INTO alert_log (code, name, rule_type, direction, kind, '
        'current_change, trigger_time, message, notify_status) '
        'VALUES (?,?,?,?,?,?,?,?,?)',
        (code, name, 'index', ('down' if (avg or 0) < 0 else 'up'), kind,
         avg, rule_engine.now_str(), msg, 'sent' if result['ok'] else 'failed'))
    conn.commit()
    conn.close()


def maybe_send_intraday_brief():
    """到点就发一条大盘快报。

    没有静默闸门、没有状态耦合 —— 三个时点到点各发一条，内容就是当时的实况。
    `intraday_log` 只用于「这个时点今天处理过没有」的去重，
    防止 Render 重启或调度重入导致同一时点重复推送。
    """
    cfg = load_config()
    ib = _intraday_cfg(cfg)
    if not ib['enabled']:
        return 0
    now = rule_engine.now()
    if not rule_engine.is_trading_day(now):
        return 0
    today = rule_engine.today_str()

    # 找「已到点、今天还没处理、且没过窗口」的最早时点
    due = None
    for s in ib['slots']:
        if database.intraday_sent(today, 'slot', s):
            continue
        h, m = (int(x) for x in s.split(':'))
        slot_at = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now < slot_at:
            break                      # 还没到点；后面的时点更晚，不用看
        if (now - slot_at).total_seconds() > BRIEF_SLOT_WINDOW_MIN * 60:
            # 错过太久就不发了（防错），标记掉免得每轮扫描反复评估
            database.intraday_mark(today, 'slot', s)
            continue
        due = s
        break
    if not due:
        return 0

    rows, avg = _index_panel()
    if not rows:
        return 0

    sectors = fund_data.fetch_sectors(2)
    msg = _build_intraday_message(due, rows, avg, sectors)
    result = notifier.send_alert(cfg, '基金监控 · 盘中快报', msg)
    database.intraday_mark(today, 'slot', due, avg)
    _log_intraday_alert('IX_BRIEF', '盘中快报', 'intraday_brief', avg, msg, result)
    return 1


# 调度线程的自述状态：线程一死，盘中快报/各类定时汇总就全都不再触发，
# 而表面上（cron 仍在打 /api/refresh）一切"正常"，极难发现 → 必须能自证还活着。
_sched_state = {'cycle': 0, 'started_at': None, 'last_cycle_at': None,
                'last_cycle_ms': None, 'last_error': None}


def scheduler_loop():
    _sched_state['started_at'] = rule_engine.now_str()
    while True:
        cycle_ok = True
        t_cycle = time.time()
        # 确保表存在（幂等；Neon compute 恢复后自动补建）。
        # 不能依赖模块级 init_db 成功——它可能因 Neon 冷启动失败。
        try:
            database.init_db()
        except Exception as e:
            print('init_db error:', e)
            time.sleep(60)
            continue
        try:
            scan_once()
        except Exception as e:
            print('scan error:', e)
            cycle_ok = False
        try:
            maybe_send_summary()
        except Exception as e:
            print('summary error:', e)
            cycle_ok = False
        try:
            maybe_eval_indices()
        except Exception as e:
            print('index alert error:', e)
            cycle_ok = False
        try:
            maybe_send_intraday_brief()
        except Exception as e:
            print('intraday brief error:', e)
            cycle_ok = False
        try:
            maybe_send_index_summary()
        except Exception as e:
            print('index summary error:', e)
            cycle_ok = False
        try:
            maybe_send_us_index_summary()
        except Exception as e:
            print('us index summary error:', e)
            cycle_ok = False
        _sched_state['cycle'] += 1
        _sched_state['last_cycle_at'] = rule_engine.now_str()
        _sched_state['last_cycle_ms'] = int((time.time() - t_cycle) * 1000)
        # 失败时快速重试（Neon 冷启动/网络抖动后能自愈），成功时按配置间隔
        try:
            time.sleep(60 if not cycle_ok else _interval())
        except Exception:
            time.sleep(60)


# ------------------------- 页面 -------------------------

@app.route('/')
def index():
    return render_template('index.html')


# ------------------------- 基金 CRUD -------------------------

@app.route('/api/funds')
def api_funds():
    conn = database.get_conn()
    funds = conn.execute('SELECT * FROM funds ORDER BY id').fetchall()
    out = []
    for f in funds:
        item = dict(f)
        last = conn.execute(
            'SELECT * FROM nav_history WHERE code=? ORDER BY id DESC LIMIT 1',
            (f['code'],)).fetchone()
        if last:
            daily_change = (last['estimated_change']
                            if last['estimated_change'] is not None
                            else last['actual_change'])
            item.update({
                'nav_date': last['nav_date'], 'unit_nav': last['unit_nav'],
                'acc_nav': last['acc_nav'],
                'estimated_nav': last['estimated_nav'],
                'estimated_change': last['estimated_change'],
                'actual_change': last['actual_change'],
                'gztime': last['gztime'], 'daily_change': daily_change,
            })
        else:
            item.update({'nav_date': None, 'unit_nav': None, 'acc_nav': None,
                         'estimated_nav': None, 'estimated_change': None,
                         'actual_change': None, 'gztime': None, 'daily_change': None})
        # 累计规则基准（基金级优先，否则全局）
        cum_rule = conn.execute(
            "SELECT * FROM rules WHERE rule_type='cumulative' AND enabled=1 AND code=? "
            "ORDER BY id LIMIT 1", (f['code'],)).fetchone()
        if not cum_rule:
            cum_rule = conn.execute(
                "SELECT * FROM rules WHERE rule_type='cumulative' AND enabled=1 "
                "AND code='GLOBAL' ORDER BY id LIMIT 1").fetchone()
        item['baseline_nav'] = None
        item['baseline_date'] = None
        item['cumulative_change'] = None
        item['cumulative_change_est'] = None
        item['cum_threshold'] = cum_rule['threshold'] if cum_rule else None
        if cum_rule:
            st = conn.execute(
                'SELECT * FROM cumulative_state WHERE rule_id=? AND code=?',
                (cum_rule['id'], f['code'])).fetchone()
            if st and st['baseline_nav']:
                item['baseline_nav'] = st['baseline_nav']
                item['baseline_date'] = st['baseline_date']
                if item.get('unit_nav'):
                    item['cumulative_change'] = round(
                        (item['unit_nav'] - st['baseline_nav']) / st['baseline_nav'] * 100, 2)
                if item.get('estimated_nav'):
                    item['cumulative_change_est'] = round(
                        (item['estimated_nav'] - st['baseline_nav']) / st['baseline_nav'] * 100, 2)
        out.append(item)
    conn.close()
    return jsonify(out)


@app.route('/api/funds', methods=['POST'])
def api_add_fund():
    data = request.get_json(silent=True) or {}
    code = str(data.get('code', '')).strip()
    if not code:
        return jsonify({'error': '请输入基金代码'}), 400
    try:
        d = fund_data.fetch_fund(code)
    except Exception as e:
        return jsonify({'error': '获取失败：' + str(e)}), 400
    conn = database.get_conn()
    if conn.execute('SELECT id FROM funds WHERE code=?', (d['code'],)).fetchone():
        conn.close()
        return jsonify({'error': '该基金已在监控列表'}), 400
    conn.execute('INSERT INTO funds (code, name, enabled, created_at) VALUES (?,?,?,?)',
                 (d['code'], d['name'], 1, rule_engine.now_str()))
    # 复用本次抓到的数据入库，避免再次请求触发限流
    conn.execute(
        'INSERT INTO nav_history (code, nav_date, unit_nav, acc_nav, actual_change, '
        'estimated_nav, estimated_change, gztime, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)',
        (d['code'], d['nav_date'], d['unit_nav'], d['acc_nav'], d['actual_change'],
         d['estimated_nav'], d['estimated_change'], d['gztime'], rule_engine.now_str()))
    conn.commit()
    conn.close()
    # 后台回填近 30 天历史净值（用于走势图），不阻塞响应
    def _backfill():
        try:
            hist = fund_data.fetch_history(d['code'], 30)
            if not hist:
                return
            c = database.get_conn()
            have = {r[0] for r in c.execute(
                'SELECT DISTINCT nav_date FROM nav_history WHERE code=?',
                (d['code'],)).fetchall()}
            for h in hist:
                if h['date'] not in have:
                    c.execute(
                        'INSERT INTO nav_history (code, nav_date, unit_nav, acc_nav, '
                        'actual_change, estimated_nav, estimated_change, gztime, fetched_at) '
                        'VALUES (?,?,?,?,?,?,?,?,?)',
                        (d['code'], h['date'], h['nav'], h['acc_nav'], h['change'],
                         None, None, None, rule_engine.now_str()))
            c.commit()
            c.close()
        except Exception:
            pass
    threading.Thread(target=_backfill, daemon=True).start()
    return jsonify({'ok': True, 'code': d['code'], 'name': d['name']})


@app.route('/api/funds/<int:fid>', methods=['PUT'])
def api_edit_fund(fid):
    data = request.get_json(silent=True) or {}
    sets, args = [], []
    if 'name' in data:
        sets.append('name=?')
        args.append(data['name'])
    if 'enabled' in data:
        sets.append('enabled=?')
        args.append(1 if data['enabled'] else 0)
    if sets:
        conn = database.get_conn()
        args.append(fid)
        conn.execute('UPDATE funds SET %s WHERE id=?' % ', '.join(sets), args)
        conn.commit()
        conn.close()
    return jsonify({'ok': True})


@app.route('/api/funds/<int:fid>', methods=['DELETE'])
def api_del_fund(fid):
    conn = database.get_conn()
    conn.execute('DELETE FROM funds WHERE id=?', (fid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ------------------------- 规则 CRUD -------------------------

@app.route('/api/rules')
def api_rules():
    conn = database.get_conn()
    rules = conn.execute('SELECT * FROM rules ORDER BY id').fetchall()
    out = []
    for r in rules:
        item = dict(r)
        if r['code'] == 'GLOBAL':
            item['scope'] = 'global'
            item['scope_name'] = '所有基金'
        else:
            item['scope'] = 'fund'
            fund = conn.execute('SELECT name FROM funds WHERE code=?', (r['code'],)).fetchone()
            item['scope_name'] = fund['name'] if fund else r['code']
        out.append(item)
    conn.close()
    return jsonify(out)


@app.route('/api/rules', methods=['POST'])
def api_add_rule():
    data = request.get_json(silent=True) or {}
    code = data.get('code') or 'GLOBAL'
    rule_type = data.get('rule_type')
    direction = data.get('direction', 'both')
    threshold = float(data.get('threshold', 0))
    enabled = 1 if data.get('enabled', True) else 0
    if rule_type not in ('daily', 'cumulative'):
        return jsonify({'error': '无效规则类型'}), 400
    if threshold <= 0:
        return jsonify({'error': '阈值必须大于 0'}), 400
    conn = database.get_conn()
    exist = conn.execute(
        'SELECT id FROM rules WHERE code=? AND rule_type=? AND direction=?',
        (code, rule_type, direction)).fetchone()
    if exist:
        conn.execute('UPDATE rules SET threshold=?, enabled=? WHERE id=?',
                     (threshold, enabled, exist['id']))
        rid = exist['id']
    else:
        cur = conn.execute(
            'INSERT INTO rules (code, rule_type, direction, threshold, enabled, created_at) '
            'VALUES (?,?,?,?,?,?)',
            (code, rule_type, direction, threshold, enabled, rule_engine.now_str()))
        rid = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'id': rid})


@app.route('/api/rules/<int:rid>', methods=['DELETE'])
def api_del_rule(rid):
    conn = database.get_conn()
    conn.execute('DELETE FROM rules WHERE id=?', (rid,))
    conn.execute('DELETE FROM cumulative_state WHERE rule_id=?', (rid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ------------------------- 手动设置基准净值 -------------------------

@app.route('/api/baseline', methods=['POST'])
def api_reset_baseline():
    """手动设置基金累计节点基准净值，设置后立即评估：

    - 若当前净值 vs 新基准未穿越阈值：基准保持用户设的值，正常累计等待
    - 若已穿越阈值：触发 cum_confirm 提醒（"定投信号"），并重置基准为
      当前净值，开始累计下一段——实现"每跌4%定投"的节点逻辑。
    """
    data = request.get_json(silent=True) or {}
    code = str(data.get('code', '')).strip()
    try:
        baseline_nav = float(data.get('baseline_nav', 0))
    except (TypeError, ValueError):
        baseline_nav = 0
    baseline_date = str(data.get('baseline_date') or rule_engine.today_str())
    if not code or baseline_nav <= 0:
        return jsonify({'error': '请填写有效的基金代码和基准净值'}), 400

    conn = database.get_conn()
    fund = conn.execute(
        'SELECT * FROM funds WHERE code=?', (code,)).fetchone()
    if not fund:
        conn.close()
        return jsonify({'error': '基金不存在'}), 400
    cum_rule = conn.execute(
        "SELECT * FROM rules WHERE rule_type='cumulative' AND enabled=1 AND code=? "
        "ORDER BY id LIMIT 1", (code,)).fetchone()
    if not cum_rule:
        cum_rule = conn.execute(
            "SELECT * FROM rules WHERE rule_type='cumulative' AND enabled=1 "
            "AND code='GLOBAL' ORDER BY id LIMIT 1").fetchone()
    if not cum_rule:
        conn.close()
        return jsonify({'error': '未启用累计规则，请先在「规则」页开启累计涨跌节点'}), 400

    st = conn.execute(
        'SELECT * FROM cumulative_state WHERE rule_id=? AND code=?',
        (cum_rule['id'], code)).fetchone()
    if st:
        conn.execute(
            'UPDATE cumulative_state SET baseline_nav=?, baseline_date=? WHERE id=?',
            (baseline_nav, baseline_date, st['id']))
    else:
        conn.execute(
            'INSERT INTO cumulative_state (rule_id, code, baseline_nav, baseline_date) '
            'VALUES (?,?,?,?)', (cum_rule['id'], code, baseline_nav, baseline_date))
    conn.commit()
    conn.close()

    # 立即评估：若已穿越阈值则触发提醒并重置基准为当前净值
    cfg = load_config()
    alerts = rule_engine.evaluate_baseline_reset(code)
    for a in alerts:
        conn = database.get_conn()
        cur = conn.execute(
            'INSERT INTO alert_log (code, name, rule_type, direction, kind, '
            'current_change, trigger_time, message, notify_status) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (a['code'], a['name'], a['rule_type'], a['direction'], a['kind'],
             a['current_change'], rule_engine.now_str(), a['message'], 'pending'))
        aid = cur.lastrowid
        conn.commit()
        conn.close()
        result = notifier.send_alert(cfg, '基金涨跌提醒', a['message'])
        status = 'sent' if result['ok'] else 'failed'
        conn = database.get_conn()
        conn.execute('UPDATE alert_log SET notify_status=? WHERE id=?', (status, aid))
        conn.commit()
        conn.close()

    triggered = len(alerts)
    return jsonify({
        'ok': True,
        'triggered': triggered,
        'message': alerts[0]['message'] if alerts else None,
    })


# ------------------------- 监控记录 -------------------------

@app.route('/api/alerts')
def api_alerts():
    code = request.args.get('code', '')
    limit = int(request.args.get('limit', 200))
    conn = database.get_conn()
    if code:
        rows = conn.execute(
            'SELECT * FROM alert_log WHERE code=? ORDER BY id DESC LIMIT ?',
            (code, limit)).fetchall()
    else:
        rows = conn.execute(
            'SELECT * FROM alert_log ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ------------------------- 历史净值（走势图数据） -------------------------

@app.route('/api/history')
def api_history():
    code = request.args.get('code', '').strip()
    days = min(max(int(request.args.get('days', 30)), 7), 120)
    if not code:
        return jsonify({'error': '缺少 code 参数'}), 400
    conn = database.get_conn()
    # 关联子查询取每个 nav_date 最新一条（SQLite 与 PostgreSQL 均兼容，GROUP BY 写法在 PG 不合法）
    hist_sql = (
        'SELECT h.nav_date AS date, h.unit_nav AS nav, h.actual_change AS change '
        'FROM nav_history h WHERE h.code=? AND h.unit_nav IS NOT NULL '
        'AND h.id = (SELECT MAX(h2.id) FROM nav_history h2 '
        '            WHERE h2.code=h.code AND h2.nav_date=h.nav_date) '
        'ORDER BY h.nav_date DESC LIMIT ?')
    rows = conn.execute(hist_sql, (code, days)).fetchall()
    have = {r['date'] for r in rows}
    if len(have) < days:
        # 本地数据不足，从接口补抓（nav_history 无唯一约束，仅插入缺失日期）
        hist = fund_data.fetch_history(code, days)
        for h in hist:
            if h['date'] not in have:
                conn.execute(
                    'INSERT INTO nav_history (code, nav_date, unit_nav, acc_nav, '
                    'actual_change, estimated_nav, estimated_change, gztime, fetched_at) '
                    'VALUES (?,?,?,?,?,?,?,?,?)',
                    (code, h['date'], h['nav'], h['acc_nav'], h['change'],
                     None, None, None, rule_engine.now_str()))
        conn.commit()
        rows = conn.execute(hist_sql, (code, days)).fetchall()
    conn.close()
    out = [dict(r) for r in rows]
    out.reverse()  # 升序返回
    return jsonify(out)


# ------------------------- 概览统计 -------------------------

@app.route('/api/indices')
def api_indices():
    """指数实时行情（60 秒内存缓存，不入库不参与预警）"""
    try:
        return jsonify(fund_data.fetch_indices())
    except Exception:
        return jsonify([])


@app.route('/api/index_history')
def api_index_history():
    """指数走势（日K + 当日分时），供详情抽屉画图

    纯展示用途：失败不抛 500 给前端，而是带着 errors 字段返回可用部分，
    让前端能"有什么画什么"。secid 必须白名单命中，否则 400。
    """
    secid = request.args.get('secid', '')
    try:
        days = int(request.args.get('days', 300))
    except (TypeError, ValueError):
        days = 300
    try:
        return jsonify(fund_data.fetch_index_history(secid, days=days))
    except ValueError as e:
        return jsonify({'error': str(e), 'allowed': sorted(fund_data.ALLOWED_SECIDS)}), 400
    except Exception as e:
        return jsonify({'error': '走势数据获取失败：%s' % e}), 500


@app.route('/api/index_probe')
def api_index_probe():
    """诊断：逐个列出指数走势数据源的原始结果与错误（同 /api/sector_probe 的路子）"""
    secid = request.args.get('secid')
    try:
        if secid:
            return jsonify(fund_data.probe_index(secid))
        return jsonify([fund_data.probe_index(s) for s in sorted(fund_data.ALLOWED_SECIDS)])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/summary')
def api_summary():
    today = rule_engine.today_str()
    conn = database.get_conn()
    funds = conn.execute('SELECT * FROM funds WHERE enabled=1').fetchall()
    up = down = flat = 0
    for f in funds:
        last = conn.execute(
            'SELECT estimated_change, actual_change FROM nav_history '
            'WHERE code=? AND (estimated_change IS NOT NULL OR actual_change IS NOT NULL) '
            'ORDER BY id DESC LIMIT 1', (f['code'],)).fetchone()
        chg = None
        if last:
            chg = last['estimated_change'] if last['estimated_change'] is not None \
                else last['actual_change']
        if chg is None:
            continue
        if chg > 0:
            up += 1
        elif chg < 0:
            down += 1
        else:
            flat += 1
    today_alerts = conn.execute(
        "SELECT COUNT(*) AS c FROM alert_log WHERE trigger_time LIKE ?", (today + '%',)
    ).fetchone()['c']
    latest = conn.execute(
        'SELECT fetched_at FROM nav_history ORDER BY id DESC LIMIT 1').fetchone()
    conn.close()
    return jsonify({
        'fund_count': len(funds),
        'up_count': up, 'down_count': down, 'flat_count': flat,
        'today_alerts': today_alerts,
        'last_scan': latest['fetched_at'] if latest else None,
    })


@app.route('/api/sector_probe')
def api_sector_probe():
    """板块接口诊断：从海外机房（Render）访问东财 push2 的真实返回

    板块信息只是快报的附加项，抓不到不影响主流程 —— 但需要能看到"为什么抓不到"，
    否则只能看到快报里默默少了一行。
    """
    attempts = fund_data.probe_sectors(3)
    return jsonify({'ok': any(a['leading'] or a['lagging'] for a in attempts),
                    'attempts': attempts,
                    'effective': fund_data.fetch_sectors(2)})


@app.route('/api/alerts/<int:aid>', methods=['DELETE'])
def api_delete_alert(aid):
    """删除一条监控记录（用于清掉误报/无用记录）

    先 SELECT 判断存在性：PgConn.execute 返回的是包装对象、没有 rowcount，
    SQLite 才有，靠 rowcount 判断会在 PG 上永远返回 ok。
    """
    conn = database.get_conn()
    try:
        row = conn.execute('SELECT id FROM alert_log WHERE id=?', (aid,)).fetchone()
        if not row:
            return jsonify({'error': '记录不存在'}), 404
        conn.execute('DELETE FROM alert_log WHERE id=?', (aid,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True, 'id': aid})


# ------------------------- 配置 -------------------------

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'GET':
        cfg = load_config()
        # 归一化返回，保证前端拿到的永远是补好默认值的完整结构
        cfg['intraday_brief'] = _intraday_cfg(cfg)
        return jsonify(cfg)
    data = request.get_json(silent=True) or {}
    cfg = load_config()
    for k in ('serverchan_sendkey', 'pushplus_token',
              'scan_interval_seconds', 'off_hours_interval_seconds'):
        if k in data:
            cfg[k] = data[k]
    if 'daily_summary' in data:
        ds = data['daily_summary'] or {}
        cfg['daily_summary'] = {
            'enabled': bool(ds.get('enabled')),
            'time': str(ds.get('time') or '20:00'),
        }
    if 'index_alert' in data:
        ia = data['index_alert'] or {}
        cfg['index_alert'] = {
            'enabled': bool(ia.get('enabled')),
            'threshold': float(ia.get('threshold') or 3),
        }
    if 'index_summary' in data:
        idxs = data['index_summary'] or {}
        cfg['index_summary'] = {
            'enabled': bool(idxs.get('enabled')),
            'time': str(idxs.get('time') or '20:00'),
        }
    if 'us_index_summary' in data:
        usix = data['us_index_summary'] or {}
        cfg['us_index_summary'] = {
            'enabled': bool(usix.get('enabled')),
            'time': str(usix.get('time') or '08:00'),
        }
    if 'intraday_brief' in data:
        ib = data['intraday_brief'] or {}
        norm = _intraday_cfg({'intraday_brief': ib})
        cfg['intraday_brief'] = norm
    if 'email' in data:
        cfg['email'] = data['email']
    save_config(cfg)
    return jsonify({'ok': True})


@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    fetched, alerts, stage = scan_once()
    return jsonify({'ok': True, 'fetched': fetched, 'alerts': alerts, 'stage': stage})


@app.route('/api/test_notify', methods=['POST'])
def api_test_notify():
    result = notifier.send_alert(
        load_config(), '基金涨跌监控 · 测试消息',
        '这是一条测试提醒，收到说明提醒渠道配置正确。')
    return jsonify(result)


# ------------------------- Web Push 订阅 -------------------------

@app.route('/api/vapid_public_key')
def api_vapid_public_key():
    """返回 VAPID 公钥（base64url raw），前端拿它作为 applicationServerKey 订阅"""
    import vapid
    _, pub = vapid.get_vapid_keys()
    return jsonify({'public_key': pub})


@app.route('/api/subscribe', methods=['POST'])
def api_subscribe():
    """接收前端 PushSubscription，存 DB（按 endpoint 去重）"""
    data = request.get_json(silent=True) or {}
    sub = data.get('subscription') or {}
    endpoint = sub.get('endpoint', '')
    keys = sub.get('keys') or {}
    p256dh = keys.get('p256dh', '')
    auth = keys.get('auth', '')
    if not endpoint:
        return jsonify({'error': '缺少 endpoint'}), 400
    ua = (request.headers.get('User-Agent') or '')[:200]
    database.save_sub(endpoint, p256dh, auth, ua=ua)
    # 订阅成功立刻给这一台发一条验证推送：既能立刻确认链路通不通，
    # 也是一次送达回执（SW 收到后会回写 last_ack_at）。
    welcome = None
    try:
        mine = [s for s in database.get_subs() if s.get('endpoint') == endpoint]
        if mine:
            w = notifier.send_webpush(
                mine, '基金涨跌监控 · 推送已开启',
                '本设备已成功订阅。基金触发预警时会弹这条通知，网页关闭也能收到。')
            welcome = {'sent': w['sent'], 'failed': w['failed'], 'errors': w['errors']}
    except Exception as e:
        welcome = {'error': '%s: %s' % (type(e).__name__, str(e)[:150])}
    return jsonify({'ok': True, 'welcome': welcome})


@app.route('/api/unsubscribe', methods=['POST'])
def api_unsubscribe():
    """删除订阅（前端关闭推送权限时调用）"""
    data = request.get_json(silent=True) or {}
    endpoint = (data.get('endpoint') or '').strip()
    if not endpoint:
        return jsonify({'error': '缺少 endpoint'}), 400
    database.del_sub(endpoint)
    return jsonify({'ok': True})


@app.route('/api/push_status')
def api_push_status():
    """返回当前 push 订阅数量 + 明细（只暴露推送服务域名和端点尾号，不暴露完整令牌）"""
    import database
    subs = database.get_subs()
    items = []
    for s in subs:
        ep = s.get('endpoint') or ''
        try:
            host = ep.split('/')[2]
        except Exception:
            host = '?'
        # ua 用来区分手机/电脑；last_ack_at 是唯一可信的"送达"证据
        items.append({'host': host, 'tail': ep[-10:], 'created_at': s.get('created_at'),
                      'ua': s.get('ua') or '', 'last_ack_at': s.get('last_ack_at')})
    return jsonify({'count': len(subs), 'items': items})


@app.route('/api/push_ack', methods=['POST'])
def api_push_ack():
    """Service Worker 收到 push 并成功弹出通知后回调这里。

    这是"到底送没送到"的唯一硬证据：FCM 返回 2xx 只代表消息入队。
    没有回执 = 手机侧从未收到（网络断开 / 进程被杀 / 通知被系统拦）。
    """
    data = request.get_json(silent=True) or {}
    sid = data.get('sid')
    if not sid:
        return jsonify({'error': '缺少 sid'}), 400
    try:
        database.ack_sub(int(sid))
    except (TypeError, ValueError):
        return jsonify({'error': 'sid 非法'}), 400
    return jsonify({'ok': True})


@app.route('/api/version')
def api_version():
    """返回代码版本，用于确认 Render 部署的是哪个 commit（不碰 DB）"""
    return jsonify({'version': '3.23', 'commit': 'selftest-ab'})


@app.route('/api/threads')
def api_threads():
    """线程栈诊断：实例"卡死"时用它看清到底堵在哪

    症状是**所有**接口（连 /static/app.js 这种不碰库不碰网络的）都超时，
    说明 worker 的线程全被占住了。黑盒只能看到"没响应"，看不到是谁堵的，
    所以把每个线程的调用栈打出来（同 /api/db_diag 的思路）。
    """
    import sys
    import traceback
    frames = sys._current_frames()
    items = []
    for th in threading.enumerate():
        fr = frames.get(th.ident)
        stack = []
        if fr is not None:
            stack = [ln.strip() for ln in traceback.format_stack(fr)[-8:]]
        items.append({'name': th.name, 'daemon': th.daemon, 'stack': stack})
    return jsonify({
        'count': len(items),
        # 调度线程死了 = 盘中快报和所有定时汇总都不会再触发（而 cron 仍在打
        # /api/refresh，表面看不出问题），所以单独给出一个明确信号
        'scheduler_alive': bool(_sched_thread and _sched_thread.is_alive()),
        'scheduler': _sched_state,
        'threads': items,
    })


@app.route('/api/db_diag')
def api_db_diag():
    """数据库连接诊断：返回 get_conn + 查询的详细错误（不吞异常）"""
    import traceback
    try:
        conn = database.get_conn()
        row = conn.execute('SELECT 1 AS ok').fetchone()
        conn.close()
        return jsonify({'ok': True, 'result': dict(row) if row else None})
    except Exception as e:
        return jsonify({'ok': False, 'type': type(e).__name__,
                        'error': str(e), 'trace': traceback.format_exc()})


_scheduler_started = False
_scheduler_lock = threading.Lock()
_sched_thread = None
_sched_last_start = 0.0


def ensure_scheduler():
    """确保调度线程活着；死了就重新拉起（幂等）。

    为什么不只在 import 时起一次：调度线程是**唯一**触发盘中快报和各种定时
    汇总的地方，而 cron 仍会正常打 /api/refresh、页面也照常能用 ——
    它一旦没起来（import 期异常 / fork 之后线程不存在），
    表面完全看不出来，只会表现为"该推的都没推"。
    所以每个请求都顺手确认一次，线程没了就补一个；最短 60s 才重启一次，
    避免万一线程秒崩造成反复创建。
    """
    global _scheduler_started, _sched_thread, _sched_last_start
    with _scheduler_lock:
        if _sched_thread is not None and _sched_thread.is_alive():
            return False
        if time.time() - _sched_last_start < 60:
            return False
        # 起个名字，方便在 /api/threads 的线程栈里一眼认出来
        _sched_thread = threading.Thread(target=scheduler_loop, daemon=True,
                                         name='fund-scheduler')
        _sched_thread.start()
        _sched_last_start = time.time()
        _scheduler_started = True
        _boot['scheduler_pid'] = os.getpid()
        return True


def start_scheduler():
    """兼容旧调用点：启动后台调度线程"""
    ensure_scheduler()


@app.before_request
def _keep_scheduler_alive():
    """每个请求顺手确认调度线程还活着（幂等，正常路径只做一次 is_alive 判断）。

    这是兜底：调度线程缺失时接口全都"正常"，只有靠它自愈才不会静默失效。
    """
    try:
        ensure_scheduler()
    except Exception as e:
        print('ensure_scheduler error:', e)


@app.route('/api/boot')
def api_boot():
    """启动留痕 + 调度线程现状，用于排查"该推的没推"这类静默故障"""
    return jsonify({
        'boot': _boot,
        'scheduler_alive': bool(_sched_thread and _sched_thread.is_alive()),
        'scheduler': _sched_state,
        'pid': os.getpid(),
    })


# 后台初始化：不阻塞 app 启动。Neon 冷启动时 init_db 可能卡住，若在
# 模块级同步执行会导致 gunicorn 起不来、Render 部署失败回滚到旧代码。
# init_db 与 start_scheduler 移到后台线程；scheduler_loop 内每轮也会
# 幂等重试 init_db，Neon 恢复后自动建表并开始扫描。
# 启动过程留痕：调度线程没起来时，光看接口是"一切正常"的，
# 必须能回看到底哪一步没走成（/api/boot）
_boot = {'at': None, 'pid': None, 'skip_env': None, 'init_db_error': None,
         'scheduler_pid': None}


def _bootstrap():
    """import 期初始化：只做建表，**不启动调度线程**。

    ⚠️ 关键教训（2026-09-14 实测）：gunicorn 会在 master 进程里 import 应用，
    然后 fork 出 worker 来处理请求 —— 而**线程不会跟着 fork 过去**。
    实测证据：/api/boot 记录的 pid=57（master，bootstrap 在这里跑），
    但同一响应里 os.getpid()=60（真正的 worker）。所以在这里起的调度线程
    只活在 master 里，worker 侧永远看不到它。

    因此调度线程统一交给 ensure_scheduler()，在真正处理请求的进程里拉起
    （见 before_request 钩子）；本地 `python app.py` 在 __main__ 里显式启动。
    这样也顺带修掉一个隐患：worker 被回收重建时，调度器会随首个请求自动回来。
    """
    _boot['at'] = rule_engine.now_str()
    _boot['pid'] = os.getpid()
    _boot['skip_env'] = os.environ.get('FUNDWATCH_NO_SCHEDULER')
    try:
        database.init_db()
    except Exception as e:
        _boot['init_db_error'] = '%s: %s' % (type(e).__name__, str(e)[:300])
        print('bootstrap init_db error (scheduler will retry):', e)


# 自测（selftest.py）只想拿到纯函数，不需要连库。
# 该开关让 app 可以被安全 import。注意这里**只建表、不起调度线程**。
if not os.environ.get('FUNDWATCH_NO_SCHEDULER'):
    threading.Thread(target=_bootstrap, daemon=True).start()


def main():
    # 本地直跑不会走 gunicorn 的 master/fork 那套，
    # 没有请求进来 before_request 也不会触发 → 这里显式起调度线程
    ensure_scheduler()
    app.run(host='127.0.0.1', port=5000, debug=False)


if __name__ == '__main__':
    main()
