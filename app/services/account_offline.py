"""Persistent account message-connection incidents; independent of platform auth.

The monitor only reads local runtime state and sends subscribed notifications.
It never requests tokens, logs in, or replays messages/orders.
"""
import asyncio
import json
import secrets
import time
from loguru import logger

GRACE_SECONDS = 90


def initialize(db):
    with db.lock:
        db.conn.execute('''CREATE TABLE IF NOT EXISTS account_offline_incidents (
            cookie_id TEXT PRIMARY KEY REFERENCES cookies(id) ON DELETE CASCADE,
            incident_id TEXT NOT NULL, offline_since REAL NOT NULL, reason_code TEXT NOT NULL,
            resolved_at REAL)''')
        db.conn.execute('''CREATE TABLE IF NOT EXISTS account_offline_notices (
            cookie_id TEXT NOT NULL REFERENCES cookies(id) ON DELETE CASCADE,
            incident_id TEXT NOT NULL, rule_id INTEGER NOT NULL,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL,
            PRIMARY KEY(incident_id,rule_id))''')
        db.conn.execute("UPDATE account_offline_notices SET status='unknown' WHERE status='sending'")
        db.conn.commit()


def connected(instance, now):
    state = getattr(instance, 'connection_state', '')
    state = getattr(state, 'value', state)
    ws = getattr(instance, 'ws', None)
    return bool(state == 'connected' and ws and not getattr(ws, 'closed', True)
                and now - getattr(instance, 'last_heartbeat_response', 0) < 90)


async def check_once(db, manager, now=None):
    if manager is None:
        return 0
    from app.services import auth_recovery, event_journal
    from app.services.notification_delivery import deliver
    from app.services.notification_templates import render_account_notification
    from XianyuAutoAsync import XianyuLive
    now = time.time() if now is None else now
    sent = 0
    for account in db.get_all_cookies():
        enabled = db.get_cookie_status(account)
        instance = manager.instances.get(account)
        recovery = auth_recovery.get(db, account)
        if recovery['auth_paused']:
            # Specific auth/credential incident is the actionable alert; don't
            # send a second generic offline email for the same paused cause.
            with db.lock:
                db.conn.execute('UPDATE account_offline_incidents SET resolved_at=? WHERE cookie_id=? AND resolved_at IS NULL', (now,account))
                db.conn.commit()
            event_journal.resolve(db,account,('account_offline',),superseded=True)
            continue
        if not enabled or (connected(instance, now) and recovery['recovery_state'] not in auth_recovery.PAUSED):
            with db.lock:
                previous = db.conn.execute('SELECT incident_id,offline_since FROM account_offline_incidents WHERE cookie_id=? AND resolved_at IS NULL', (account,)).fetchone()
                db.conn.execute('UPDATE account_offline_incidents SET resolved_at=? WHERE cookie_id=? AND resolved_at IS NULL', (now,account))
                db.conn.commit()
            event_journal.resolve(db,account,('account_offline',),superseded=not enabled)
            if enabled and previous and now-previous[1] >= GRACE_SECONDS:
                live = XianyuLive.__new__(XianyuLive)
                live.cookie_id = account
                sent += await event_journal.dispatch(db,live,'message_connection_recovered',
                    '消息连接和心跳已确认恢复',context=previous[0])
            continue
        reason = recovery['failure_category'] if recovery['recovery_state']=='required' else 'message_connection_lost'
        with db.lock:
            row = db.conn.execute('SELECT incident_id,offline_since,resolved_at FROM account_offline_incidents WHERE cookie_id=?', (account,)).fetchone()
            if not row or row[2] is not None:
                row = (secrets.token_hex(16),now,None)
                db.conn.execute('INSERT OR REPLACE INTO account_offline_incidents VALUES(?,?,?,?,NULL)', (account,row[0],now,reason))
                db.conn.commit()
        if now-row[1] < GRACE_SECONDS:
            continue
        incident = event_journal.observe(db,account,'account_offline','消息连接持续中断超过90秒观察期',context=row[0])
        rules = db.get_account_notifications(account, user_id=db.get_cookie_owner_user(account), event_type='account_offline')
        for rule in rules:
            if not rule.get('enabled', True):
                continue
            with db.lock:
                previous = db.conn.execute('SELECT status,attempts,next_attempt_at FROM account_offline_notices WHERE incident_id=? AND rule_id=?', (row[0],rule['id'])).fetchone()
                if previous and (previous[0] != 'failed' or previous[1]>=3 or previous[2]>now):
                    continue
                attempts = (previous[1] if previous else 0)+1
                db.conn.execute('INSERT OR REPLACE INTO account_offline_notices VALUES(?,?,?,?,?,?,?)',
                    (account,row[0],rule['id'],'sending',attempts,0,now))
                db.conn.commit()
            live = XianyuLive.__new__(XianyuLive)
            live.cookie_id = account
            message = ('账号消息服务持续中断，当前无法监听新消息或自动处理新订单。\n'
                       '原因：'+auth_recovery.FAILURES.get(reason,'消息连接持续断开')+'\n'
                       '请打开后台账号管理查看诊断并检查连接。\n'
                       '此通知表示后台消息连接状态，不代表买家端“来过”状态。')
            message = render_account_notification(db,account,'account_offline',message)
            receipts=[]
            try:
                sent += await deliver(live,[rule],'account_offline',message,receipt_sink=receipts)
                status = receipts[0]['status'] if receipts else 'unknown'
                if status not in ('submitted','failed'):
                    status='unknown'
            except BaseException:
                with db.lock:
                    db.conn.execute("UPDATE account_offline_notices SET status='unknown',updated_at=? WHERE incident_id=? AND rule_id=?",(time.time(),row[0],rule['id']))
                    db.conn.commit()
                raise
            with db.lock:
                db.conn.execute('UPDATE account_offline_notices SET status=?,next_attempt_at=?,updated_at=? WHERE incident_id=? AND rule_id=?',
                                (status,now+300*2**(attempts-1),now,row[0],rule['id']))
                db.conn.commit()
            with db.lock:
                db.conn.execute("UPDATE risk_control_logs SET processing_result=?,error_message=?,updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=(SELECT log_id FROM operational_incidents WHERE id=?)",
                    ('通知已提交；账号仍离线' if status=='submitted' else '通知'+('结果待核查，不自动重发' if status=='unknown' else '提交失败，最多受控重试3次'),
                     '' if status=='submitted' else status,incident))
                db.conn.commit()
    return sent


async def monitor(db, get_manager):
    # Let native startup load accounts before judging absent instances.
    await asyncio.sleep(15)
    while True:
        try:
            await check_once(db,get_manager())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(f'source=account_offline_monitor error={type(error).__name__}')
        await asyncio.sleep(30)
