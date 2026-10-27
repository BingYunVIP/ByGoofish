"""Account-owned client identity and compare-and-swap authentication cookies."""
import time
import hashlib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class VerifiedToken:
    cookie_id: str
    device_id: str = field(repr=False)
    token: str = field(repr=False)
    issued_at: float
    recovery_attempt: str = ''
    cookie_digest: str = field(default='', repr=False)


def seed_verified_token(db, instance, verified):
    """Private in-memory handoff; never re-request an already verified token."""
    from app.services import auth_recovery
    with db.lock:
        state = auth_recovery.get(db, instance.cookie_id)
        if (not isinstance(verified, VerifiedToken) or verified.cookie_id != instance.cookie_id
                or verified.device_id != instance.device_id
                or not verified.token or not 0 <= time.time() - verified.issued_at <= 60
                or state['recovery_state'] != 'checking'
                or state['attempt_id'] != verified.recovery_attempt
                or hashlib.sha256(instance.cookies_str.encode()).hexdigest() != verified.cookie_digest
                or db.get_cookie(instance.cookie_id) != instance.cookies_str):
            return False
        if not auth_recovery.claim_token_probe(db, instance.cookie_id):
            return False
        instance.current_token = verified.token
        instance.last_token_refresh_time = verified.issued_at
        instance.last_token_refresh_status = 'verified_handoff'
        return True


def initialize(db):
    with db.lock:
        db.conn.execute('''CREATE TABLE IF NOT EXISTS account_auth_clients (
            cookie_id TEXT PRIMARY KEY REFERENCES cookies(id) ON DELETE CASCADE,
            device_id TEXT NOT NULL, created_at REAL NOT NULL
        )''')
        db.conn.commit()


def device_id(db, cookie_id, platform_user_id):
    from utils.xianyu_utils import generate_device_id
    with db.lock:
        row = db.conn.execute('SELECT device_id FROM account_auth_clients WHERE cookie_id=?',
                              (cookie_id,)).fetchone()
        if row:
            return row[0]
        value = generate_device_id(platform_user_id)
        # Legacy standalone clients without a saved account remain supported.
        if not db.conn.execute('SELECT 1 FROM cookies WHERE id=?', (cookie_id,)).fetchone():
            return value
        db.conn.execute('INSERT OR IGNORE INTO account_auth_clients VALUES(?,?,?)',
                        (cookie_id, value, time.time()))
        db.conn.commit()
        return db.conn.execute('SELECT device_id FROM account_auth_clients WHERE cookie_id=?',
                               (cookie_id,)).fetchone()[0]


def save_cookie_delta(db, cookie_id, expected, updates):
    """Never overwrite credentials replaced while a network request was awaiting."""
    from utils.xianyu_utils import trans_cookies
    with db.lock:
        latest = db.get_cookie(cookie_id)
        if latest is None or latest != expected:
            return None
        merged = trans_cookies(expected)
        if updates.get('unb') and merged.get('unb') != updates['unb']:
            return None
        merged.update(updates)
        value = '; '.join(f'{key}={val}' for key, val in merged.items())
        if not db.update_cookie_account_info(cookie_id, cookie_value=value):
            return None
        from app.cookie_manager import manager
        if manager is not None and cookie_id in manager.cookies:
            manager.cookies[cookie_id] = value
        return value
