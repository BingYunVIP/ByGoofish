"""持久化、可撤销的管理会话和账号范围的服务密钥。"""
import hashlib
import secrets
import time
from typing import Optional

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.db_manager import db_manager

security = HTTPBearer(auto_error=False)
TOKEN_EXPIRE_TIME = 24 * 60 * 60


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class SessionStore:
    """磁盘仅保存 token 摘要；请求时同时验证当前用户及密码指纹。"""
    def __init__(self):
        with db_manager.lock:
            db_manager.conn.executescript('''
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    password_fingerprint TEXT NOT NULL, created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS auth_sessions_user ON auth_sessions(user_id);
                CREATE INDEX IF NOT EXISTS auth_sessions_expiry ON auth_sessions(expires_at);
                CREATE TABLE IF NOT EXISTS account_api_keys (
                    cookie_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    key_hash TEXT NOT NULL, created_at REAL NOT NULL
                );
            ''')
            db_manager.conn.execute('DELETE FROM auth_sessions WHERE expires_at <= ?', (time.time(),))
            db_manager.conn.commit()

    def __setitem__(self, token, data):
        with db_manager.lock:
            user = db_manager.get_user_by_id(data['user_id'])
            # get_user_by_id 不返回哈希，使用私有查询，不通过日志输出。
            row = db_manager.conn.execute('SELECT password_hash FROM users WHERE id=? AND is_active=1', (data['user_id'],)).fetchone()
            if not row or not user:
                raise ValueError('用户不可用')
            now = time.time()
            db_manager.conn.execute('DELETE FROM auth_sessions WHERE expires_at <= ?', (now,))
            db_manager.conn.execute('INSERT INTO auth_sessions VALUES (?,?,?,?,?)',
                                    (digest(token), data['user_id'], digest(row[0]), now, now + TOKEN_EXPIRE_TIME))
            db_manager.conn.execute('''DELETE FROM auth_sessions WHERE token_hash IN (
                SELECT token_hash FROM auth_sessions WHERE user_id=? ORDER BY created_at DESC LIMIT -1 OFFSET 20
            )''', (data['user_id'],))
            db_manager.conn.commit()

    def get(self, token, default=None):
        if not isinstance(token, str) or not token:
            return default
        with db_manager.lock:
            row = db_manager.conn.execute('''
                SELECT s.user_id,u.username,u.password_hash,u.is_active,
                       COALESCE(u.must_change_password,0),
                       s.password_fingerprint,s.created_at,s.expires_at
                FROM auth_sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?
            ''', (digest(token),)).fetchone()
            if not row or not row[3] or row[7] <= time.time() or not secrets.compare_digest(digest(row[2]), row[5]):
                return default
            return {'user_id':row[0], 'username':row[1], 'is_admin':row[1]=='admin',
                    'must_change_password': bool(row[4]), 'timestamp':row[6]}

    def __getitem__(self, token):
        value = self.get(token)
        if value is None: raise KeyError(token)
        return value

    def __contains__(self, token):
        return self.get(token) is not None

    def pop(self, token, default=None):
        value = self.get(token, default)
        with db_manager.lock:
            db_manager.conn.execute('DELETE FROM auth_sessions WHERE token_hash=?', (digest(token),))
            db_manager.conn.commit()
        return value

    def __delitem__(self, token):
        self.pop(token)

    def revoke_user(self, user_id):
        with db_manager.lock:
            cursor = db_manager.conn.execute('DELETE FROM auth_sessions WHERE user_id=?', (user_id,))
            db_manager.conn.commit()
            return cursor.rowcount

    def clear(self):
        with db_manager.lock:
            db_manager.conn.execute('DELETE FROM auth_sessions')
            db_manager.conn.commit()


SESSION_TOKENS = SessionStore()


def verify_token(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    return SESSION_TOKENS.get(credentials.credentials) if credentials else None


def require_auth(user_info=Depends(verify_token)):
    if not user_info: raise HTTPException(401, '未授权访问')
    return user_info


def get_current_user(user_info=Depends(require_auth)):
    return user_info


def get_current_user_optional(user_info=Depends(verify_token)):
    return user_info


def require_admin(current_user=Depends(get_current_user)):
    if current_user['username'] != 'admin': raise HTTPException(403, '需要管理员权限')
    return current_user


verify_admin_token = require_admin


def require_owned_account(cookie_id, user):
    details = db_manager.get_cookie_details(cookie_id)
    if not details or details.get('user_id') != user['user_id']:
        raise HTTPException(404, '账号不存在或无权限')
    return details


def rotate_account_key(cookie_id, user):
    require_owned_account(cookie_id, user)
    key = secrets.token_urlsafe(32)
    with db_manager.lock:
        db_manager.conn.execute('''INSERT INTO account_api_keys VALUES(?,?,?,?)
            ON CONFLICT(cookie_id) DO UPDATE SET user_id=excluded.user_id,
            key_hash=excluded.key_hash,created_at=excluded.created_at''',
            (cookie_id, user['user_id'], digest(key), time.time()))
        db_manager.conn.commit()
    return key


def verify_account_key(key, cookie_id):
    if not key or len(key) < 32: return False
    with db_manager.lock:
        row = db_manager.conn.execute('''SELECT k.key_hash FROM account_api_keys k
            JOIN cookies c ON c.id=k.cookie_id AND c.user_id=k.user_id
            JOIN users u ON u.id=k.user_id AND u.is_active=1 WHERE k.cookie_id=?''', (cookie_id,)).fetchone()
    return bool(row and secrets.compare_digest(digest(key), row[0]))
