"""Shared, bounded and at-most-once automatic replies for push and history.

Receipts/outbound correlation store identifiers and hashes, never message bodies.
A crash after reserving a send is deliberately not retried automatically.
"""
import asyncio
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from loguru import logger

LOOKBACK = 300


def initialize(db):
    with db.lock:
        db.conn.executescript('''
        CREATE TABLE IF NOT EXISTS auto_reply_activation (
            cookie_id TEXT PRIMARY KEY REFERENCES cookies(id) ON DELETE CASCADE,
            enabled INTEGER NOT NULL, activated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS auto_reply_receipts (
            cookie_id TEXT NOT NULL REFERENCES cookies(id) ON DELETE CASCADE,
            message_key TEXT NOT NULL, platform_id TEXT NOT NULL DEFAULT '',fallback_key TEXT NOT NULL,
            state TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY(cookie_id,message_key));
        CREATE UNIQUE INDEX IF NOT EXISTS idx_reply_platform_id ON auto_reply_receipts(cookie_id,platform_id) WHERE platform_id!='';
        CREATE TABLE IF NOT EXISTS auto_reply_outbound (
            id INTEGER PRIMARY KEY, cookie_id TEXT NOT NULL REFERENCES cookies(id) ON DELETE CASCADE,
            receiver_id TEXT NOT NULL,cid TEXT NOT NULL,content_hash TEXT NOT NULL,
            platform_id TEXT NOT NULL DEFAULT '',state TEXT NOT NULL,created_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_reply_outbound ON auto_reply_outbound(cookie_id,receiver_id,cid,created_at);
        ''')
        columns = {r[1] for r in db.conn.execute('PRAGMA table_info(auto_reply_message_logs)')}
        for name, definition in {
            'message_source': "TEXT NOT NULL DEFAULT 'push'",
            'config_updated_at': "TEXT NOT NULL DEFAULT ''",
            'text_send_status': "TEXT NOT NULL DEFAULT 'not_requested'",
            'image_send_status': "TEXT NOT NULL DEFAULT 'not_requested'",
            'remaining_pause_seconds': 'INTEGER NOT NULL DEFAULT 0',
        }.items():
            if name not in columns:
                db.conn.execute(f'ALTER TABLE auto_reply_message_logs ADD COLUMN {name} {definition}')
        db.conn.execute("UPDATE auto_reply_receipts SET state='unknown' WHERE state='queued'")
        db.conn.execute("UPDATE auto_reply_message_logs SET process_status='unknown',decision_reason='result_unconfirmed',send_status='unknown',text_send_status=CASE WHEN text_send_status='sending' THEN 'unknown' ELSE text_send_status END,image_send_status=CASE WHEN image_send_status='sending' THEN 'unknown' ELSE image_send_status END WHERE process_status='pending'")
        db.conn.commit()
        for (account,) in db.conn.execute('SELECT id FROM cookies').fetchall():
            activation(db, account)


def configured(db, account):
    from app.config import AUTO_REPLY
    if AUTO_REPLY.get('api', {}).get('enabled', False):
        return True
    with db.lock:
        return bool(db.conn.execute('''SELECT 1 FROM default_replies WHERE cookie_id=? AND enabled=1
            UNION ALL SELECT 1 FROM keywords WHERE cookie_id=?
            UNION ALL SELECT 1 FROM item_replay WHERE cookie_id=? AND trim(coalesce(reply_content,''))!=''
            UNION ALL SELECT 1 FROM ai_reply_settings WHERE cookie_id=? AND ai_enabled=1 LIMIT 1''',
            (account, account, account, account)).fetchone())


def activation(db, account, now=None):
    """Only off -> on moves the watermark; edits do not lose new incoming messages."""
    now = time.time() if now is None else now
    enabled = configured(db, account)
    with db.lock:
        db.conn.execute('DELETE FROM auto_reply_receipts WHERE created_at<?', (now-7*86400,))
        db.conn.execute('DELETE FROM auto_reply_outbound WHERE created_at<?', (now-86400,))
        row = db.conn.execute('SELECT enabled,activated_at FROM auto_reply_activation WHERE cookie_id=?', (account,)).fetchone()
        if not row or bool(row[0]) != enabled:
            db.conn.execute('INSERT OR REPLACE INTO auto_reply_activation VALUES(?,?,?)', (account, enabled, now))
            db.conn.commit()
            return now if enabled else None
        db.conn.commit()
        return row[1] if enabled else None


def stamp_ms(value):
    try:
        value = float(value)
        return int(value * 1000 if value < 100000000000 else value)
    except (TypeError, ValueError, OverflowError):
        return 0


def digest(text):
    return hashlib.sha256(str(text).encode()).hexdigest()


def key_for(msg):
    # Same fallback key even when one delivery source has no platform message ID.
    return digest(json.dumps([msg['cid'], msg['senderId'], msg['text'], stamp_ms(msg['time'])], ensure_ascii=False))


def claim(db, account, msg):
    fallback = key_for(msg)
    message_id = str(msg.get('messageId') or '')
    key = 'id:' + message_id if message_id else 'hash:' + fallback
    with db.lock:
        try:
            db.conn.execute('BEGIN IMMEDIATE')
            # A fallback delivery aliases an ID delivery, but distinct known IDs
            # must remain distinct even for equal text and millisecond timestamps.
            row = db.conn.execute('''SELECT message_key,platform_id FROM auto_reply_receipts WHERE cookie_id=? AND
                (message_key=? OR (platform_id=? AND ?!='') OR (fallback_key=? AND (platform_id='' OR ?=''))) LIMIT 1''',
                (account, key, message_id, message_id, fallback, message_id)).fetchone()
            if row:
                if message_id and not row[1]:
                    db.conn.execute('UPDATE auto_reply_receipts SET platform_id=? WHERE cookie_id=? AND message_key=?', (message_id, account, row[0]))
                db.conn.commit()
                return None
            db.conn.execute('INSERT INTO auto_reply_receipts VALUES(?,?,?,?,?,?)',
                (account, key, message_id, fallback, 'queued', time.time()))
            db.conn.commit()
            return key
        except sqlite3.IntegrityError:
            db.conn.rollback()
            return None
        except Exception:
            db.conn.rollback()
            raise


def finish(db, account, key, state):
    with db.lock:
        db.conn.execute('UPDATE auto_reply_receipts SET state=? WHERE cookie_id=? AND message_key=?', (state, account, key))
        db.conn.commit()


def platform_id(response):
    if not isinstance(response, dict):
        return ''
    for node in (response, response.get('body'), response.get('data')):
        if isinstance(node, dict):
            if node.get('messageId') or node.get('msgId'):
                return str(node.get('messageId') or node['msgId'])
            for name in ('data', 'message'):
                child = node.get(name)
                if isinstance(child, dict) and child.get('messageId'):
                    return str(child['messageId'])
    return ''


def confirm_receipt(response):
    if not isinstance(response, dict):
        raise TimeoutError('缺少平台回执')
    body = response.get('body') or {}
    code = response.get('code')
    if (code is not None and str(code) != '200') or (isinstance(body, dict) and (body.get('reason') or body.get('code'))):
        raise RuntimeError('消息服务拒绝发送')
    if str(code) != '200' and not platform_id(response):
        raise TimeoutError('缺少平台成功回执')


def runtime_status(db, account):
    from app.cookie_manager import manager
    instance = (getattr(manager, 'instances', {}) or {}).get(account) if manager else None
    connection = getattr(instance, 'connection_state', '')
    connection = getattr(connection, 'value', connection)
    with db.lock:
        row = db.conn.execute('SELECT count(*),min(expires_at) FROM chat_handovers WHERE cookie_id=? AND expires_at>?', (account, time.time())).fetchone()
    return {'connected': connection == 'connected', 'paused_conversations': row[0],
            'remaining_pause_seconds': max(0, int((row[1] or 0) - time.time()))}


def reserve_outbound(db, account, msg, text):
    with db.lock:
        cursor = db.conn.execute('''INSERT INTO auto_reply_outbound(cookie_id,receiver_id,cid,content_hash,state,created_at)
            VALUES(?,?,?,?,?,?)''', (account, msg['senderId'], msg['cid'], digest(text), 'sending', time.time()))
        db.conn.commit()
        return cursor.lastrowid


def complete_outbound(db, row_id, state, response=None):
    with db.lock:
        db.conn.execute('UPDATE auto_reply_outbound SET state=?,platform_id=? WHERE id=?', (state, platform_id(response), row_id))
        db.conn.commit()


def own_automation_echo(db, account, cid, text, stamp, message_id=''):
    with db.lock:
        rows = db.conn.execute('''SELECT platform_id FROM auto_reply_outbound WHERE cookie_id=? AND cid=?
            AND content_hash=? AND created_at BETWEEN ? AND ? AND state IN ('sending','success','unknown')''',
            (account, str(cid).split('@')[0], digest(text), stamp_ms(stamp)/1000-120, stamp_ms(stamp)/1000+120)).fetchall()
    return any(not known_id or not message_id or known_id == str(message_id) for (known_id,) in rows)


def automation_echo(db, account, msg):
    """Correlate only system automatic sends between accounts of the same owner."""
    with db.lock:
        rows = db.conn.execute('''SELECT o.platform_id FROM auto_reply_outbound o
            JOIN cookies source ON source.id=o.cookie_id JOIN cookies target ON target.id=? AND target.user_id=source.user_id
            WHERE o.cookie_id=? AND o.receiver_id=? AND o.cid=? AND o.content_hash=?
            AND o.created_at BETWEEN ? AND ? AND o.state IN ('sending','success','unknown')''',
            (account, msg['senderId'], str(account), msg['cid'], digest(msg['text']),
             stamp_ms(msg['time']) / 1000 - 120, stamp_ms(msg['time']) / 1000 + 120)).fetchall()
    incoming_id = str(msg.get('messageId') or '')
    return any(not known_id or not incoming_id or known_id == incoming_id for (known_id,) in rows)


def log(db, instance, msg, source, **values):
    settings = db.get_default_reply(instance.cookie_id) or {}
    return db.add_auto_reply_log(cookie_id=instance.cookie_id, chat_id=msg['cid'], item_id=msg.get('itemId'),
        source_message_id=msg.get('messageId'), sender_user_id=msg['senderId'], sender_user_name=msg.get('senderName'),
        source_message=msg['text'], message_source=source, config_updated_at=settings.get('updated_at') or '', **values)


def available(instance, db):
    from app.services.auth_recovery import auth_paused
    from utils.risk_control import registry
    connection = getattr(instance, 'connection_state', '')
    return (db.get_cookie_status(instance.cookie_id) and not auth_paused(instance.cookie_id)
            and not registry.get(instance.cookie_id).is_blocked and getattr(connection, 'value', connection) == 'connected')


async def accept(instance, db, msg, source='push'):
    from app.ai_reply_engine import AIReplyEngine
    from app.config import AUTO_REPLY
    msg = dict(msg)
    msg['cid'] = str(msg.get('cid') or '').split('@')[0]
    msg['senderId'] = str(msg.get('senderId') or '').split('@')[0]
    msg['text'] = str(msg.get('text') or ('[图片]' if msg.get('type') == 'image' else ''))
    msg['time'] = stamp_ms(msg.get('time'))
    if (not msg['cid'] or not msg['senderId'] or msg['senderId'] in ('unknown', '0', str(instance.myid))
            or msg.get('type', 'text') not in ('text', 'image') or msg.get('event_type')
            or not msg['text'] or AIReplyEngine.is_system_or_order_event(msg['text'])):
        return False
    now = time.time()
    active = activation(db, instance.cookie_id)
    if not AUTO_REPLY.get('enabled', True):
        active = None
    # No historical replay on startup/enable. Reject undated or future messages.
    if not now - LOOKBACK <= msg['time'] / 1000 <= now + 60:
        return False
    if active is not None and msg['time'] < int(active*1000):
        return False
    if not available(instance, db):
        return False
    key = claim(db, instance.cookie_id, msg)
    if key is None:
        return False
    if active is None:
        log(db, instance, msg, source, process_status='skipped', decision_reason='auto_reply_disabled')
        finish(db, instance.cookie_id, key, 'auto_reply_disabled')
        return False
    if automation_echo(db, instance.cookie_id, msg):
        log(db, instance, msg, source, process_status='skipped', decision_reason='automation_echo')
        finish(db, instance.cookie_id, key, 'automation_echo')
        return False
    pending = getattr(instance, '_auto_reply_pending', None)
    if pending is None:
        pending = instance._auto_reply_pending = {}
    previous = pending.get(msg['cid'])
    if previous:
        previous.cancel()
    async def run():
        try:
            await asyncio.sleep(getattr(instance, 'message_debounce_delay', 1))
            if pending.get(msg['cid']) is asyncio.current_task():
                pending.pop(msg['cid'], None)
            locks = getattr(instance, '_auto_reply_locks', None)
            if locks is None:
                locks = instance._auto_reply_locks = {}
            async with locks.setdefault(msg['cid'], asyncio.Lock()):
                await process(instance, db, msg, source, key)
        except asyncio.CancelledError:
            finish(db, instance.cookie_id, key, 'cancelled')
            raise
        except Exception as error:
            finish(db, instance.cookie_id, key, 'failed')
            log(db, instance, msg, source, process_status='failed', decision_reason='reply_selection_failed', error_message=type(error).__name__)
            logger.error(f'auto_reply source={source} error={type(error).__name__}')
        finally:
            if pending.get(msg['cid']) is asyncio.current_task():
                pending.pop(msg['cid'], None)
    task = instance._create_tracked_task(run())
    pending[msg['cid']] = task
    return True


async def prepare_image(instance, url):
    """Prepare once without writing a generated CDN URL over the user's configuration."""
    width, height = 800, 600
    if url.startswith(('/static/uploads/', 'static/uploads/')):
        root = Path('static/uploads').resolve()
        path = Path(url.lstrip('/')).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('默认回复图片不存在或路径无效')
        from utils.image_uploader import ImageUploader
        async with ImageUploader(instance.cookies_str) as uploader:
            result = await uploader.upload_image(str(path))
        if not result:
            raise ValueError('默认回复图片上传失败')
        from utils.image_utils import image_manager
        width, height = image_manager.get_image_size(str(path))
        return result, width or 800, height or 600
    if not url.startswith(('https://', 'http://')):
        raise ValueError('回复图片地址无效')
    actual_width, actual_height = await instance._get_image_size_from_url(url)
    return url, actual_width or width, actual_height or height


async def process(instance, db, msg, source, key):
    from XianyuAutoAsync import pause_manager
    from app.config import AUTO_REPLY
    account = instance.cookie_id
    log_id = log(db, instance, msg, source, process_status='pending', decision_reason='selecting_reply')
    def update(**values):
        db.update_auto_reply_log(log_id, **values)
    def skip(reason):
        update(process_status='skipped', decision_reason=reason)
        finish(db, account, key, reason)
    if not available(instance, db):
        skip('account_unavailable'); return
    if not AUTO_REPLY.get('enabled', True):
        skip('auto_reply_disabled'); return
    active = activation(db, account)
    if active is None:
        skip('auto_reply_disabled'); return
    if msg['time'] < max(int(active*1000), int((time.time()-LOOKBACK)*1000)):
        skip('message_outside_window'); return
    if pause_manager.is_chat_paused(msg['cid'], account):
        update(remaining_pause_seconds=pause_manager.get_remaining_pause_time(msg['cid'], account))
        skip('chat_paused'); return
    matched = db.matches_message_filter(account, msg['text'], 'skip_reply')
    if matched:
        update(matched_keyword=matched); skip('skip_reply_filter'); return
    text, image, strategy = '', '', 'none'
    try:
        if AUTO_REPLY.get('api', {}).get('enabled', False):
            text = await instance.get_api_reply('', f'https://www.goofish.com/personal?userId={msg["senderId"]}',
                msg['senderId'], msg.get('senderName') or '', msg.get('itemId'), msg['text'], msg['cid'])
            strategy = 'api'
        else:
            reply = await instance.get_keyword_reply(msg.get('senderName') or '', msg['senderId'], msg['text'], msg.get('itemId'))
            if reply == 'EMPTY_REPLY':
                update(reply_strategy='keyword'); skip('empty_reply'); return
            if reply:
                strategy = 'keyword'
                update(matched_keyword=instance._find_reply_keyword(msg['text'], msg.get('itemId')))
                if reply.startswith('__IMAGE_SEND__'):
                    image = reply[len('__IMAGE_SEND__'):]
                else:
                    text = reply
            else:
                text = await instance.get_ai_reply(msg.get('senderName') or '', msg['senderId'], msg['text'], msg.get('itemId'), msg['cid'])
                if text:
                    strategy = 'ai'
                else:
                    settings = db.get_default_reply(account) or {}
                    item = db.get_item_reply(account, msg.get('itemId')) if msg.get('itemId') else None
                    if not (item and item.get('reply_content')):
                        if not settings.get('enabled'):
                            skip('default_reply_disabled'); return
                        if settings.get('reply_once') and db.has_default_reply_record(account, msg['cid']):
                            update(reply_strategy='default'); skip('reply_once'); return
                    reply = await instance.get_default_reply(msg.get('senderName') or '', msg['senderId'], msg['text'], msg['cid'], msg.get('itemId'))
                    strategy = 'default'
                    if reply == 'EMPTY_REPLY':
                        update(reply_strategy=strategy); skip('empty_reply'); return
                    if reply:
                        text, image = reply.get('text') or '', reply.get('image_url') or ''
        text = str(text or '').strip()
        update(reply_strategy=strategy, reply_text=text or ('[图片]' if image else None))
        if not text and not image:
            skip('no_rule_matched'); return
    except Exception as error:
        update(process_status='failed', decision_reason='reply_selection_failed', error_message=type(error).__name__)
        finish(db, account, key, 'failed'); return
    states = {'text': 'not_requested', 'image': 'not_requested'}
    errors = []
    for kind, content in (('image', image), ('text', text)):
        if not content:
            continue
        if not configured(db, account):
            states[kind] = 'skipped'; errors.append('auto_reply_disabled'); continue
        if not available(instance, db) or pause_manager.is_chat_paused(msg['cid'], account):
            states[kind] = 'skipped'
            errors.append('chat_paused_before_send' if pause_manager.is_chat_paused(msg['cid'], account) else 'account_unavailable')
            continue
        row_id = None
        try:
            if kind == 'image':
                url, width, height = await prepare_image(instance, content)
            # The upload may have taken time; honor takeover immediately before sending.
            if not configured(db, account):
                states[kind] = 'skipped'; errors.append('auto_reply_disabled'); continue
            if not available(instance, db) or pause_manager.is_chat_paused(msg['cid'], account):
                states[kind] = 'skipped'; errors.append('chat_paused_before_send'); continue
            row_id = reserve_outbound(db, account, msg, '[图片]' if kind == 'image' else content)
            states[kind] = 'sending'
            update(**{f'{kind}_send_status': 'sending'})
            response = (await instance.send_im_image(msg['cid'], msg['senderId'], url, width, height) if kind == 'image'
                        else await instance.send_im_text(msg['cid'], msg['senderId'], content))
            confirm_receipt(response)
            states[kind] = 'success'
            complete_outbound(db, row_id, 'success', response)
        except (TimeoutError, ConnectionError, asyncio.CancelledError) as error:
            states[kind] = 'unknown' if row_id else 'failed'
            errors.append('result_unconfirmed' if row_id else 'image_prepare_failed')
            if row_id:
                complete_outbound(db, row_id, 'unknown')
            if isinstance(error, asyncio.CancelledError):
                update(process_status='unknown', decision_reason='result_unconfirmed', send_status='unknown',
                       text_send_status=states['text'], image_send_status=states['image'])
                finish(db, account, key, 'unknown')
                raise
        except Exception as error:
            states[kind] = 'failed'
            errors.append('platform_rejected' if row_id else 'image_prepare_failed')
            if row_id:
                complete_outbound(db, row_id, 'failed')
        update(**{f'{kind}_send_status': states[kind]})
    requested = [state for state in states.values() if state != 'not_requested']
    status = 'success' if all(s == 'success' for s in requested) else ('unknown' if 'unknown' in requested else 'failed')
    reason = 'reply_sent' if status == 'success' else ('result_unconfirmed' if status == 'unknown' else (errors[0] if errors else 'send_failed'))
    update(process_status=status, send_status='success' if status == 'success' else ('unknown' if status == 'unknown' else 'failed'),
           decision_reason=reason, error_message='、'.join(dict.fromkeys(errors)) or None,
           text_send_status=states['text'], image_send_status=states['image'])
    if status == 'success' and strategy == 'default':
        instance._record_default_reply_sent(msg['cid'])
    finish(db, account, key, status)
