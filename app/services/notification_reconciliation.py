"""Bounded recovery of recent incoming messages when a full IM push is missing.

Only notifications are recovered here. Never replay chat replies or transactions.
"""
import asyncio
import hashlib
import json
import time
from loguru import logger

INTERVAL = 60
LOOKBACK = 300


def initialize(db):
    with db.lock:
        db.conn.execute('''CREATE TABLE IF NOT EXISTS buyer_notification_activation (
            cookie_id TEXT PRIMARY KEY REFERENCES cookies(id) ON DELETE CASCADE,
            activated_at REAL NOT NULL)''')
        db.conn.execute('''CREATE TABLE IF NOT EXISTS buyer_notification_receipts (
            cookie_id TEXT NOT NULL REFERENCES cookies(id) ON DELETE CASCADE,
            message_key TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY(cookie_id,message_key))''')
        db.conn.commit()


def message_key(cid, sender, text, stamp):
    # A stable hash links push and history without retaining message content.
    # Legacy callers without a timestamp keep a bounded content-based key.
    stamp = int(stamp or 0)
    if not stamp:
        stamp = int(time.time() // LOOKBACK)
    raw = json.dumps([str(cid).split('@')[0], str(sender), str(text), stamp], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def claim(db, account, cid, sender, text, stamp):
    with db.lock:
        db.conn.execute('DELETE FROM buyer_notification_receipts WHERE created_at<?', (time.time() - 7 * 86400,))
        changed = db.conn.execute('INSERT OR IGNORE INTO buyer_notification_receipts VALUES(?,?,?,?)',
                                 (account, message_key(cid, sender, text, stamp), 'sending', time.time())).rowcount
        db.conn.commit()
    return bool(changed)


def finish(db, account, key, status):
    with db.lock:
        db.conn.execute('UPDATE buyer_notification_receipts SET status=? WHERE cookie_id=? AND message_key=?',
                        (status, account, key))
        db.conn.commit()


def payload(body):
    if not isinstance(body, dict):
        return {}
    inner = body.get('data') if isinstance(body.get('data'), dict) else body
    if any(p.get('reason') or p.get('code') in ('400600001', 429, '429') for p in (body, inner)):
        raise RuntimeError('IM request limited')
    return inner


async def reconcile_once(instance, db, now=None):
    from app.services.auth_recovery import auth_paused
    from utils.risk_control import registry
    from app.xianyu_im import parse_conversation, parse_message
    from app.ai_reply_engine import AIReplyEngine
    account = instance.cookie_id
    connection = getattr(instance, 'connection_state', '')
    connection = getattr(connection, 'value', connection)
    if (not db.get_cookie_status(account) or auth_paused(account)
            or registry.get(account).is_blocked or connection != 'connected'):
        return 0
    if not any(r.get('enabled', True) for r in db.get_account_notifications(account, event_type='buyer_message')):
        # Enabling a subscription later must not send messages received while disabled.
        with db.lock:
            db.conn.execute('DELETE FROM buyer_notification_activation WHERE cookie_id=?', (account,))
            db.conn.commit()
        return 0
    now = time.time() if now is None else now
    with db.lock:
        row = db.conn.execute('SELECT activated_at FROM buyer_notification_activation WHERE cookie_id=?', (account,)).fetchone()
        if not row:
            db.conn.execute('INSERT INTO buyer_notification_activation VALUES(?,?)', (account, now))
            db.conn.commit()
            return 0
    cutoff = max(row[0], now - LOOKBACK)
    conversations = payload(await instance.get_im_conversations(limit=30))
    scans = {cid: stamp for cid, stamp in getattr(instance, '_notification_history_scans', {}).items() if stamp >= now - 3600}
    instance._notification_history_scans = scans
    eligible = []
    for item in conversations.get('userConvs', []) or []:
        raw = item.get('singleChatUserConversation', item) if isinstance(item, dict) else {}
        conv = parse_conversation(raw, instance.myid)
        if not conv or conv['lastMessageTime'] / 1000 < cutoff:
            continue
        eligible.append(conv)
    sent = 0
    # Rotate fairly when more than five threads are active; do not repeatedly
    # spend the entire budget reading the same five threads.
    for conv in sorted(eligible, key=lambda c: scans.get(c['cid'], 0))[:5]:
        history = payload(await instance.get_im_messages(conv['cid'], limit=50))
        scans[conv['cid']] = now
        for raw_message in reversed(history.get('userMessageModels', []) or []):
            msg = parse_message(raw_message, instance.myid)
            if (not msg or msg['isSelf'] or msg.get('senderId') != conv['otherUserId']
                    or msg['type'] not in ('text', 'image') or msg.get('event_type')
                    or not cutoff <= msg['time'] / 1000 <= now + 60):
                continue
            text = msg['text'] or ('[图片]' if msg['type'] == 'image' else '')
            if not text or AIReplyEngine.is_system_or_order_event(text):
                continue
            sent += await instance.send_notification(msg.get('senderName') or conv['otherUserId'], msg['senderId'],
                         text, conv['itemId'], conv['cid'], message_time=msg['time']) or 0
    return sent


async def reconcile_loop(instance):
    from app.db_manager import db_manager
    delay = INTERVAL
    while True:
        try:
            await reconcile_once(instance, db_manager)
            delay = INTERVAL
        except asyncio.CancelledError:
            raise
        except Exception as error:
            delay = min(delay * 2, 900)
            logger.warning(f'source=notification_reconciliation result={type(error).__name__} retry_seconds={delay}')
        await asyncio.sleep(delay)
