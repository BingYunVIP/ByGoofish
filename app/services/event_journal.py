"""Durable event lifecycle and bounded, per-rule reminders. No credential/body copies."""
import hashlib
import json
import secrets
import time

from app.notification_events import get_event

ACCOUNT_EVENTS = frozenset(('login_verification_required', 'login_session_expired',
    'message_verification_required', 'credentials_invalid', 'login_failed',
    'instance_restart_failed', 'transient_warning'))
ORDER_FAILURES = ('delivery_failed', 'delivery_timeout', 'delivery_confirm_failed',
                  'buyer_interaction_failed', 'buyer_info_incomplete')
DEFAULT_LIMIT = 2
DEFAULT_INTERVAL = 5


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
            CREATE TABLE IF NOT EXISTS operational_notice_attempts (
                incident_id TEXT NOT NULL REFERENCES operational_incidents(id) ON DELETE CASCADE,
                rule_id INTEGER NOT NULL, ordinal INTEGER NOT NULL, status TEXT NOT NULL,
                request_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(incident_id,rule_id,ordinal)
            );
        ''')
        for table, additions in (
            ('operational_incidents', (('acknowledged_at', 'REAL'), ('reminder_detail', "TEXT NOT NULL DEFAULT ''"),
                ('reminder_values', "TEXT NOT NULL DEFAULT '{}'"), ('chat_id', "TEXT NOT NULL DEFAULT ''"),
                ('order_id', "TEXT NOT NULL DEFAULT ''"), ('reminders_enabled','INTEGER NOT NULL DEFAULT 0'))),
            ('operational_notices', (('attempts', 'INTEGER NOT NULL DEFAULT 1'),)),
            ('message_notifications', (('reminder_limit', 'INTEGER NOT NULL DEFAULT 2'),
                                       ('reminder_interval_minutes', 'INTEGER NOT NULL DEFAULT 5'))),
        ):
            columns = {r[1] for r in db.conn.execute('PRAGMA table_info('+table+')')}
            for name, declaration in additions:
                if name not in columns:
                    db.conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
        db.conn.execute("UPDATE operational_notices SET status='unknown' WHERE status='sending'")
        db.conn.execute("UPDATE operational_notice_attempts SET status='unknown' WHERE status='sending'")
        # Keep the old offline audit, importing its delivery budget only once into the common ledger.
        for account,old_id,rule,status,updated,attempts in db.conn.execute(
                'SELECT cookie_id,incident_id,rule_id,status,updated_at,attempts FROM account_offline_notices').fetchall():
            row=db.conn.execute("SELECT id FROM operational_incidents WHERE cookie_id=? AND event_type='account_offline' "
                'AND context_hash=? AND resolved_at IS NULL',(account,hashlib.sha256(old_id.encode()).hexdigest())).fetchone()
            if row:
                db.conn.execute('INSERT OR IGNORE INTO operational_notices(incident_id,rule_id,status,updated_at,attempts) '
                                'VALUES(?,?,?,?,?)',(row[0],rule,status,updated,attempts))
        db.conn.execute('''INSERT OR IGNORE INTO operational_notice_attempts
            SELECT incident_id,rule_id,attempts,status,request_id,updated_at,updated_at FROM operational_notices''')
        db.conn.commit()


def _digest(event_type, context):
    return hashlib.sha256(str('account' if event_type in ACCOUNT_EVENTS else context).encode()).hexdigest()


def observe(db, account, event_type, detail='', *, context='account', diagnostic_id=None):
    definition = get_event(event_type)
    if not definition:
        raise ValueError('未定义的业务事件')
    from app.services.captcha_diagnostics import sanitize
    safe_detail = sanitize(detail)
    digest = _digest(event_type, context)
    now = time.time()
    with db.lock:
        # Account faults never get a new budget just because their error text/context changed.
        if event_type in ACCOUNT_EVENTS:
            row = db.conn.execute('SELECT id,log_id FROM operational_incidents WHERE cookie_id=? AND event_type=? '
                'AND resolved_at IS NULL ORDER BY opened_at LIMIT 1', (account,event_type)).fetchone()
        else:
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
        db.conn.execute('INSERT INTO operational_incidents(id,cookie_id,event_type,context_hash,log_id,opened_at,last_seen_at,reminder_detail) '
                        'VALUES(?,?,?,?,?,?,?,?)',(incident,account,event_type,digest,log_id,now,now,safe_detail))
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


def acknowledge(db, user_id, log_id):
    """Acknowledgement stops reminders, never fabricates platform recovery or resets a live fault."""
    with db.lock:
        row = db.conn.execute('SELECT i.id FROM operational_incidents i JOIN cookies c ON c.id=i.cookie_id '
                              'WHERE i.log_id=? AND c.user_id=?', (log_id,user_id)).fetchone()
        if not row:
            return False
        db.conn.execute('UPDATE operational_incidents SET acknowledged_at=COALESCE(acknowledged_at,?) WHERE id=?',
                        (time.time(),row[0]))
        db.conn.commit()
        return True


def resolve_chat(db, account, cid, before):
    with db.lock:
        db.conn.execute("UPDATE operational_incidents SET resolved_at=? WHERE cookie_id=? AND event_type='buyer_message' "
                        'AND chat_id=? AND opened_at<=? AND resolved_at IS NULL',
                        (time.time(),account,str(cid).split('@')[0],before))
        db.conn.commit()


def resolve_order(db, account, order_id, status):
    if status not in ('shipped','completed','cancelled','closed'):
        return
    with db.lock:
        db.conn.execute('UPDATE operational_incidents SET resolved_at=? WHERE cookie_id=? AND order_id=? '
                        "AND event_type IN ('delivery_failed','delivery_timeout','delivery_confirm_failed') AND resolved_at IS NULL",
                        (time.time(),account,str(order_id)))
        db.conn.commit()


def summaries(db, log_ids):
    if not log_ids:
        return {}
    with db.lock:
        rows = db.conn.execute('SELECT log_id,id,acknowledged_at,resolved_at FROM operational_incidents WHERE log_id IN ('+
                               ','.join('?' for _ in log_ids)+')',list(log_ids)).fetchall()
        return {r[0]: {'id':r[1], 'acknowledged':r[2] is not None,'resolved':r[3] is not None,
                'attempts': db.conn.execute('SELECT COALESCE(sum(attempts),0) FROM operational_notices WHERE incident_id=?',(r[1],)).fetchone()[0]}
                for r in rows}


def _claim(db, incident, rule, now):
    with db.lock:
        # Guard with both lifecycle and CURRENT rule/channel ownership, not a stale task snapshot.
        active = db.conn.execute('''SELECT 1 FROM operational_incidents i JOIN cookies c ON c.id=i.cookie_id
            LEFT JOIN cookie_status cs ON cs.cookie_id=c.id
            JOIN message_notifications n ON n.cookie_id=i.cookie_id JOIN notification_channels ch ON ch.id=n.channel_id
            WHERE i.id=? AND n.id=? AND i.resolved_at IS NULL AND i.acknowledged_at IS NULL
            AND n.enabled=1 AND ch.enabled=1 AND COALESCE(cs.enabled,1)=1 AND ch.user_id=c.user_id''', (incident,rule['id'])).fetchone()
        if not active:
            return 0
        previous = db.conn.execute('SELECT status,attempts,updated_at FROM operational_notices WHERE incident_id=? AND rule_id=?',
                                   (incident,rule['id'])).fetchone()
        limit = rule.get('reminder_limit',DEFAULT_LIMIT)
        interval = rule.get('reminder_interval_minutes',DEFAULT_INTERVAL)*60
        if previous and (previous[0] not in ('submitted','handler_completed','failed','unsupported') or
                         previous[1]>=limit or now<previous[2]+interval):
            return 0
        ordinal = previous[1]+1 if previous else 1
        db.conn.execute('INSERT INTO operational_notices(incident_id,rule_id,status,updated_at,attempts) VALUES(?,?,\'sending\',?,?) '
                        "ON CONFLICT(incident_id,rule_id) DO UPDATE SET status='sending',updated_at=excluded.updated_at,attempts=excluded.attempts",
                        (incident,rule['id'],now,ordinal))
        db.conn.execute("INSERT INTO operational_notice_attempts VALUES(?,?,?,'sending','',?,?)", (incident,rule['id'],ordinal,now,now))
        db.conn.commit()
        return ordinal


async def _deliver_incident(db, live, incident, event_type, message, *, attachment_path=None, now=None):
    from app.services.notification_delivery import deliver
    now = time.time() if now is None else now
    sent = 0
    rules = db.get_account_notifications(live.cookie_id,user_id=db.get_cookie_owner_user(live.cookie_id),event_type=event_type)
    for rule in rules:
        if not rule.get('enabled',True):
            continue
        ordinal = _claim(db,incident,rule,now)
        if not ordinal:
            continue
        receipts=[]
        try:
            sent += await deliver(live,[rule],event_type,message,attachment_path,receipt_sink=receipts)
        finally:
            receipt=receipts[0] if receipts else {}
            args=(receipt.get('status','unknown'),receipt.get('request_id',''),max(now,time.time()),incident,rule['id'])
            with db.lock:
                db.conn.execute('UPDATE operational_notices SET status=?,request_id=?,updated_at=? WHERE incident_id=? AND rule_id=?',args)
                db.conn.execute('UPDATE operational_notice_attempts SET status=?,request_id=?,updated_at=? WHERE incident_id=? AND rule_id=? AND ordinal=?',(*args,ordinal))
                db.conn.commit()
    return sent


async def dispatch(db, live, event_type, detail, *, context='account', attachment_path=None, verification_url='', values=None,
                   reminder_detail=None, now=None):
    from app.services.notification_templates import render_account_notification
    from app.services.captcha_diagnostics import sanitize
    account = live.cookie_id
    incident = observe(db,account,event_type,detail if reminder_detail is None else reminder_detail,context=context)
    metadata = {k:(str(v)[:128] if k.endswith('_id') else sanitize(str(v))[:256]) for k,v in (values or {}).items()
                if k in ('buyer','buyer_id','item_id','chat_id','order_id','interaction_action')}
    with db.lock:
        db.conn.execute('UPDATE operational_incidents SET reminder_values=?,chat_id=?,order_id=?,reminders_enabled=1 WHERE id=?',
                        (json.dumps(metadata,ensure_ascii=False),metadata.get('chat_id','').split('@')[0],metadata.get('order_id',''),incident))
        db.conn.commit()
    message = render_account_notification(db,account,event_type,detail,verification_url=verification_url,**(values or {}))
    return await _deliver_incident(db,live,incident,event_type,message,attachment_path=attachment_path,now=now)


async def process_due(db, *, now=None, limit=100):
    """Only local state is read; reminders never poll the platform or repeat trading actions."""
    from XianyuAutoAsync import XianyuLive
    from app.services.notification_templates import render_account_notification
    now = time.time() if now is None else now
    with db.lock:
        rows = db.conn.execute('''SELECT DISTINCT i.id,i.cookie_id,i.event_type,i.reminder_detail,i.reminder_values,i.order_id
            FROM operational_incidents i JOIN operational_notices n ON n.incident_id=i.id
            JOIN message_notifications r ON r.id=n.rule_id JOIN notification_channels ch ON ch.id=r.channel_id
            JOIN cookies c ON c.id=i.cookie_id LEFT JOIN cookie_status cs ON cs.cookie_id=c.id
            WHERE i.resolved_at IS NULL AND i.acknowledged_at IS NULL AND i.reminders_enabled=1 AND n.attempts<r.reminder_limit
              AND n.status IN ('submitted','handler_completed','failed','unsupported')
              AND n.updated_at+r.reminder_interval_minutes*60<=? AND r.enabled=1 AND ch.enabled=1
              AND COALESCE(cs.enabled,1)=1 AND ch.user_id=c.user_id ORDER BY i.opened_at LIMIT ?''',(now,limit)).fetchall()
    sent = 0
    for incident,account,event,detail,raw_values,order_id in rows:
        values=json.loads(raw_values or '{}')
        if order_id:
            order=db.get_order_by_id(order_id)
            if order and order.get('cookie_id')==account:
                resolve_order(db,account,order_id,order.get('order_status'))
                resolved=False
                if event=='buyer_info_incomplete':
                    resolved=all(order.get(k) for k in ('buyer_id','buyer_nick','receiver_name','receiver_phone','receiver_address'))
                if event=='buyer_interaction_failed' and values.get('interaction_action'):
                    with db.lock:
                        row=db.conn.execute('SELECT status FROM buyer_interaction_attempts WHERE cookie_id=? AND order_id=? AND action=?',
                            (account,order_id,values['interaction_action'])).fetchone()
                    resolved=bool(row and row[0]=='sent')
                if resolved:
                    with db.lock:
                        db.conn.execute('UPDATE operational_incidents SET resolved_at=? WHERE id=?',(now,incident));db.conn.commit()
        live=XianyuLive.__new__(XianyuLive);live.cookie_id=account
        message=render_account_notification(db,account,event,detail or '请打开后台查看此事件的处理状态',**values)
        sent += await _deliver_incident(db,live,incident,event,message,now=now)
    return sent
