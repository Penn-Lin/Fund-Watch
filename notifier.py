# -*- coding: utf-8 -*-
"""通知：Server酱 / PushPlus（微信推送）+ 邮件 + Web Push（浏览器系统推送）"""
import json
import smtplib
import time
from concurrent.futures import ThreadPoolExecutor
from email.mime.text import MIMEText
import requests

# SMTP 单次 socket 操作超时。Render 免费层封禁 25/465/587，connect 会被防火墙
# 静默丢包（不返回 RST），只能等超时，所以这个值直接决定「测试推送」卡多久。
SMTP_TIMEOUT = 10

# SMTP 熔断：Render 免费层封了 25/465/587，connect 被静默丢包，每次都要等满
# SMTP_TIMEOUT 才失败。自动提醒是按条串行发送的，如果不熔断，第 N 条的浏览器推送
# 会被前 N-1 条的邮件等待一层层往后推。一旦确认是网络层拒绝，冷却期内直接跳过。
EMAIL_BLOCK_COOLDOWN = 1800  # 秒
_email_block = {'until': 0.0, 'key': ''}


def _is_network_block(err_text):
    """区分「SMTP 被网络层阻断」和「账号/授权码错」——前者才值得熔断"""
    t = (err_text or '').lower()
    return any(s in t for s in (
        'network is unreachable', 'no route to host', 'connection refused',
        'timed out', 'timeout', 'errno 101', 'errno 110', 'errno 111', 'errno 113',
    ))


def send_serverchan(sendkey, title, content):
    r = requests.post(
        'https://sctapi.ftqq.com/%s.send' % sendkey,
        data={'title': title, 'desp': content}, timeout=10)
    return r.json().get('code') == 0


def send_pushplus(token, title, content):
    r = requests.post(
        'http://www.pushplus.plus/send',
        json={'token': token, 'title': title, 'content': content, 'template': 'txt'},
        timeout=10)
    return r.json().get('code') == 200


def send_email(ec, title, content):
    msg = MIMEText(content, 'plain', 'utf-8')
    msg['Subject'] = title
    from_addr = ec.get('from_addr') or ec.get('username')
    msg['From'] = from_addr
    to_addrs = ec.get('to_addrs') or []
    msg['To'] = ','.join(to_addrs)
    if ec.get('use_ssl'):
        server = smtplib.SMTP_SSL(ec['smtp_host'], int(ec.get('smtp_port', 465)), timeout=SMTP_TIMEOUT)
    else:
        server = smtplib.SMTP(ec['smtp_host'], int(ec.get('smtp_port', 25)), timeout=SMTP_TIMEOUT)
        server.starttls()
    server.login(ec['username'], ec['password'])
    server.sendmail(from_addr, to_addrs, msg.as_string())
    server.quit()
    return True


def send_webpush(subs, title, content):
    """给一组 push 订阅推送系统通知，
    返回 {'sent': n, 'failed': n, 'gone': [endpoints], 'errors': [{'endpoint','error'}]}

    410 Gone / 404 表示客户端已取消订阅，调用方应删除这些记录。
    401/403 表示该订阅不是用当前 VAPID 密钥创建的（密钥轮换/测试残留），
    永远推不通，同样删掉，避免每次发送都留下一条"失败"。
    """
    import vapid
    from pywebpush import webpush

    # 注意：必须传 Vapid 对象。pywebpush 只在 key 是 Vapid 实例时直接用，
    # 否则走 Vapid.from_string() —— 那个函数只认 base64url 密钥，喂 PEM 必然报错。
    signer = vapid.get_vapid_signer()
    claims = vapid.get_vapid_claims()
    payload = json.dumps({'title': title, 'body': content}, ensure_ascii=False)
    sent = failed = 0
    gone = []
    errors = []
    for s in subs:
        ep = s['endpoint']
        sub_info = {
            'endpoint': ep,
            'keys': {'p256dh': s['p256dh'], 'auth': s['auth']},
        }
        try:
            r = webpush(
                subscription_info=sub_info,
                data=payload,
                vapid_private_key=signer,
                vapid_claims=claims,
                timeout=10,
            )
            if r.status_code in (200, 201):
                sent += 1
            elif r.status_code in (404, 410):
                gone.append(ep)
                failed += 1
                errors.append({'endpoint': ep[-24:], 'error': 'HTTP %s (订阅已失效)' % r.status_code})
            else:
                failed += 1
                errors.append({'endpoint': ep[-24:],
                               'error': 'HTTP %s %s' % (r.status_code, (r.text or '')[:150])})
        except Exception as e:
            failed += 1
            # pywebpush 对非 2xx 一律抛 WebPushException，状态码在 e.response 里
            st = getattr(getattr(e, 'response', None), 'status_code', None)
            if st in (401, 403, 404, 410):
                gone.append(ep)  # 永久推不通：订阅失效或 VAPID 密钥不匹配
            # 不再静默吞掉：把真实原因带出来，便于在 /api/test_notify 里直接看到
            errors.append({'endpoint': ep[-24:],
                           'error': 'HTTP %s %s: %s' % (st, type(e).__name__, str(e)[:150])})
    return {'sent': sent, 'failed': failed, 'gone': gone, 'errors': errors[:5]}


def send_alert(cfg, title, content):
    """推送各渠道，返回 {'ok': bool, 'channels': {...}, 'timing': {渠道_ms: ms}}

    顺序是关键（曾被这个问题坑过）：
    - Web Push 走 HTTPS，1~3 秒就能到，必须**排在第一个**；
    - 邮件走 SMTP，Render 免费层封了 25/465/587，connect 被静默丢包，
      单次要卡到 SMTP_TIMEOUT 才报错。如果它排前面（旧的顺序），
      用户点「测试推送」后要等十几秒才收到浏览器推送。
    - serverchan / pushplus / email 之间用线程并行，互不拖累。
    """
    results = {}
    timing = {}
    jobs = {}  # 渠道名 -> 无参函数

    # ---------- 1) Web Push 先发（最快、且是用户主要依赖的渠道） ----------
    wp_detail = None
    t0 = time.time()
    try:
        import database
        subs = database.get_subs()
        if subs:
            wp = send_webpush(subs, title, content)
            for ep in wp['gone']:
                database.del_sub(ep)
            results['webpush'] = wp['sent'] > 0
            wp_detail = wp
    except Exception as e:
        results['webpush'] = False
        results['webpush_error'] = '%s: %s' % (type(e).__name__, str(e)[:150])
    timing['webpush_ms'] = int((time.time() - t0) * 1000)

    # ---------- 2) 其余渠道并行 ----------
    sk = (cfg.get('serverchan_sendkey') or '').strip()
    if sk:
        jobs['serverchan'] = lambda: send_serverchan(sk, title, content)
    pt = (cfg.get('pushplus_token') or '').strip()
    if pt:
        jobs['pushplus'] = lambda: send_pushplus(pt, title, content)
    ec = cfg.get('email') or {}
    email_key = '%s:%s:%s' % (ec.get('smtp_host'), ec.get('smtp_port'), ec.get('username'))
    if _email_block['key'] != email_key:
        # SMTP 配置变了（换服务商/账号）→ 解除熔断，重新给一次机会
        _email_block['key'] = email_key
        _email_block['until'] = 0.0
    if ec.get('smtp_host') and ec.get('username') and ec.get('to_addrs'):
        if time.time() < _email_block['until']:
            left = int((_email_block['until'] - time.time()) / 60) + 1
            results['email'] = False
            results['email_error'] = ('SMTP 已熔断：连接被网络层阻断（Render 免费层封禁 25/465/587），'
                                      '约 %d 分钟后才会重试，避免每次都白等 %ds' % (left, SMTP_TIMEOUT))
            timing['email_ms'] = 0
        else:
            jobs['email'] = lambda: send_email(ec, title, content)

    if jobs:
        started = {}
        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            futs = {}
            for name, fn in jobs.items():
                started[name] = time.time()
                futs[name] = ex.submit(fn)
            for name, fut in futs.items():
                try:
                    results[name] = bool(fut.result(timeout=SMTP_TIMEOUT + 10))
                except Exception as e:
                    results[name] = False
                    # 暴露真实失败原因（授权码错 / 海外 IP 被拒绝 / 端口被封）
                    results[name + '_error'] = '%s: %s' % (type(e).__name__, str(e)[:180])
                    if name == 'email' and _is_network_block(str(e)):
                        _email_block['until'] = time.time() + EMAIL_BLOCK_COOLDOWN
                timing[name + '_ms'] = int((time.time() - started[name]) * 1000)

    out = {
        'ok': any(v for k, v in results.items() if not k.endswith('_error')),
        'channels': results,
        'timing': timing,
        'total_ms': sum(v for k, v in timing.items() if k != 'webpush_ms') + timing.get('webpush_ms', 0),
    }
    if wp_detail:
        out['webpush_detail'] = wp_detail
    return out
