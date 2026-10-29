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
            if enabled and recovery.get('event_type'):
                live=XianyuLive.__new__(XianyuLive);live.cookie_id=account
                sent += await event_journal.dispatch(db,live,recovery['event_type'],
                    auth_recovery.FAILURES.get(recovery['failure_category'],'请在账号管理处理当前认证状态'),now=now)
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
        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = account
        sent += await event_journal.dispatch(db,live,'account_offline',
            '账号消息连接持续中断超过90秒，请打开账号管理核查连接。',context=row[0],now=now)
    return sent


async def monitor(db, get_manager):
    # Let native startup load accounts before judging absent instances.
    await asyncio.sleep(15)
    while True:
        try:
            await check_once(db,get_manager())
            from app.services.event_journal import process_due
            await process_due(db)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(f'source=account_offline_monitor error={type(error).__name__}')
        await asyncio.sleep(30)
