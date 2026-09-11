# -*- coding: utf-8 -*-
"""VAPID 密钥管理：环境变量优先 → DB config → 自动生成存 DB

Web Push 用 EC P-256 密钥对：
- 私钥（PEM）：后端推送签名用，绝不能暴露给前端
- 公钥（raw base64url）：前端 pushManager.subscribe 时作为 applicationServerKey

同一套密钥必须跨实例重启保持稳定，否则旧订阅全部失效。
所以本地无环境变量时自动生成一次并写入 DB config，下次直接读。
"""
import os
import base64
import json

import database


def _b64url(raw: bytes) -> str:
    """raw bytes → base64url 无填充字符串（前端 applicationServerKey 要求的格式）"""
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _generate_keys():
    """生成 EC P-256 密钥对，返回 (private_pem, public_b64url)"""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization

    priv = ec.generate_private_key(ec.SECP256R1())
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode('ascii')

    # raw 公钥：04 || x(32) || y(32) = 65 字节，前端要的就是这个
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    pub_b64url = _b64url(pub_raw)
    return priv_pem, pub_b64url


def get_vapid_keys():
    """返回 (private_pem, public_b64url)。

    优先级：
    1. 环境变量 VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY（生产部署用）
    2. DB config 里的 vapid.private_key / vapid.public_key
    3. 都没有 → 自动生成一次存 DB config

    环境变量里的公钥也接受 PEM 格式，自动转 raw base64url。
    """
    priv = os.environ.get('VAPID_PRIVATE_KEY', '').strip()
    pub = os.environ.get('VAPID_PUBLIC_KEY', '').strip()
    if priv and pub:
        return priv, _normalize_public_key(pub)

    cfg = database.get_config() or {}
    v = cfg.get('vapid') or {}
    if v.get('private_key') and v.get('public_key'):
        return v['private_key'], v['public_key']

    # 自动生成并持久化
    priv_pem, pub_b64url = _generate_keys()
    cfg['vapid'] = {'private_key': priv_pem, 'public_key': pub_b64url}
    # 保留已有配置项，合并写入
    database.save_config(cfg)
    return priv_pem, pub_b64url


def _normalize_public_key(pub: str) -> str:
    """环境变量里的公钥可能是 PEM 也可能是 base64url raw，统一输出 base64url raw"""
    pub = pub.strip()
    if pub.startswith('-----BEGIN'):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend
        try:
            k = serialization.load_pem_public_key(
                pub.encode('ascii'), backend=default_backend())
            raw = k.public_bytes(
                encoding=serialization.Encoding.X962,
                format=serialization.PublicFormat.UncompressedPoint,
            )
            return _b64url(raw)
        except Exception:
            return pub
    # 已经是 base64url，补齐可能的填充再校验长度
    pad = '=' * (-len(pub) % 4)
    try:
        raw = base64.urlsafe_b64decode(pub + pad)
        if len(raw) == 65:
            return pub
    except Exception:
        pass
    return pub


def get_vapid_claims():
    """构造 VAPID JWT claims，subject 用部署地址或本地占位"""
    subject = os.environ.get('VAPID_SUBJECT', 'mailto:noreply@fund-monitor.local')
    return {'sub': subject}
