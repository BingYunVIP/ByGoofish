"""Operational incidents and per-rule delivery claims, with no credential/body copies."""
import hashlib
import secrets
import time

from app.notification_events import get_event


def initialize(db):
    with db.lock:
        db.conn.executescript('''
            CREATE TABLE IF NOT EXISTS operational_incidents (
                id TEXT PRIMARY KEY, cookie_id TEXT NOT NULL REFERENCES cookies(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL, context_hash TEXT NOT NULL, log_id INTEGER,
                opened_at REAL NOT NULL, last_seen_at REAL NOT NULL,
                occurrences INTEGER NOT NULL DEFAULT 1, resolved_at REAL
            );
            CREATE INDEX IF NOT EXISTS operational_incidents_active
                ON operational_incidents(cookie_id,event_type,context_hash,resolved_at);
            CREATE TABLE IF NOT EXISTS operational_notices (
                incident_id TEXT NOT NULL REFERENCES operational_incidents(id) ON DELETE CASCADE,
                rule_id INTEGER NOT NULL, status TEXT NOT NULL, request_id TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL, PRIMARY KEY(incident_id,rule_id)
            );
        ''')
        db.conn.execute("UPDATE operational_notices SET status='unknown' WHERE status='sending'")
        db.conn.commit()


def observe(db, account, event_type, detail='', *, context='account', diagnostic_id=None):
    definition = get_event(event_type)
    if not definition:
        raise ValueError('未定义的业务事件')
    from app.services.captcha_diagnostics import sanitize
    safe_detail = sanitize(detail)
    digest = hashlib.sha256(str(context).encode()).hexdigest()
    now = time.time()
    with db.lock:
        row = db.conn.execute('SELECT id,log_id FROM operational_incidents WHERE cookie_id=? AND event_type=? '
                              'AND context_hash=? AND resolved_at IS NULL', (account,event_type,digest)).fetchone()
        if row:
            db.conn.execute('UPDATE operational_incidents SET last_seen_at=?,occurrences=occurrences+1 WHERE id=?', (now,row[0]))
            if diagnostic_id:
                db.conn.execute('UPDATE risk_control_logs SET diagnostic_id=? WHERE id=?', (diagnostic_id,row[1]))
            db.conn.commit()
            return row[0]
        incident = secrets.token_hex(16)
        saved = db.add_risk_control_log(account,event_type=event_type,event_description=definition['label'],
            processing_status='success' if definition['severity']=='info' else 'processing',
            processing_result=safe_detail, severity=definition['severity'], action_code=event_type,
            recommended_action=definition['action'],diagnostic_id=diagnostic_id)
        if not saved:
            raise RuntimeError('业务事件记录未保存')
        log_id = db.conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        db.conn.execute('INSERT INTO operational_incidents(id,cookie_id,event_type,context_hash,log_id,opened_at,last_seen_at) '
                        'VALUES(?,?,?,?,?,?,?)',(incident,account,event_type,digest,log_id,now,now))
        db.conn.commit()
        return incident


def resolve(db, account, event_types, *, context=None, superseded=False):
    if not event_types:
        return
    with db.lock:
        params = [account,*event_types]
        clause = 'cookie_id=? AND event_type IN ('+','.join('?' for _ in event_types)+') AND resolved_at IS NULL'
        if context is not None:
            clause += ' AND context_hash=?'
            params.append(hashlib.sha256(str(context).encode()).hexdigest())
        rows = db.conn.execute('SELECT id,log_id FROM operational_incidents WHERE '+clause,params).fetchall()
        for incident, log_id in rows:
            db.conn.execute('UPDATE operational_incidents SET resolved_at=? WHERE id=?',(time.time(),incident))
            db.conn.execute("UPDATE risk_control_logs SET processing_status=?,processing_result=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            ('cancelled' if superseded else 'success',
                             '故障分类已更新，请查看当前账号事件' if superseded else '问题已解决',log_id))
        db.conn.commit()


async def dispatch(db, live, event_type, detail, *, context='account', attachment_path=None, verification_url='', values=None):
    definition = get_event(event_type)
    if not definition:
        raise ValueError('未定义的业务事件')
    account = live.cookie_id
    incident = observe(db,account,event_type,detail,context=context)
    rules = db.get_account_notifications(account,user_id=db.get_cookie_owner_user(account),event_type=event_type)
    from app.services.notification_templates import render_account_notification
    from app.services.notification_delivery import deliver
    message = render_account_notification(db,account,event_type,detail,
        verification_url=verification_url,**(values or {}))
    sent = 0
    for rule in rules:
        if not rule.get('enabled',True):
            continue
        with db.lock:
            claimed = db.conn.execute("INSERT OR IGNORE INTO operational_notices(incident_id,rule_id,status,updated_at) VALUES(?,?,'sending',?)",
                                      (incident,rule['id'],time.time())).rowcount
            db.conn.commit()
        if not claimed:
            continue
        receipts=[]
        try:
            sent += await deliver(live,[rule],event_type,message,attachment_path,receipt_sink=receipts)
        finally:
            receipt=receipts[0] if receipts else {}
            with db.lock:
                db.conn.execute('UPDATE operational_notices SET status=?,request_id=?,updated_at=? WHERE incident_id=? AND rule_id=?',
                    (receipt.get('status','unknown'),receipt.get('request_id',''),time.time(),incident,rule['id']))
                db.conn.commit()
    return sent
