"""Account-scoped operator state, durable outgoing claims and local IM invalidations.

No platform requests or message sends originate in this module.
"""

import asyncio
import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from contextlib import contextmanager


def initialize(cursor):
    for sql in (
        """CREATE TABLE IF NOT EXISTS chat_workspace_state (
        user_id INTEGER NOT NULL,cookie_id TEXT NOT NULL,cid TEXT NOT NULL,
        snapshot TEXT NOT NULL DEFAULT '{}',pinned INTEGER NOT NULL DEFAULT 0,
        viewed_time INTEGER NOT NULL DEFAULT 0,viewed_summary TEXT NOT NULL DEFAULT '',viewed_count INTEGER NOT NULL DEFAULT 0,
        last_inbound INTEGER NOT NULL DEFAULT 0,last_outbound INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(user_id,cookie_id,cid),FOREIGN KEY(cookie_id) REFERENCES cookies(id) ON DELETE CASCADE)""",
        """CREATE TABLE IF NOT EXISTS chat_customer_notes (
        user_id INTEGER NOT NULL,cookie_id TEXT NOT NULL,buyer_id TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(user_id,cookie_id,buyer_id),FOREIGN KEY(cookie_id) REFERENCES cookies(id) ON DELETE CASCADE)""",
        """CREATE TABLE IF NOT EXISTS chat_buyer_profiles (
        user_id INTEGER NOT NULL,cookie_id TEXT NOT NULL,buyer_id TEXT NOT NULL,
        nickname TEXT NOT NULL DEFAULT '',source TEXT NOT NULL DEFAULT '',
        fetched_at REAL NOT NULL DEFAULT 0,retry_after REAL NOT NULL DEFAULT 0,
        fetch_started_at REAL NOT NULL DEFAULT 0,
        PRIMARY KEY(user_id,cookie_id,buyer_id),
        FOREIGN KEY(cookie_id) REFERENCES cookies(id) ON DELETE CASCADE)""",
        """CREATE TABLE IF NOT EXISTS chat_handovers (
        cookie_id TEXT NOT NULL,cid TEXT NOT NULL,expires_at REAL NOT NULL,
        PRIMARY KEY(cookie_id,cid),FOREIGN KEY(cookie_id) REFERENCES cookies(id) ON DELETE CASCADE)""",
        """CREATE TABLE IF NOT EXISTS chat_send_attempts (
        user_id INTEGER NOT NULL,cookie_id TEXT NOT NULL,request_id TEXT NOT NULL,cid TEXT NOT NULL,
        recipient TEXT NOT NULL,kind TEXT NOT NULL,payload_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'sending',message_id TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,updated_at REAL NOT NULL,
        PRIMARY KEY(cookie_id,request_id),FOREIGN KEY(cookie_id) REFERENCES cookies(id) ON DELETE CASCADE)""",
    ):
        cursor.execute(sql)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS chat_send_by_conversation ON chat_send_attempts(user_id,cookie_id,cid,created_at)"
    )
    if 'avatar_url' not in {r[1] for r in cursor.execute('PRAGMA table_info(chat_buyer_profiles)')}:
        cursor.execute("ALTER TABLE chat_buyer_profiles ADD COLUMN avatar_url TEXT NOT NULL DEFAULT ''")
    # Old versions persisted notification headings as names. Repair only that cached field.
    for uid, account, cid, raw in cursor.execute(
        "SELECT user_id,cookie_id,cid,snapshot FROM chat_workspace_state"
    ).fetchall():
        try:
            snapshot = json.loads(raw)
        except (TypeError, ValueError):
            continue
        name = snapshot.get("otherUserName")
        if name and not _valid_nickname(name):
            snapshot["otherUserName"] = ""
            cursor.execute("UPDATE chat_workspace_state SET snapshot=? WHERE user_id=? AND cookie_id=? AND cid=?",
                           (json.dumps(snapshot, ensure_ascii=False), uid, account, cid))
    for uid, account, buyer, name in cursor.execute(
        "SELECT user_id,cookie_id,buyer_id,nickname FROM chat_buyer_profiles WHERE nickname<>''"
    ).fetchall():
        if not _valid_nickname(name):
            cursor.execute("UPDATE chat_buyer_profiles SET nickname='',source='invalidated',fetched_at=0,retry_after=0,fetch_started_at=0 WHERE user_id=? AND cookie_id=? AND buyer_id=?",
                           (uid, account, buyer))


BUYER_PROFILE_RETRY_SECONDS = 6 * 60 * 60
BUYER_PROFILE_FETCH_LEASE_SECONDS = 120


def buyer_profile(db, user_id, cookie_id, buyer_id):
    with db.lock:
        row = _row(
            db,
            "SELECT nickname,source,fetched_at,retry_after,fetch_started_at,avatar_url "
            "FROM chat_buyer_profiles WHERE user_id=? AND cookie_id=? AND buyer_id=?",
            (user_id, cookie_id, buyer_id),
        )
    if row and row["nickname"] and not _valid_nickname(row["nickname"]):
        with db.lock:
            db.conn.execute(
                "UPDATE chat_buyer_profiles SET nickname='',source='invalidated',fetched_at=0,retry_after=0,fetch_started_at=0 WHERE user_id=? AND cookie_id=? AND buyer_id=?",
                (user_id, cookie_id, buyer_id),
            )
            db.conn.commit()
        row = None
    return row or {
        "nickname": "",
        "avatar_url": "",
        "source": "",
        "fetched_at": 0,
        "retry_after": 0,
        "fetch_started_at": 0,
    }


def claim_buyer_profile_fetch(db, user_id, cookie_id, buyer_id, now=None, *, need_avatar=False):
    now = float(now if now is not None else time.time())
    with db.lock:
        row = _row(
            db,
            "SELECT nickname,retry_after,fetch_started_at,avatar_url,fetched_at FROM chat_buyer_profiles "
            "WHERE user_id=? AND cookie_id=? AND buyer_id=?",
            (user_id, cookie_id, buyer_id),
        )
        if row and row["nickname"] and _valid_nickname(row["nickname"]) and not need_avatar:
            return False
        if need_avatar and row and row['avatar_url'] and row['fetched_at'] > now-86400:
            return False
        if row and float(row["retry_after"] or 0) > now:
            return False
        if row and float(row["fetch_started_at"] or 0) > now - BUYER_PROFILE_FETCH_LEASE_SECONDS:
            return False
        db.conn.execute(
            """INSERT INTO chat_buyer_profiles
            (user_id,cookie_id,buyer_id,retry_after,fetch_started_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id,cookie_id,buyer_id) DO UPDATE SET
                retry_after=excluded.retry_after,fetch_started_at=excluded.fetch_started_at""",
            (
                user_id,
                cookie_id,
                buyer_id,
                now + BUYER_PROFILE_RETRY_SECONDS,
                now,
            ),
        )
        db.conn.commit()
        return True


def finish_buyer_profile_fetch(
    db, user_id, cookie_id, buyer_id, nickname="", now=None, *, avatar_url=""
):
    now = float(now if now is not None else time.time())
    nickname = str(nickname or "").strip()[:120]
    if not _valid_nickname(nickname):
        nickname = ""
    avatar_url = valid_avatar(avatar_url)
    with db.lock:
        db.conn.execute(
            """INSERT INTO chat_buyer_profiles
            (user_id,cookie_id,buyer_id,nickname,source,fetched_at,retry_after,fetch_started_at,avatar_url)
            VALUES(?,?,?,?,?,?,?,0,?)
            ON CONFLICT(user_id,cookie_id,buyer_id) DO UPDATE SET
                nickname=CASE WHEN excluded.nickname<>'' THEN excluded.nickname ELSE chat_buyer_profiles.nickname END,
                avatar_url=CASE WHEN excluded.avatar_url<>'' THEN excluded.avatar_url ELSE chat_buyer_profiles.avatar_url END,
                source=excluded.source,fetched_at=excluded.fetched_at,retry_after=excluded.retry_after,fetch_started_at=0""",
            (
                user_id,
                cookie_id,
                buyer_id,
                nickname,
                "public_profile" if nickname else "unavailable",
                now if nickname or avatar_url else 0,
                0 if avatar_url else now + BUYER_PROFILE_RETRY_SECONDS,
                avatar_url,
            ),
        )
        db.conn.commit()


def _row(db, sql, args):
    cursor = db.conn.execute(sql, args)
    row = cursor.fetchone()
    return dict(zip([c[0] for c in cursor.description], row)) if row else None


def _valid_nickname(value):
    from app.xianyu_im import is_valid_participant_nickname

    return is_valid_participant_nickname(value)


def _order_created_epoch(value):
    if not value:
        return None
    try:
        date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        # orders.created_at is normalized to UTC by utils.order_time.
        return date.replace(tzinfo=timezone.utc).timestamp() if date.tzinfo is None else date.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _event_order_state_compatible(event_type, status):
    states = {
        'order_pending_payment': {'processing', 'pending_ship', 'shipped', 'completed', 'refunding', 'cancelled'},
        'order_paid_pending_ship': {'pending_ship', 'shipped', 'completed', 'refunding', 'cancelled'},
        'seller_shipped': {'shipped', 'completed', 'refunding', 'cancelled'},
        'shipping_reminder': {'pending_ship', 'shipped', 'completed', 'refunding', 'cancelled'},
        'buyer_received': {'completed', 'refunding'},
        'review_requested': {'completed', 'refunding'},
        'review_completed': {'completed', 'refunding'},
        'receipt_thanks': {'completed', 'refunding'},
        'flower_requested': {'completed', 'refunding'},
        'flower_sent_confirmation': {'completed', 'refunding'},
        'flower_received': {'completed', 'refunding'},
        'refund_requested': {'refunding', 'cancelled', 'completed'},
        'refund_completed': {'refunding', 'cancelled', 'completed'},
        'order_closed': {'cancelled', 'refunding'},
    }
    return str(status or '').lower() in states.get(event_type, set())


def associate_transaction_events(db, cookie_id, cid, messages, user_id=None, my_id=None):
    """Resolve display-only links from card ids, interaction receipts, then UTC event time."""
    from app.chat_events import resolve_direction

    my_id = str(my_id or cookie_id)
    with db.lock:
        owner = db.get_cookie_owner_user(cookie_id)
        if owner is None or (user_id is not None and user_id != owner):
            return messages
        conv = db.conn.execute(
            'SELECT snapshot FROM chat_workspace_state WHERE user_id=? AND cookie_id=? AND cid=?',
            (owner, cookie_id, cid),
        ).fetchone()
        if not conv:
            return messages
        snapshot = json.loads(conv[0])
        peer = str(snapshot.get('otherUserId') or '')
        item_id = str(snapshot.get('itemId') or '')
        owned_accounts = set(db.get_all_cookies(owner))
        query_sides = [(cookie_id, peer, True)]
        if peer in owned_accounts and peer != cookie_id:
            query_sides.append((peer, cookie_id, False))
        rows = []
        for seller_account, buyer_account, seller_view in query_sides:
            rows.extend((row[0], row[1], row[2], row[3], row[4], seller_account,
                         buyer_account, seller_view) for row in db.conn.execute(
                '''SELECT order_id,item_id,chat_id,order_status,created_at FROM orders
                   WHERE cookie_id=? AND buyer_id=?
                   AND (chat_id=? OR chat_id=? OR (item_id=? AND ?<>''))''',
                (seller_account, buyer_account, cid, cid + '@goofish', item_id, item_id),
            ).fetchall())
        interactions = db.conn.execute(
        '''SELECT a.order_id,a.action,a.updated_at,a.cookie_id FROM buyer_interaction_attempts a
               JOIN orders o ON o.order_id=a.order_id AND o.cookie_id=a.cookie_id
           WHERE a.status='sent' AND ((a.cookie_id=? AND o.buyer_id=?) OR (a.cookie_id=? AND o.buyer_id=?)) ''',
        (cookie_id, peer, peer, cookie_id),
        ).fetchall()
    orders = {str(r[0]): r for r in rows}

    def link(message, row, source):
        message.update(event_order_id=str(row[0]), card={'kind': 'order', 'orderId': str(row[0])},
                       event_order_match=source, _linked_seller_id=str(row[5]),
                       _linked_buyer_id=str(row[6]))
        resolve_direction(message, my_id, peer, str(row[5]), str(row[6]))
        message.pop('_linked_seller_id', None)
        message.pop('_linked_buyer_id', None)

    for message in messages:
        event_type = message.get('event_type')
        if not event_type:
            continue
        resolve_direction(message, my_id, peer)
        explicit_id = str(message.get('event_order_id') or '')
        if not explicit_id and (message.get('card') or {}).get('kind') == 'order':
            explicit_id = str(message['card'].get('orderId') or '')
        if explicit_id:
            with db.lock:
                row = db.conn.execute(
                    'SELECT order_id,item_id,chat_id,order_status,created_at,cookie_id,buyer_id FROM orders WHERE order_id=?',
                    (explicit_id,),
                ).fetchone()
            if row and ((str(row[5]), str(row[6])) not in {
                    (str(cookie_id), peer), (peer, str(cookie_id))
                } or str(row[5]) not in owned_accounts):
                # A card reference is not permission to expose another account's order.
                message.update(event_order_id='', card=None, event_order_match='unavailable')
            elif row:
                link(message, row, 'platform_card')
            else:
                # Keep the exact platform reference; opening it will report unsynced, not pick another order.
                message.update(event_order_id=explicit_id, card={'kind': 'order', 'orderId': explicit_id},
                               event_order_match='platform_card_uncached')
            continue

        try:
            stamp = float(message.get('time') or 0)
            stamp = stamp / 1000 if stamp >= 1e11 else stamp
        except (TypeError, ValueError):
            stamp = 0
        if not stamp:
            continue
        eligible = []
        for row in rows:
            created = _order_created_epoch(row[4])
            chat = str(row[2] or '').split('@')[0]
            if created is None or created > stamp:
                continue
            if item_id and str(row[1] or '') != item_id:
                continue
            if chat and chat != cid:
                continue
            if _event_order_state_compatible(event_type, row[3]):
                eligible.append((created, row, chat == cid))
        if not eligible:
            continue
        action = {'review_completed': 'rate', 'receipt_thanks': 'thanks',
                  'flower_requested': 'flower', 'flower_sent_confirmation': 'flower'}.get(event_type)
        allowed = {(str(v[1][0]), str(v[1][5])) for v in eligible}
        receipt_ids = {(str(oid), str(account)) for oid, kind, updated, account in interactions
                       if action and kind == action and (str(oid), str(account)) in allowed and abs(float(updated) - stamp) <= 120}
        if len(receipt_ids) == 1:
            link(message, orders[next(iter(receipt_ids))[0]], 'interaction_receipt')
            continue
        if len(receipt_ids) > 1:
            message['event_order_candidates'] = sorted(oid for oid, _ in receipt_ids)
            continue
        newest = max(v[0] for v in eligible)
        matches = [v[1] for v in eligible if v[0] == newest]
        if len(matches) == 1:
            link(message, matches[0], 'event_time')
        else:
            message['event_order_candidates'] = [str(v[0]) for v in matches]
    return messages


def repair_buyer_nickname(db, user_id, cookie_id, cid, buyer_id, nickname):
    """Repair only invalid cached values; preserve a name learned during an in-flight lookup."""
    with db.lock:
        current = state(db, user_id, cookie_id, cid)
        if not current or str(current['snapshot'].get('otherUserId') or '') != str(buyer_id):
            return
        if _valid_nickname(current['snapshot'].get('otherUserName')):
            return
        current['snapshot']['otherUserName'] = str(nickname).strip()[:120] if _valid_nickname(nickname) else ''
        db.conn.execute(
            'UPDATE chat_workspace_state SET snapshot=? WHERE user_id=? AND cookie_id=? AND cid=?',
            (json.dumps(current['snapshot'], ensure_ascii=False), user_id, cookie_id, cid),
        )
        db.conn.commit()


def state(db, user_id, cookie_id, cid):
    with db.lock:
        row = _row(
            db,
            "SELECT * FROM chat_workspace_state WHERE user_id=? AND cookie_id=? AND cid=?",
            (user_id, cookie_id, cid),
        )
        if not row:
            return None
        snapshot = json.loads(row["snapshot"])
        note = _row(
            db,
            "SELECT note FROM chat_customer_notes WHERE user_id=? AND cookie_id=? AND buyer_id=?",
            (user_id, cookie_id, snapshot.get("otherUserId", "")),
        )
    return dict(
        pinned=bool(row["pinned"]),
        viewed_message_time=row["viewed_time"],
        viewed_message_summary=row["viewed_summary"],
        viewed_unread_count=row["viewed_count"],
        needs_reply=row["last_inbound"] > row["last_outbound"],
        note=note["note"] if note else "",
        snapshot=snapshot,
    )


def observe_conversations(db, user_id, cookie_id, conversations):
    with db.lock:
        for v in conversations:
            previous = _row(
                db,
                "SELECT snapshot FROM chat_workspace_state WHERE user_id=? AND cookie_id=? AND cid=?",
                (user_id, cookie_id, v["cid"]),
            )
            before = json.loads(previous["snapshot"]) if previous else {}
            name = v.get("otherUserName") if _valid_nickname(v.get("otherUserName")) else ""
            if not name and before.get("otherUserId") == v.get("otherUserId") and _valid_nickname(before.get("otherUserName")):
                name = before["otherUserName"]
            if not name:
                known = db.conn.execute(
                    """SELECT buyer_nick FROM orders WHERE cookie_id=? AND buyer_id=?
                    AND (REPLACE(COALESCE(chat_id,''),'@goofish','')=? OR (item_id=? AND ?<>''))
                    AND TRIM(COALESCE(buyer_nick,''))<>''
                    ORDER BY CASE WHEN REPLACE(COALESCE(chat_id,''),'@goofish','')=? THEN 0 ELSE 1 END,
                    datetime(created_at) DESC LIMIT 30""",
                    (cookie_id, v.get("otherUserId"), v["cid"], v.get("itemId") or "", v.get("itemId") or "", v["cid"]),
                ).fetchall()
                name = next((row[0] for row in known if _valid_nickname(row[0])), "")
            if not name:
                name = buyer_profile(db, user_id, cookie_id, v.get("otherUserId"))["nickname"]
            v["otherUserName"] = name or ""
            snapshot = {
                k: v.get(k)
                for k in (
                    "cid",
                    "otherUserId",
                    "otherUserName",
                    "itemId",
                    "itemTitle",
                    "itemImage",
                    "lastMessageTime",
                    "lastMessageSummary",
                    "unreadCount",
                )
            }
            db.conn.execute(
                """INSERT INTO chat_workspace_state(user_id,cookie_id,cid,snapshot) VALUES(?,?,?,?)
                ON CONFLICT(user_id,cookie_id,cid) DO UPDATE SET snapshot=excluded.snapshot""",
                (
                    user_id,
                    cookie_id,
                    v["cid"],
                    json.dumps(snapshot, ensure_ascii=False),
                ),
            )
            sender = v.get("lastMessageSenderId")
            if v.get("lastMessageType") in ("text", "image", "card") and sender:
                column = (
                    "last_inbound"
                    if str(sender) == str(v.get("otherUserId"))
                    else "last_outbound"
                )
                db.conn.execute(
                    f"UPDATE chat_workspace_state SET {column}=MAX({column},?) WHERE user_id=? AND cookie_id=? AND cid=?",
                    (int(v.get("lastMessageTime") or 0), user_id, cookie_id, v["cid"]),
                )
        db.conn.commit()
    for v in conversations:
        s = state(db, user_id, cookie_id, v["cid"]) or {}
        s.pop("snapshot", None)
        v["workspace"] = s


def observe_messages(db, user_id, cookie_id, cid, messages):
    current = state(db, user_id, cookie_id, cid)
    if not current:
        return
    buyer = str(current["snapshot"].get("otherUserId") or "")
    current_name = current["snapshot"].get("otherUserName") or ""
    valid_current_name = _valid_nickname(current_name)
    with db.lock:
        if not valid_current_name:
            replacement_name = next((
                str(m.get("senderName") or "").strip() for m in messages
                if not m.get("isSelf") and str(m.get("senderId") or "") == buyer
                and not m.get("event_type") and m.get("type") in ("text", "image", "card")
                and _valid_nickname(m.get("senderName"))
            ), "")
            if not replacement_name:
                rows = db.conn.execute(
                    """SELECT buyer_nick FROM orders WHERE cookie_id=? AND buyer_id=?
                    AND (REPLACE(COALESCE(chat_id,''),'@goofish','')=? OR (item_id=? AND ?<>''))
                    ORDER BY CASE WHEN REPLACE(COALESCE(chat_id,''),'@goofish','')=? THEN 0 ELSE 1 END,
                    datetime(created_at) DESC LIMIT 30""",
                    (cookie_id, buyer, cid, current["snapshot"].get("itemId") or "", current["snapshot"].get("itemId") or "", cid),
                ).fetchall()
                replacement_name = next((row[0] for row in rows if _valid_nickname(row[0])), "")
            current["snapshot"]["otherUserName"] = replacement_name
            valid_current_name = _valid_nickname(replacement_name)
            db.conn.execute(
                "UPDATE chat_workspace_state SET snapshot=? WHERE user_id=? AND cookie_id=? AND cid=?",
                (json.dumps(current["snapshot"], ensure_ascii=False), user_id, cookie_id, cid),
            )
        for m in messages:
            if m.get("type") not in ("text", "image", "card"):
                continue
            if m.get("event_type"):
                continue
            if not m.get("isSelf") and str(m.get("senderId") or "") != buyer:
                continue
            nickname = str(m.get("senderName") or "").strip()
            if not valid_current_name and not m.get("isSelf") and _valid_nickname(nickname):
                current["snapshot"]["otherUserName"] = nickname[:120]
                valid_current_name = True
                db.conn.execute(
                    "UPDATE chat_workspace_state SET snapshot=? WHERE user_id=? AND cookie_id=? AND cid=?",
                    (json.dumps(current["snapshot"], ensure_ascii=False), user_id, cookie_id, cid),
                )
            column = "last_outbound" if m.get("isSelf") else "last_inbound"
            db.conn.execute(
                f"UPDATE chat_workspace_state SET {column}=MAX({column},?) WHERE user_id=? AND cookie_id=? AND cid=?",
                (int(m.get("time") or 0), user_id, cookie_id, cid),
            )
        db.conn.commit()


def update_state(db, user_id, cookie_id, cid, changes):
    current = state(db, user_id, cookie_id, cid)
    if current is None:
        raise ValueError("会话尚未从当前账号加载，请刷新会话列表")
    with db.lock:
        if "pinned" in changes:
            db.conn.execute(
                "UPDATE chat_workspace_state SET pinned=? WHERE user_id=? AND cookie_id=? AND cid=?",
                (int(bool(changes["pinned"])), user_id, cookie_id, cid),
            )
        if "viewed_message_time" in changes:
            timestamp = int(changes["viewed_message_time"])
            if timestamp >= int(current["viewed_message_time"]):
                db.conn.execute(
                    "UPDATE chat_workspace_state SET viewed_time=?,viewed_summary=?,viewed_count=? WHERE user_id=? AND cookie_id=? AND cid=?",
                    (
                        timestamp,
                        str(changes.get("viewed_message_summary", ""))[:500],
                        max(0, int(changes.get("viewed_unread_count", 0))),
                        user_id,
                        cookie_id,
                        cid,
                    ),
                )
        if "note" in changes:
            buyer = current["snapshot"].get("otherUserId")
            if not buyer:
                raise ValueError("会话没有可确认的买家身份")
            db.conn.execute(
                "INSERT INTO chat_customer_notes VALUES(?,?,?,?) ON CONFLICT(user_id,cookie_id,buyer_id) DO UPDATE SET note=excluded.note",
                (user_id, cookie_id, buyer, str(changes["note"])[:2000]),
            )
        db.conn.commit()
    result = state(db, user_id, cookie_id, cid)
    result.pop("snapshot", None)
    return result


def handover(db, cookie_id, cid):
    with db.lock:
        row = db.conn.execute(
            "SELECT expires_at FROM chat_handovers WHERE cookie_id=? AND cid=?",
            (cookie_id, cid),
        ).fetchone()
    return float(row[0]) if row and float(row[0]) > time.time() else 0.0


def set_handover(db, cookie_id, cid, expires_at):
    with db.lock:
        if not db.conn.execute(
            "SELECT 1 FROM cookies WHERE id=?", (cookie_id,)
        ).fetchone():
            return
        if expires_at > time.time():
            db.conn.execute(
                "INSERT INTO chat_handovers VALUES(?,?,?) ON CONFLICT(cookie_id,cid) DO UPDATE SET expires_at=excluded.expires_at",
                (cookie_id, cid, expires_at),
            )
        else:
            db.conn.execute(
                "DELETE FROM chat_handovers WHERE cookie_id=? AND cid=?",
                (cookie_id, cid),
            )
        db.conn.commit()
    events.publish(cookie_id, "handover", cid)


def peek_send(db, user_id, cookie_id, cid, recipient, request_id, kind, payload):
    with db.lock:
        row = _row(
            db,
            "SELECT * FROM chat_send_attempts WHERE cookie_id=? AND request_id=?",
            (cookie_id, request_id),
        )
    if row:
        if (
            row["user_id"],
            row["cid"],
            row["recipient"],
            row["kind"],
            row["payload_hash"],
        ) != (user_id, cid, recipient, kind, hashlib.sha256(payload).hexdigest()):
            raise ValueError("请求标识已被其他发送内容使用")
        if row["status"] == "sending" and row["created_at"] < time.time() - 75:
            row["status"] = "unknown"
    return row


def claim_send(db, user_id, cookie_id, cid, recipient, request_id, kind, payload):
    digest = hashlib.sha256(payload).hexdigest()
    with db.lock:
        previous = _row(
            db,
            "SELECT * FROM chat_send_attempts WHERE cookie_id=? AND request_id=?",
            (cookie_id, request_id),
        )
        if previous:
            if (
                previous["user_id"],
                previous["cid"],
                previous["recipient"],
                previous["kind"],
                previous["payload_hash"],
            ) != (user_id, cid, recipient, kind, digest):
                raise ValueError("请求标识已被其他发送内容使用")
            if (
                previous["status"] == "sending"
                and previous["created_at"] < time.time() - 75
            ):
                db.conn.execute(
                    "UPDATE chat_send_attempts SET status='unknown',updated_at=? WHERE cookie_id=? AND request_id=? AND status='sending'",
                    (time.time(), cookie_id, request_id),
                )
                db.conn.commit()
                previous["status"] = "unknown"
            return False, previous
        stamp = time.time()
        db.conn.execute(
            "INSERT INTO chat_send_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                user_id,
                cookie_id,
                request_id,
                cid,
                recipient,
                kind,
                digest,
                "sending",
                "",
                stamp,
                stamp,
            ),
        )
        db.conn.commit()
        return True, dict(status="sending", message_id="", created_at=stamp)


def finish_send(db, cookie_id, request_id, status, message_id=""):
    if status not in ("sent", "failed", "unknown"):
        raise ValueError("invalid send state")
    with db.lock:
        db.conn.execute(
            "UPDATE chat_send_attempts SET status=?,message_id=?,updated_at=? WHERE cookie_id=? AND request_id=?",
            (status, message_id, time.time(), cookie_id, request_id),
        )
        db.conn.commit()
    events.publish(cookie_id, "outgoing")


def outgoing(db, user_id, cookie_id, cid):
    with db.lock:
        cursor = db.conn.execute(
            "SELECT request_id,kind,status,message_id,created_at FROM chat_send_attempts WHERE user_id=? AND cookie_id=? AND cid=? ORDER BY created_at DESC LIMIT 20",
            (user_id, cookie_id, cid),
        )
        rows = [
            dict(zip([c[0] for c in cursor.description], row))
            for row in cursor.fetchall()
        ]
    for row in rows:
        if row["status"] == "sending" and row["created_at"] < time.time() - 75:
            row["status"] = "unknown"
    return rows


class EventHub:
    def __init__(self):
        self.lock = threading.Lock()
        self.subscribers = {}

    @contextmanager
    def subscribe(self, cookie_id):
        queue = asyncio.Queue(maxsize=100)
        token = object()
        loop = asyncio.get_running_loop()
        with self.lock:
            self.subscribers[token] = (str(cookie_id), loop, queue)
        try:
            yield queue
        finally:
            with self.lock:
                self.subscribers.pop(token, None)

    def publish(self, cookie_id, kind="message", cid=""):
        with self.lock:
            targets = [v for v in self.subscribers.values() if v[0] == str(cookie_id)]

        def enqueue(queue):
            event = {"type": kind, "cid": str(cid), "at": time.time()}
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                event = {"type": "reset", "at": time.time()}
            queue.put_nowait(event)

        for _, loop, queue in targets:
            if not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(enqueue, queue)
                except RuntimeError:
                    pass


events = EventHub()


def valid_avatar(value):
    from urllib.parse import urlsplit
    url=str(value or '').strip()
    if url.startswith('//'):url='https:'+url
    if url.startswith('http://'):url='https://'+url[7:]
    try:
        parts=urlsplit(url)
        if (parts.scheme!='https' or parts.username or parts.password or len(url)>2048 or
            not any((parts.hostname or '').endswith('.'+d) or parts.hostname==d
                    for d in ('alicdn.com','taobaocdn.com','goofish.com','tbcdn.cn'))):return ''
    except ValueError:return ''
    return url
