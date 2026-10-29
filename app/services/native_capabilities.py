"""Verified native features and bounded, redacted action diagnostics."""
import time
from fastapi import HTTPException

KINDS=('reply_text','forward_text','forward_image')


def initialize(db):
    with db.lock:
        db.conn.execute('''CREATE TABLE IF NOT EXISTS chat_native_capabilities (
            kind TEXT PRIMARY KEY,status TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',updated_at REAL NOT NULL)''')
        db.conn.execute('''CREATE TABLE IF NOT EXISTS chat_action_diagnostics (
            cookie_id TEXT NOT NULL REFERENCES cookies(id) ON DELETE CASCADE,request_id TEXT NOT NULL,
            code TEXT NOT NULL,reason TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(cookie_id,request_id))''')
        for kind in KINDS:db.conn.execute('INSERT OR IGNORE INTO chat_native_capabilities VALUES(?,?,?,?)',(kind,'unverified','原生能力尚未完成实际验收',time.time()))
        db.conn.commit()


def capabilities(db):
    with db.lock:
        rows=db.conn.execute('SELECT kind,status,reason FROM chat_native_capabilities').fetchall()
    return {kind:{'supported':status=='verified','reason':reason} for kind,status,reason in rows}


def require(db,uid,recipient,kind):
    if capabilities(db).get(kind,{}).get('supported'):return
    # Unverified probes are confined to the operator's own second account.
    # Normal customer conversations cannot be used to discover unsupported protocols.
    if recipient in db.get_all_cookies(uid):return
    raise HTTPException(409,'此原生操作尚未通过实际验收，暂不可用',headers={'X-Chat-Send-Status':'not_sent'})


def record(db,account,request_id,response):
    if db is None or not request_id:return
    from utils.log_sanitizer import redact_sensitive_text
    body=response.get('body') if isinstance(response,dict) else {}
    body=body if isinstance(body,dict) else {}
    code=str(response.get('code') or '') if isinstance(response,dict) else 'invalid_response'
    reason=str(body.get('reason') or body.get('developerMessage') or body.get('code') or '')
    if len(reason)>512:reason='平台返回了过长的错误信息，已省略'
    reason=redact_sensitive_text(reason)[:200]
    with db.lock:
        db.conn.execute('INSERT OR REPLACE INTO chat_action_diagnostics VALUES(?,?,?,?,?)',(account,request_id,code,reason,time.time()))
        db.conn.commit()
