"""Verified native features and bounded, redacted action diagnostics."""
import time
from fastapi import HTTPException

KINDS=('reply_text','forward_text','forward_image')
# These exact protocol implementations passed platform receipts and receiver-history
# checks on 2026-10-29; native text replies were also confirmed in the official App.
# A future protocol change must use a new revision and undergo acceptance again.
RELEASE_PROTOCOLS = {
    'reply_text': 'reply-message-id-v2',
    'forward_text': 'message-forward-text-v1',
    'forward_image': 'message-forward-image-v1',
}


def initialize(db):
    with db.lock:
        db.conn.execute('''CREATE TABLE IF NOT EXISTS chat_native_capabilities (
            kind TEXT PRIMARY KEY,status TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',updated_at REAL NOT NULL)''')
        if 'protocol_revision' not in {r[1] for r in db.conn.execute('PRAGMA table_info(chat_native_capabilities)')}:
            db.conn.execute("ALTER TABLE chat_native_capabilities ADD COLUMN protocol_revision TEXT NOT NULL DEFAULT ''")
        db.conn.execute('''CREATE TABLE IF NOT EXISTS chat_action_diagnostics (
            cookie_id TEXT NOT NULL REFERENCES cookies(id) ON DELETE CASCADE,request_id TEXT NOT NULL,
            code TEXT NOT NULL,reason TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(cookie_id,request_id))''')
        for kind, revision in RELEASE_PROTOCOLS.items():
            db.conn.execute('INSERT OR IGNORE INTO chat_native_capabilities(kind,status,reason,updated_at,protocol_revision) VALUES(?,?,?,?,?)',
                            (kind,'inherit','',time.time(),revision))
            # Only the old initialization marker is reset, never a real failure or disable.
            db.conn.execute("UPDATE chat_native_capabilities SET status='inherit',reason='',protocol_revision=? WHERE kind=? AND status='unverified' AND protocol_revision='' AND reason='原生能力尚未完成实际验收'",
                            (revision,kind))
            db.conn.execute("UPDATE chat_native_capabilities SET protocol_revision=? WHERE kind=? AND status='verified' AND protocol_revision=''",(revision,kind))
        db.conn.commit()


def capabilities(db):
    with db.lock:
        rows={r[0]:r[1:] for r in db.conn.execute('SELECT kind,status,reason,protocol_revision FROM chat_native_capabilities')}
    result={}
    for kind, revision in RELEASE_PROTOCOLS.items():
        status,reason,stored_revision=rows.get(kind,('inherit','',revision))
        supported=status=='inherit' or (status=='verified' and stored_revision==revision)
        result[kind]={'supported':supported,'reason':'' if supported else reason or '此原生能力已禁用或当前协议尚未验收',
                      'protocol_revision':revision,'source':'release' if status=='inherit' else 'override'}
    return result


def require(db,uid,recipient,kind):
    if kind not in RELEASE_PROTOCOLS:
        raise HTTPException(400,'不支持此原生消息类型',headers={'X-Chat-Send-Status':'not_sent'})
    if capabilities(db)[kind]['supported']:return
    with db.lock:
        row=db.conn.execute('SELECT status FROM chat_native_capabilities WHERE kind=?',(kind,)).fetchone()
    if row and row[0] in ('failed','disabled'):
        raise HTTPException(409,'此原生能力已禁用，请查看失败原因',headers={'X-Chat-Send-Status':'not_sent'})
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
