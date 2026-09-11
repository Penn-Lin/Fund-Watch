# -*- coding: utf-8 -*-
"""通知：Server酱 / PushPlus（微信推送）+ 邮件 + Web Push（浏览器系统推送）"""
import json
import smtplib
from email.mime.text import MIMEText
import requests


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
        server = smtplib.SMTP_SSL(ec['smtp_host'], int(ec.get('smtp_port', 465)), timeout=15)
    else:
        server = smtplib.SMTP(ec['smtp_host'], int(ec.get('smtp_port', 25)), timeout=15)
        server.starttls()
    server.login(ec['username'], ec['password'])
    server.sendmail(from_addr, to_addrs, msg.as_string())
    server.quit()
    return True


def send_webpush(subs, title, content):
    """给一组 push 订阅推送系统通知，返回 {'sent': n, 'failed': n, 'gone': [endpoints]}

    410 Gone / 404 表示客户端已取消订阅，调用方应删除这些记录。
    """
    import vapid
    from pywebpush import webpush

    priv, _ = vapid.get_vapid_keys()
    claims = vapid.get_vapid_claims()
    payload = json.dumps({'title': title, 'body': content}, ensure_ascii=False)
    sent = failed = 0
    gone = []
    for s in subs:
        sub_info = {
            'endpoint': s['endpoint'],
            'keys': {'p256dh': s['p256dh'], 'auth': s['auth']},
        }
        try:
            r = webpush(
                subscription_info=sub_info,
                data=payload,
                vapid_private_key=priv,
                vapid_claims=claims,
                timeout=10,
            )
            if r.status_code in (200, 201):
                sent += 1
            elif r.status_code in (404, 410):
                gone.append(s['endpoint'])
                failed += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return {'sent': sent, 'failed': failed, 'gone': gone}


def send_alert(cfg, title, content):
    """按配置依次尝试各渠道，返回 {'ok': bool, 'channels': {渠道: 是否成功}}

    webpush 不依赖 cfg（订阅存 DB），只要有订阅就推。
    """
    results = {}
    sk = (cfg.get('serverchan_sendkey') or '').strip()
    if sk:
        try:
            results['serverchan'] = send_serverchan(sk, title, content)
        except Exception:
            results['serverchan'] = False
    pt = (cfg.get('pushplus_token') or '').strip()
    if pt:
        try:
            results['pushplus'] = send_pushplus(pt, title, content)
        except Exception:
            results['pushplus'] = False
    ec = cfg.get('email') or {}
    if ec.get('smtp_host') and ec.get('username') and ec.get('to_addrs'):
        try:
            results['email'] = send_email(ec, title, content)
        except Exception as e:
            results['email'] = False
            results['email_error'] = str(e)[:200]  # 暴露真实失败原因（授权码错/海外IP被拒等）

    # Web Push：从 DB 查所有订阅推送，失效订阅自动清理
    wp_detail = None
    try:
        import database
        subs = database.get_subs()
        if subs:
            wp = send_webpush(subs, title, content)
            for ep in wp['gone']:
                database.del_sub(ep)
            results['webpush'] = wp['sent'] > 0
            wp_detail = wp
    except Exception:
        results['webpush'] = False

    out = {'ok': any(results.values()), 'channels': results}
    if wp_detail:
        out['webpush_detail'] = wp_detail
    return out
