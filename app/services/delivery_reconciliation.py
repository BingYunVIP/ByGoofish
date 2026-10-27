"""Bounded seller-side fallback. Existing delivery ledgers still own stock/sends."""
from datetime import datetime, timezone
import json
import time
from loguru import logger

INTERVAL_SECONDS = 300
MAX_ORDER_AGE_SECONDS = 86400


def initialize(db):
    with db.lock:
        db.conn.execute('''CREATE TABLE IF NOT EXISTS delivery_reconciliation_activation (
            cookie_id TEXT PRIMARY KEY REFERENCES cookies(id) ON DELETE CASCADE,
            activated_at REAL NOT NULL)''')
        db.conn.execute('''CREATE TABLE IF NOT EXISTS delivery_reconciliation_runs (
            cookie_id TEXT NOT NULL REFERENCES cookies(id) ON DELETE CASCADE,
            order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
            status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY(cookie_id,order_id))''')
        db.conn.execute('INSERT OR IGNORE INTO delivery_reconciliation_activation SELECT id,? FROM cookies',
                        (time.time(),))
        db.conn.commit()


def activate(db, cookie_id):
    with db.lock:
        if not db.conn.execute('SELECT 1 FROM cookies WHERE id=?', (cookie_id,)).fetchone():
            return None
        db.conn.execute('INSERT OR IGNORE INTO delivery_reconciliation_activation VALUES(?,?)',
                        (cookie_id, time.time()))
        db.conn.commit()
        return db.conn.execute('SELECT activated_at FROM delivery_reconciliation_activation WHERE cookie_id=?',
                               (cookie_id,)).fetchone()[0]


def resolve_chat_id(db, cookie_id, buyer_id, item_id):
    """Require account, buyer and product; ambiguous conversations need review."""
    if not buyer_id or not item_id:
        return None
    with db.lock:
        matches = set()
        for cid, raw in db.conn.execute('SELECT cid,snapshot FROM chat_workspace_state WHERE cookie_id=?',
                                       (cookie_id,)).fetchall():
            try:
                snapshot = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(snapshot, dict):
                continue
            if (str(snapshot.get('otherUserId', '')) == str(buyer_id)
                    and str(snapshot.get('itemId', '')) == str(item_id) and cid):
                matches.add(str(cid))
        if matches:
            return next(iter(matches)) if len(matches) == 1 else None
        for sql, args in (
            ("SELECT DISTINCT chat_id FROM orders WHERE cookie_id=? AND buyer_id=? AND item_id=?",
             (cookie_id, buyer_id, item_id)),
            ("SELECT DISTINCT chat_id FROM ai_conversations WHERE cookie_id=? AND user_id=? AND item_id=?",
             (cookie_id, buyer_id, item_id)),
        ):
            matches = {str(r[0]) for r in db.conn.execute(sql, args).fetchall() if r[0]}
            if matches:
                return next(iter(matches)) if len(matches) == 1 else None
        return None


def fresh_payment_event(db, cookie_id, timestamp_ms):
    """Do not turn replayed pre-upgrade payment messages into new deliveries."""
    try:
        stamp = float(timestamp_ms) / 1000
    except (TypeError, ValueError):
        return False
    with db.lock:
        row = db.conn.execute('SELECT activated_at FROM delivery_reconciliation_activation WHERE cookie_id=?',
                               (cookie_id,)).fetchone()
    return bool(row and stamp >= row[0] and 0 <= time.time() - stamp <= MAX_ORDER_AGE_SECONDS)


def eligible(db, cookie_id, parsed, activated_at, now):
    from utils.order_status_rules import normalize_order_status
    order_id = parsed.get('order_id')
    if not order_id or normalize_order_status('', parsed.get('status_text')) != 'pending_ship':
        return False
    try:
        created = datetime.fromisoformat(parsed['created_at'])
        stamp = (created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created).timestamp()
    except (KeyError, TypeError, ValueError):
        return False
    if stamp < activated_at or not 0 <= now - stamp <= MAX_ORDER_AGE_SECONDS:
        return False
    with db.lock:
        config = db.conn.execute('SELECT enabled FROM item_delivery_configs WHERE cookie_id=? AND item_id=?',
                                 (cookie_id, parsed.get('item_id'))).fetchone()
        row = db.get_order_by_id(order_id)
        attempted = db.conn.execute('SELECT 1 FROM delivery_attempts WHERE cookie_id=? AND order_id=?',
                                    (cookie_id, order_id)).fetchone()
        scheduled = db.conn.execute('SELECT 1 FROM delivery_reconciliation_runs WHERE cookie_id=? AND order_id=?',
                                    (cookie_id, order_id)).fetchone()
        return bool(config and config[0] and row and row.get('cookie_id') == cookie_id
                    and row.get('order_status') == 'pending_ship' and not row.get('system_shipped')
                    and parsed.get('buyer_id') and parsed.get('item_id') and not attempted and not scheduled)


async def reconcile(live, db):
    from app.services import auth_recovery
    from utils import risk_control
    from utils.seller_order_sync import _save_order
    from utils.xianyu_seller_api import XianyuSellerAPI, parse_sold_order
    from utils.xianyu_utils import trans_cookies

    def ready():
        ws = getattr(live, 'ws', None)
        return (not auth_recovery.get(db, live.cookie_id)['auth_paused']
                and not risk_control.registry.get(live.cookie_id).is_blocked
                and ws is not None and not getattr(ws, 'closed', True))

    result = {'checked': 0, 'dispatched': 0}
    if not ready():
        return result
    activated = activate(db, live.cookie_id)
    source = db.get_cookie(live.cookie_id)
    if activated is None or not source:
        return result
    api = XianyuSellerAPI(live.cookie_id, source)
    try:
        batch = await api.get_sold_orders(query_code='NOT_SHIP', rows_per_page=20)
    finally:
        await api.close()
    if not ready() or db.get_cookie(live.cookie_id) != source:
        return result
    if api.cookies_str != source:
        from app.services.account_auth_client import save_cookie_delta
        saved = save_cookie_delta(db, live.cookie_id, source, trans_cookies(api.cookies_str))
        if saved is None:
            return result
        live.cookies_str, live.cookies = saved, trans_cookies(saved)
    candidates = []
    for item in batch.get('items') or []:
        parsed = parse_sold_order(item)
        if not parsed.get('order_id'):
            continue
        result['checked'] += 1
        with db.lock:
            previous = db.get_order_by_id(parsed['order_id']) or {}
            if previous.get('system_shipped') or previous.get('order_status') in ('shipped', 'completed', 'cancelled', 'refunded'):
                continue
            saved = _save_order(db, live.cookie_id, parsed)
        if saved and eligible(db, live.cookie_id, parsed, activated, time.time()):
            candidates.append(parsed)
    if batch.get('next_page'):
        logger.warning(f'【{live.cookie_id}】待发货核对仅检查本页20条，更多订单需在订单管理核对')
    loaded_conversations = False
    for parsed in candidates:
        if not ready():
            break
        order_id, buyer, item_id = parsed['order_id'], parsed['buyer_id'], parsed['item_id']
        row = db.get_order_by_id(order_id) or {}
        chat = row.get('chat_id') or resolve_chat_id(db, live.cookie_id, buyer, item_id)
        if not chat and not loaded_conversations:
            from app.xianyu_im import parse_conversation
            from app.services.chat_workspace import observe_conversations
            body = await live.get_im_conversations(None, 100)
            if not ready():
                break
            conversations = []
            for raw in body.get('userConvs', []) if isinstance(body, dict) else []:
                raw = raw.get('singleChatUserConversation', raw) if isinstance(raw, dict) else {}
                conversation = parse_conversation(raw, str(live.myid))
                if conversation:
                    conversations.append(conversation)
            details = db.get_cookie_details(live.cookie_id) or {}
            observe_conversations(db, details.get('user_id'), live.cookie_id, conversations)
            loaded_conversations = True
            chat = resolve_chat_id(db, live.cookie_id, buyer, item_id)
        if not chat:
            logger.warning(f'【{live.cookie_id}】待发货核对缺少唯一买家/商品会话 order_suffix={order_id[-8:]}，未发送')
            continue
        if not ready() or not eligible(db, live.cookie_id, parsed, activated, time.time()):
            continue
        with db.lock:
            now = time.time()
            claim = db.conn.execute('INSERT OR IGNORE INTO delivery_reconciliation_runs VALUES(?,?,?,?,?)',
                                    (live.cookie_id, order_id, 'started', now, now))
            db.conn.commit()
            if not claim.rowcount:
                continue
        logger.info(f'【{live.cookie_id}】卖家待发货核对进入统一发货 order_suffix={order_id[-8:]} source=seller_pending')
        try:
            await live._handle_auto_delivery(live.ws, {'3': {'orderId': order_id}},
                parsed.get('buyer_nick') or '买家', buyer, item_id, chat, time.strftime('%Y-%m-%d %H:%M:%S'))
        finally:
            current = db.get_order_by_id(order_id) or {}
            with db.lock:
                db.conn.execute('UPDATE delivery_reconciliation_runs SET status=?,updated_at=? WHERE cookie_id=? AND order_id=?',
                    ('done' if current.get('system_shipped') else 'review', time.time(), live.cookie_id, order_id))
                db.conn.commit()
        result['dispatched'] += 1
    return result
