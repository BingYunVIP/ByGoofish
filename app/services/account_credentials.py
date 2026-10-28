"""Atomic management-user credential changes. No platform-account credentials are touched."""
import secrets
import time

from loguru import logger
from app.auth_validators import validate_email, validate_password
from app.password_hasher import hash_password, verify_password

MESSAGES = {
    'user_unavailable': '登录用户不可用，请重新登录',
    'current_password_wrong': '当前密码错误',
    'password_unchanged': '新密码不能与当前密码相同',
    'email_unchanged': '新邮箱不能与当前邮箱相同',
    'email_in_use': '该邮箱已被其他用户使用',
    'code_expired': '邮箱验证码已过期，请重新发送',
    'code_unavailable': '邮箱验证码不存在或已使用，请重新发送',
    'code_wrong': '邮箱验证码错误，请检查后重试',
    'code_attempts_exceeded': '验证码错误次数已达上限，请重新发送',
    'write_failed': '修改未完成，原登录信息保持有效，请稍后重试',
}


def result_error(category, message=None, *, request_id=None, **extra):
    return {'success': False, 'error_code': category,
            'message': message or MESSAGES.get(category, '操作未完成，请稍后重试'),
            'request_id': request_id or secrets.token_hex(6), **extra}


def change(db, user_id, current_password, *, new_password=None, new_email=None, code=None):
    request_id = secrets.token_hex(6)
    is_email = new_email is not None
    new_email = (new_email or '').strip().lower() if is_email else None
    valid, reason = validate_email(new_email) if is_email else validate_password(new_password)
    if not valid:
        return result_error('email_invalid' if is_email else 'password_invalid', reason, request_id=request_id)
    with db.lock:
        try:
            db.conn.execute('BEGIN IMMEDIATE')
            cursor = db.conn.cursor()
            user = cursor.execute('SELECT id,username,email,password_hash,is_active FROM users WHERE id=?',
                                  (user_id,)).fetchone()

            def reject(category, message=None):
                db.conn.rollback()
                return result_error(category, message, request_id=request_id)

            if not user or not user[4]:
                return reject('user_unavailable')
            if not verify_password(current_password, user[3]):
                return reject('current_password_wrong')
            if is_email:
                if new_email == (user[2] or '').strip().lower():
                    return reject('email_unchanged')
                if cursor.execute('SELECT 1 FROM users WHERE email=? COLLATE NOCASE AND id!=?',
                                  (new_email, user_id)).fetchone():
                    return reject('email_in_use')
                verification = cursor.execute('''SELECT id,code,expires_at,used,failed_attempts
                    FROM email_verifications WHERE email=? AND type=? ORDER BY id DESC LIMIT 1''',
                    (new_email, f'email_change:{user_id}')).fetchone()
                if not verification:
                    return reject('code_unavailable')
                code_id, expected, expiry, used, attempts = verification
                if (attempts or 0) >= 5:
                    return reject('code_attempts_exceeded')
                if used:
                    return reject('code_unavailable')
                if expiry <= time.time():
                    return reject('code_expired')
                if not secrets.compare_digest(str(expected).encode(), str(code or '').strip().encode()):
                    attempts = (attempts or 0) + 1
                    cursor.execute('UPDATE email_verifications SET failed_attempts=?,used=? WHERE id=?',
                                   (attempts, attempts >= 5, code_id))
                    db.conn.commit()
                    return result_error('code_attempts_exceeded' if attempts >= 5 else 'code_wrong', request_id=request_id)
                cursor.execute('UPDATE users SET email=?,updated_at=CURRENT_TIMESTAMP WHERE id=?', (new_email,user_id))
                cursor.execute('UPDATE email_verifications SET used=1 WHERE id=?', (code_id,))
            else:
                if new_password == current_password:
                    return reject('password_unchanged')
                cursor.execute('UPDATE users SET password_hash=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                               (hash_password(new_password),user_id))
            # The credential mutation and revocation succeed or roll back together.
            cursor.execute('DELETE FROM auth_sessions WHERE user_id=?', (user_id,))
            db.conn.commit()
            profile = {'user_id': user_id, 'username': user[1], 'email': new_email if is_email else user[2]}
            logger.info('管理用户凭证修改完成 kind={} request_id={}', 'email' if is_email else 'password', request_id)
            return {'success': True, 'message': '登录邮箱已修改，请使用新邮箱重新登录' if is_email else '密码已修改，请使用新密码重新登录',
                    'reauth_required': True, 'profile': profile, 'request_id': request_id}
        except Exception as exc:
            db.conn.rollback()
            logger.warning('管理用户凭证修改回滚 kind={} error={} request_id={}',
                           'email' if is_email else 'password', type(exc).__name__, request_id)
            return result_error('write_failed', request_id=request_id)


def invalidate_sent_code(db, email, user_id, code):
    """Invalidate only this failed submission, not a concurrent replacement code."""
    with db.lock:
        db.conn.execute('UPDATE email_verifications SET used=1 WHERE email=? AND type=? AND code=?',
                        (email, f'email_change:{user_id}', code))
        db.conn.commit()
