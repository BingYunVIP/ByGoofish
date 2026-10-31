"""应用秘密的本地加密存储。主密钥由部署环境单独保存。"""
from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = 'enc:v1:'


def _key_path() -> Path:
    value = os.getenv('BYGOOFISH_MASTER_KEY_FILE', '').strip()
    if value:
        return Path(value).expanduser().resolve()
    return (Path(__file__).resolve().parents[2] / '.local' / 'bygoofish' / 'master.key').resolve()


def _load_key() -> bytes:
    path = _key_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        path.write_bytes(base64.urlsafe_b64encode(secrets.token_bytes(32)) + b'\n')
        path.chmod(0o600)
    raw = path.read_text(encoding='ascii').strip()
    try:
        key = base64.urlsafe_b64decode(raw.encode('ascii'))
    except Exception as exc:
        raise RuntimeError('应用主密钥格式无效') from exc
    if len(key) != 32:
        raise RuntimeError('应用主密钥长度无效')
    return key


def seal(value: str | None, context: str) -> str:
    if value is None:
        return ''
    text = str(value)
    if text.startswith(PREFIX):
        return text
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_load_key()).encrypt(nonce, text.encode('utf-8'), context.encode('utf-8'))
    return PREFIX + base64.urlsafe_b64encode(nonce + ciphertext).decode('ascii')


def open_secret(value: str | None, context: str) -> str:
    if value is None:
        return ''
    text = str(value)
    if not text.startswith(PREFIX):
        # One-time compatibility for data created before encryption. New writes
        # always use seal(); clearing the business database removes this path.
        return text
    try:
        raw = base64.urlsafe_b64decode(text[len(PREFIX):].encode('ascii'))
        return AESGCM(_load_key()).decrypt(raw[:12], raw[12:], context.encode('utf-8')).decode('utf-8')
    except Exception as exc:
        raise RuntimeError('应用秘密无法解密，请检查主密钥') from exc


def is_secret_setting(key: str) -> bool:
    key = str(key or '')
    return key in {'smtp_password', 'ai_api_key', 'qq_reply_secret_key'} or key.endswith(('_api_key', '_token', '_secret', '_password'))
