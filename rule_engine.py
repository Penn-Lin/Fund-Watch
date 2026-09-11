# -*- coding: utf-8 -*-
"""规则引擎：当日涨跌阈值 + 累计涨跌节点"""
import datetime
import database

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo('Asia/Shanghai')
except Exception:
    _TZ = datetime.timezone(datetime.timedelta(hours=8))


def now():
    """当前北京时间——全系统统一时间基准。

    服务器(Render)为 UTC，若直接用 datetime.now() 会导致 A 股交易时段
    判断(9:30-15:00)整体错 8 小时、日期切分错位、盘中估值预警与收盘汇总
    的触发时机不准。所有时间判断与时间戳入库统一走北京时间。
    """
    return datetime.datetime.now(_TZ)


def now_str():
    return now().strftime('%Y-%m-%d %H:%M:%S')


def today_str():
    return now().strftime('%Y-%m-%d')


def is_trading_day(dt=None):
    dt = dt or now()
    return dt.weekday() < 5


def is_market_hours(dt=None):
    dt = dt or now()
    hm = dt.hour * 100 + dt.minute
    return 930 <= hm <= 1500


def _hit_direction(cum, threshold, direction):
    """返回实际触发的方向，未触发返回 None"""
    if direction in ('up', 'both') and cum >= threshold:
        return 'up'
    if direction in ('down', 'both') and cum <= -threshold:
        return 'down'
    return None


def _already_alerted(conn, code, rule_type, direction, kind, date):
    row = conn.execute(
        'SELECT COUNT(*) AS c FROM alert_log WHERE code=? AND rule_type=? '
        'AND direction=? AND kind=? AND trigger_time LIKE ?',
        (code, rule_type, direction, kind, date + '%')).fetchone()
    return row['c'] > 0


def _mk_alert(code, name, rule_type, direction, kind, change, msg):
    return {
        'code': code, 'name': name, 'rule_type': rule_type,
        'direction': direction, 'kind': kind,
        'current_change': change, 'message': msg,
    }


def _get_rule(conn, code, rule_type):
    r = conn.execute(
        "SELECT * FROM rules WHERE enabled=1 AND rule_type=? AND code=? ORDER BY id LIMIT 1",
        (rule_type, code)).fetchone()
    if r:
        return r
    r = conn.execute(
        "SELECT * FROM rules WHERE enabled=1 AND rule_type=? AND code='GLOBAL' ORDER BY id LIMIT 1",
        (rule_type,)).fetchone()
    return r


def _eval_daily(conn, rule, d, tstr):
    # 只在有"当日"涨跌数据时评估，避免非交易日误用上一交易日涨跌
    if d.get('estimated_change') is not None:
        change = d['estimated_change']  # 盘中当日估值涨跌
    elif d.get('nav_date') == tstr:
        change = d.get('actual_change')  # 当日净值已确认
    else:
        return []
    hit = _hit_direction(change, rule['threshold'], rule['direction'])
    if not hit:
        return []
    if _already_alerted(conn, d['code'], 'daily', hit, 'daily', tstr):
        return []
    up_down = '上涨' if hit == 'up' else '下跌'
    msg = '【%s】(%s) 当日%s %.2f%%，达到阈值 %.2f%%' % (
        d['name'], d['code'], up_down, change, rule['threshold'])
    return [_mk_alert(d['code'], d['name'], 'daily', hit, 'daily', change, msg)]


def _eval_cumulative(conn, rule, d, now, tstr):
    code = d['code']
    name = d['name']
    threshold = rule['threshold']
    direction = rule['direction']
    alerts = []

    state = conn.execute(
        'SELECT * FROM cumulative_state WHERE rule_id=? AND code=?',
        (rule['id'], code)).fetchone()
    if state is None or not state['baseline_nav'] or state['baseline_nav'] <= 0:
        base = d['unit_nav'] if d['unit_nav'] else d['estimated_nav']
        if base:
            conn.execute(
                'INSERT INTO cumulative_state (rule_id, code, baseline_nav, baseline_date) '
                'VALUES (?,?,?,?)', (rule['id'], code, base, tstr))
            conn.commit()
        return []

    base = state['baseline_nav']

    # 盘中：实时估值预警（不重置基准，待收盘净值确认）
    if (is_trading_day(now) and is_market_hours(now)
            and d['estimated_nav'] is not None and d['estimated_nav'] > 0):
        cum = (d['estimated_nav'] - base) / base * 100
        hit = _hit_direction(cum, threshold, direction)
        if hit and not _already_alerted(conn, code, 'cumulative', hit, 'cum_estimate', tstr):
            up_down = '上涨' if hit == 'up' else '下跌'
            msg = '【%s】(%s) 累计%s已达 %.2f%%（估值预警，收盘净值确认后定基），节点 %.2f%%' % (
                name, code, up_down, cum, threshold)
            alerts.append(_mk_alert(code, name, 'cumulative', hit, 'cum_estimate', cum, msg))

    # 当日净值确认：以确认净值定基并重置基准
    if d['nav_date'] == tstr and d['unit_nav'] and d['unit_nav'] > 0:
        cum = (d['unit_nav'] - base) / base * 100
        hit = _hit_direction(cum, threshold, direction)
        if hit and not _already_alerted(conn, code, 'cumulative', hit, 'cum_confirm', tstr):
            up_down = '上涨' if hit == 'up' else '下跌'
            msg = '【%s】(%s) 累计%s已达 %.2f%%，触发节点并重置基准' % (
                name, code, up_down, cum)
            alerts.append(_mk_alert(code, name, 'cumulative', hit, 'cum_confirm', cum, msg))
            conn.execute(
                'UPDATE cumulative_state SET baseline_nav=?, baseline_date=? WHERE id=?',
                (d['unit_nav'], tstr, state['id']))
            conn.commit()

    return alerts


def evaluate_baseline_reset(code):
    """手动重置基准后立即评估单只基金。

    用最新净值（不限当日）计算累计涨跌，若已穿越阈值则生成 cum_confirm
    提醒并重置基准为当前净值——实现"每跌4%定投"的核心逻辑：
    用户设一个高点基准，若当前已跌超4%立即触发定投信号，并从当前
    净值重新累计下一段4%。

    与 _eval_cumulative 的区别：不依赖 nav_date==today 条件，非交易日/
    盘前/盘中均可评估，用于手动设置基准后的即时判定。
    """
    conn = database.get_conn()
    tstr = today_str()
    fund = conn.execute(
        'SELECT * FROM funds WHERE code=? AND enabled=1', (code,)).fetchone()
    if not fund:
        conn.close()
        return []
    last = conn.execute(
        'SELECT * FROM nav_history WHERE code=? ORDER BY id DESC LIMIT 1',
        (code,)).fetchone()
    if not last:
        conn.close()
        return []
    cum_rule = _get_rule(conn, code, 'cumulative')
    if not cum_rule:
        conn.close()
        return []
    state = conn.execute(
        'SELECT * FROM cumulative_state WHERE rule_id=? AND code=?',
        (cum_rule['id'], code)).fetchone()
    if not state or not state['baseline_nav'] or state['baseline_nav'] <= 0:
        conn.close()
        return []
    base = state['baseline_nav']
    # 用最新净值（优先 unit_nav，回退 estimated_nav）
    nav = last['unit_nav']
    if not nav or nav <= 0:
        nav = last['estimated_nav']
    if not nav or nav <= 0:
        conn.close()
        return []
    cum = (nav - base) / base * 100
    hit = _hit_direction(cum, cum_rule['threshold'], cum_rule['direction'])
    alerts = []
    if hit and not _already_alerted(
            conn, code, 'cumulative', hit, 'cum_confirm', tstr):
        up_down = '上涨' if hit == 'up' else '下跌'
        msg = ('【%s】(%s) 累计%s已达 %.2f%%（手动设置基准后即时评估），'
               '触发节点并重置基准为当前净值 %.4f') % (
            fund['name'], code, up_down, cum, nav)
        alerts.append(_mk_alert(code, fund['name'], 'cumulative', hit,
                                'cum_confirm', cum, msg))
        conn.execute(
            'UPDATE cumulative_state SET baseline_nav=?, baseline_date=? WHERE id=?',
            (nav, tstr, state['id']))
        conn.commit()
    conn.close()
    return alerts


def evaluate_all():
    """评估所有启用基金的规则，返回需要提醒的 alert 列表"""
    conn = database.get_conn()
    alerts = []
    now = now()
    tstr = today_str()
    funds = conn.execute('SELECT * FROM funds WHERE enabled=1').fetchall()
    for f in funds:
        last = conn.execute(
            'SELECT * FROM nav_history WHERE code=? ORDER BY id DESC LIMIT 1',
            (f['code'],)).fetchone()
        if not last:
            continue
        d = {
            'code': f['code'], 'name': f['name'],
            'unit_nav': last['unit_nav'],
            'estimated_nav': last['estimated_nav'],
            'estimated_change': last['estimated_change'],
            'actual_change': last['actual_change'],
            'nav_date': last['nav_date'],
        }
        daily_rule = _get_rule(conn, f['code'], 'daily')
        if daily_rule:
            alerts += _eval_daily(conn, daily_rule, d, tstr)
        cum_rule = _get_rule(conn, f['code'], 'cumulative')
        if cum_rule:
            alerts += _eval_cumulative(conn, cum_rule, d, now, tstr)
    conn.close()
    return alerts
