"""Persistent authentication state with explicit login/message recovery stages."""

import secrets
import time

from app.notification_events import get_event

PAUSED = frozenset(("required", "verifying", "checking"))
MESSAGES = {"required":"待处理", "verifying":"处理中", "checking":"检查连接中", "recovered":"已恢复", "none":""}

FAILURES = {
    "login_verification_required": "闲鱼要求本人完成登录安全验证；请使用同一账号完成官方登录或重新扫码",
    "message_verification_required": "闲鱼消息服务要求本人完成安全验证；请在官网打开消息完成验证，再同步网页登录态并检查连接",
    "challenge_page_error": "消息验证页面返回错误；请在官网消息页完成验证，再同步网页登录态或检查连接",
    "official_verification_required": "消息会话仍要求安全验证；请同步官网已通过验证的网页登录态",
    "challenge_not_ready": "验证控件未能正常加载；请查看诊断后再处理",
    "cancelled": "本轮操作已取消；原认证问题仍需处理",
    "verification_timeout": "本轮验证等待超时；请查看诊断，不要连续重试",
    "verification_failed": "本轮验证未完成；请查看诊断",
    "queue_timeout": "本机验证浏览器资源暂不可用；请稍后查看诊断",
    "browser_error": "本机验证浏览器发生错误；请查看诊断",
    "challenge_unavailable": "平台没有返回可用的验证页面；请查看诊断并按消息安全验证步骤处理",
    "signing_token_missing": "签名凭证缺失，尚未证明登录会话失效；查看诊断后更新凭证或重新扫码",
    "signing_token_expired": "签名令牌更新未完成，不能据此判断登录会话过期；请按连接检查结果处理",
    "challenge_request_failed": "验证地址请求失败；请查看诊断",
    "challenge_invalid_response": "平台验证响应无法识别；请查看诊断",
    "token_probe_failed": "登录令牌检查未通过；请查看诊断并按具体原因更新凭证",
    "connection_failed": "消息连接检查未通过；请查看诊断并重新检查连接",
    "registration_rejected": "消息服务拒绝了当前凭证；请查看诊断、更新已验证会话后检查连接",
    "interrupted": "上一次认证检查被中断；请重新检查当前凭证",
    "login_session_expired": "登录会话已过期；请使用同一闲鱼账号重新扫码登录",
    "account_disabled": "凭证已保存，但账号当前未启用",
}

FAILURE_EVENTS = {
    "login_verification_required":"login_verification_required",
    "login_session_expired":"login_session_expired",
    "message_verification_required":"message_verification_required",
    "challenge_page_error":"message_verification_required",
    "official_verification_required":"message_verification_required",
    "signing_token_missing":"credentials_invalid",
    "signing_token_expired":"credentials_invalid",
    "token_probe_failed":"credentials_invalid",
    "connection_failed":"credentials_invalid",
    "registration_rejected":"credentials_invalid",
    "challenge_not_ready":"transient_warning",
    "challenge_unavailable":"message_verification_required",
    "challenge_request_failed":"transient_warning",
    "challenge_invalid_response":"transient_warning",
    "verification_timeout":"transient_warning",
    "verification_failed":"transient_warning",
    "queue_timeout":"transient_warning",
    "browser_error":"transient_warning",
    "interrupted":"transient_warning",
    "cancelled":"transient_warning",
    "account_disabled":"transient_warning",
}
CHECKABLE_FAILURES = frozenset((
    'connection_failed', 'registration_rejected', 'signing_token_missing', 'signing_token_expired',
    'token_probe_failed', 'challenge_request_failed', 'challenge_invalid_response', 'challenge_unavailable',
    'challenge_not_ready', 'queue_timeout', 'browser_error', 'verification_timeout', 'interrupted',
))


def _event_for(category):
    event_id = FAILURE_EVENTS.get(category, "credentials_invalid")
    return get_event(event_id) or get_event("credentials_invalid")


def record_failure(db, cookie_id, category, diagnostic_id=None):
    event = _event_for(category)
    if diagnostic_id:
        with db.lock:
            result = db.conn.execute(
                "UPDATE risk_control_logs SET event_type=?,event_description=?,processing_result=?,processing_status=?,error_message=?,severity=?,action_code=?,recommended_action=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE diagnostic_id=? AND cookie_id=?",
                (event["id"], event["label"], FAILURES.get(category, "认证检查未通过"),
                 "cancelled" if category == "cancelled" else "failed", category, event["severity"],
                 event["id"], event["action"], diagnostic_id, cookie_id),
            )
            db.conn.commit()
            if result.rowcount:
                return
    db.add_risk_control_log(
        cookie_id, event_type=event["id"], event_description=event["label"],
        processing_result=FAILURES.get(category, "认证检查未通过"),
        processing_status="cancelled" if category == "cancelled" else "failed",
        error_message=category, severity=event["severity"], action_code=event["id"],
        recommended_action=event["action"],
    )


def initialize(db):
    with db.lock:
        db.conn.execute("""CREATE TABLE IF NOT EXISTS account_auth_recovery (
            cookie_id TEXT PRIMARY KEY REFERENCES cookies(id) ON DELETE CASCADE,
            state TEXT NOT NULL, failure_category TEXT NOT NULL DEFAULT '',
            attempt_id TEXT NOT NULL DEFAULT '', token_claimed INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL, last_check_at REAL NOT NULL DEFAULT 0,
            incident_id TEXT NOT NULL DEFAULT '', auth_stage TEXT NOT NULL DEFAULT 'login'
        )""")
        columns = {row[1] for row in db.conn.execute('PRAGMA table_info(account_auth_recovery)')}
        for name, declaration in (
            ('last_check_at', 'REAL NOT NULL DEFAULT 0'),
            ('incident_id', "TEXT NOT NULL DEFAULT ''"),
            ('auth_stage', "TEXT NOT NULL DEFAULT 'login'"),
        ):
            if name not in columns:
                db.conn.execute(f'ALTER TABLE account_auth_recovery ADD COLUMN {name} {declaration}')
        db.conn.execute(
            "UPDATE account_auth_recovery SET state='required', failure_category=CASE WHEN failure_category='' THEN 'interrupted' ELSE failure_category END, "
            "attempt_id='', token_claimed=0, updated_at=? WHERE state IN ('verifying','checking')",
            (time.time(),),
        )
        db.conn.commit()


def get(db, cookie_id):
    with db.lock:
        row = db.conn.execute(
            "SELECT state,failure_category,attempt_id,token_claimed,updated_at,last_check_at,incident_id,auth_stage "
            "FROM account_auth_recovery WHERE cookie_id=?", (cookie_id,),
        ).fetchone()
    state, category, attempt, claimed, updated, last_check, incident_id, auth_stage = row or ("none", "", "", 0, None, 0, "", "login")
    event = _event_for(category) if category else None
    event_id = event["id"] if event else ""
    scope = "login" if event_id in ("login_verification_required", "login_session_expired") else (
        "message" if event_id == "message_verification_required" else "none")
    # Only verified platform challenges are described as requiring a human.
    requires_human = state in PAUSED and event_id in ("login_verification_required", "message_verification_required")
    action_codes = event.get("actions", []) if event else []
    if category in ("connection_failed", "registration_rejected"):
        action_codes = ["view_diagnostic", "sync_web_session", "check_connection"]
    elif category in ("signing_token_missing", "signing_token_expired"):
        action_codes = ["view_diagnostic", "edit_credentials", "qr_login", "check_connection"]
    elif category in ("challenge_page_error", "official_verification_required"):
        action_codes = ["view_diagnostic", "sync_web_session", "check_connection"]
    elif category in CHECKABLE_FAILURES:
        action_codes = ["view_diagnostic", "check_connection"]
    can_start = False
    action = ("qr_login" if scope == "login" else "sync_web_session" if scope == "message"
              else "check_connection" if "check_connection" in action_codes else action_codes[0] if action_codes
              else "view_diagnostic" if state in PAUSED else "none")
    can_check = state == "required" and (scope == "message" or category in CHECKABLE_FAILURES) and time.time() - (last_check or 0) >= 60
    return {
        "recovery_state": state, "failure_category": category, "event_type": event_id,
        "severity": event["severity"] if event else "info", "event_label": event["label"] if event else "",
        "event_impact": event["impact"] if event else "", "recommended_action": event["action"] if event else "",
        "verification_scope": scope, "attempt_id": attempt, "token_claimed": bool(claimed),
        "updated_at": updated, "auth_paused": state in PAUSED,
        "incident_id": incident_id, "auth_stage": auth_stage,
        "requires_manual_verification": requires_human, "requires_action": state == 'required',
        "can_start_verification": can_start, "can_check_connection": can_check,
        "recovery_action": action, "last_check_at": last_check,
        "action_codes": action_codes,
        "recovery_message": (f"{event['label']}待处理" if state == "required" and event else MESSAGES.get(state, "")),
        "recovery_detail": FAILURES.get(category, "") if category else "",
    }


def set_state(db, cookie_id, state, category="", expected_attempt=None, *, stage=None):
    if state not in MESSAGES:
        raise ValueError("无效认证恢复状态")
    with db.lock:
        old = get(db, cookie_id)
        if expected_attempt is not None and old["attempt_id"] != expected_attempt:
            return None
        # Closing a modal cancels the attempt, not the underlying platform incident.
        if category == "cancelled" and old["failure_category"]:
            category = old["failure_category"]
        if state in ("verifying", "checking") and not category:
            category = old["failure_category"]
        attempt = secrets.token_hex(16) if state in ("verifying", "checking") else ""
        old_event_id = old.get('event_type') or ''
        new_event_id = _event_for(category)['id'] if category else ''
        starts_incident = state in ('required', 'checking') and bool(new_event_id) and (
            not old['auth_paused'] or old_event_id != new_event_id or not old.get('incident_id')
        )
        incident_id = secrets.token_hex(16) if starts_incident else old.get('incident_id', '')
        auth_stage = stage or ('login' if new_event_id in ('login_verification_required','login_session_expired') else 'message' if new_event_id == 'message_verification_required' else old.get('auth_stage','login'))
        db.conn.execute("""INSERT INTO account_auth_recovery
            (cookie_id,state,failure_category,attempt_id,token_claimed,updated_at,last_check_at,incident_id,auth_stage)
            VALUES(?,?,?,?,0,?,0,?,?) ON CONFLICT(cookie_id) DO UPDATE SET
            state=excluded.state,failure_category=excluded.failure_category,
            attempt_id=excluded.attempt_id,token_claimed=0,updated_at=excluded.updated_at,
            incident_id=excluded.incident_id,auth_stage=excluded.auth_stage""",
            (cookie_id, state, category, attempt, time.time(), incident_id, auth_stage))
        db.conn.commit()
        from app.services import event_journal
        if starts_incident:
            event_journal.observe(db,cookie_id,new_event_id,FAILURES.get(category,'认证检查未通过'))
            with db.lock:
                db.conn.execute("UPDATE risk_control_logs SET error_message=? WHERE id IN (SELECT log_id FROM operational_incidents WHERE cookie_id=? AND event_type=? AND resolved_at IS NULL)",
                                (category,cookie_id,new_event_id))
                db.conn.commit()
        if state == 'recovered':
            event_journal.resolve(db,cookie_id,('login_verification_required','login_session_expired','message_verification_required','credentials_invalid','transient_warning','account_offline'))
        elif starts_incident and old_event_id and old_event_id != new_event_id:
            event_journal.resolve(db,cookie_id,(old_event_id,),superseded=True)
        if old["recovery_state"] == "checking" and state in ("required", "recovered"):
            from app.services.captcha_diagnostics import recovery_result
            recovery_result(db, cookie_id, old["attempt_id"], state, category)
        return get(db, cookie_id)


def begin_verification(db, cookie_id, check_only=False):
    with db.lock:
        current = get(db, cookie_id)
        fresh_check = check_only and current['recovery_state'] in ('none', 'recovered')
        permitted = (current["can_check_connection"] or fresh_check) if check_only else current["can_start_verification"]
        if not permitted:
            return None
        if check_only:
            db.conn.execute("UPDATE account_auth_recovery SET last_check_at=? WHERE cookie_id=?", (time.time(), cookie_id))
            db.conn.commit()
            if fresh_check:
                return set_state(db, cookie_id, 'checking', 'connection_failed')
        return set_state(db, cookie_id, "verifying")


def save_explicit_credentials(db, cookie_id, writer):
    with db.lock:
        paused = get(db, cookie_id)["auth_paused"]
        saved = writer()
        if saved and paused:
            set_state(db, cookie_id, "checking")
        return saved


def claim_token_probe(db, cookie_id):
    with db.lock:
        result = db.conn.execute("UPDATE account_auth_recovery SET token_claimed=1 "
                                 "WHERE cookie_id=? AND state='checking' AND token_claimed=0", (cookie_id,))
        db.conn.commit()
        return result.rowcount == 1


def default_db():
    from app.db_manager import db_manager
    return db_manager


def status(cookie_id):
    return get(default_db(), cookie_id)


def require_manual(cookie_id, category):
    return set_state(default_db(), cookie_id, "required", category)


def auth_paused(cookie_id):
    return status(cookie_id)["auth_paused"]
