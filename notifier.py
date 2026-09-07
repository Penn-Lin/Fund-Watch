# -*- coding: utf-8 -*-
"""通知：Server酱 / PushPlus（微信推送）+ 邮件"""
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


def send_alert(cfg, title, content):
    """按配置依次尝试各渠道，返回 {'ok': bool, 'channels': {渠道: 是否成功}}"""
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
        except Exception:
            results['email'] = False
    return {'ok': any(results.values()), 'channels': results}
