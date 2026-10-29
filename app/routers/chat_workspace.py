"""Local operator workspace API; all platform operations use the account loop."""

import asyncio
import io
import json
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from app.services import chat_workspace as store


class StateUpdate(BaseModel):
    pinned: Optional[bool] = None
    viewed_message_time: Optional[int] = Field(None, ge=0)
    viewed_message_summary: Optional[str] = Field(None, max_length=500)
    viewed_unread_count: Optional[int] = Field(None, ge=0)
    note: Optional[str] = Field(None, max_length=2000)


class HandoverUpdate(BaseModel):
    action: str = Field(..., pattern="^(pause|resume)$")


class ForwardRequest(BaseModel):
    source_cid: str = Field(..., min_length=1, max_length=200)
    message_id: str = Field(..., min_length=1, max_length=200)
    target_cid: str = Field(..., min_length=1, max_length=200)
    client_request_id: str = Field(..., min_length=8, max_length=120)


def conversation(db, user_id, cookie_id, cid, recipient=None):
    s = store.state(db, user_id, cookie_id, cid)
    if s is None:
        raise HTTPException(404, "会话尚未从当前账号加载，请刷新会话列表")
    if recipient and str(s["snapshot"].get("otherUserId")) != str(recipient):
        raise HTTPException(403, "会话与接收者不匹配")
    return s


def response(row):
    return {
        "success": row["status"] == "sent",
        "message": {
            "sent": "消息已发送",
            "sending": "发送处理中",
            "unknown": "发送结果待核查，请查看聊天历史，勿重复发送",
            "failed": "发送失败",
        }[row["status"]],
        "data": {"status": row["status"], "messageId": row.get("message_id", "")},
    }


async def buyer_identity(db, uid, cookie_id, buyer, snapshot, run_account, cid="", item_id="", matched_orders=None):
    """Use known participant data before one bounded public-profile request."""
    from app.services.chat_workspace import _valid_nickname

    nickname = str(snapshot.get("otherUserName") or "").strip()
    if _valid_nickname(nickname):
        return nickname, "conversation"
    for order in matched_orders or []:
        candidate = str(order.get("buyer_nick") or "").strip()
        if _valid_nickname(candidate):
            return candidate, "order"
    with db.lock:
        known = db.conn.execute(
            """SELECT buyer_nick FROM orders WHERE cookie_id=? AND buyer_id=?
            AND (REPLACE(COALESCE(chat_id,''),'@goofish','')=? OR (item_id=? AND ?<>''))
            AND TRIM(COALESCE(buyer_nick,''))<>''
            ORDER BY CASE WHEN REPLACE(COALESCE(chat_id,''),'@goofish','')=? THEN 0 ELSE 1 END,
            datetime(created_at) DESC LIMIT 30""",
            (cookie_id, buyer, cid, item_id, item_id, cid),
        ).fetchall()
    order_nickname = next((row[0] for row in known if _valid_nickname(row[0])), "")
    if order_nickname:
        return order_nickname, "order"
    cached = store.buyer_profile(db, uid, cookie_id, buyer)
    if cached["nickname"]:
        return cached["nickname"], "public_profile_cache"
    from app.services.auth_recovery import auth_paused
    from utils.risk_control import registry

    if (not buyer or not db.get_cookie_status(cookie_id) or auth_paused(cookie_id)
            or registry.get(cookie_id).is_blocked):
        return "", "unavailable"
    if not store.claim_buyer_profile_fetch(db, uid, cookie_id, buyer):
        return "", "unavailable"
    nickname = ""
    try:
        async def read(instance):
            from utils.xianyu_seller_api import XianyuSellerAPI, parse_user_profile

            original = instance.cookies_str
            async with XianyuSellerAPI(cookie_id, original) as api:
                profile = parse_user_profile(await api.get_user_profile(buyer))
                # A concurrent login must not be overwritten by this older request.
                if instance.cookies_str == original and api.cookies_str != original:
                    from utils.xianyu_utils import trans_cookies
                    instance.cookies_str = api.cookies_str
                    instance.cookies = trans_cookies(api.cookies_str)
                return profile.get("nickname") or ""

        nickname = str(await asyncio.wait_for(run_account(cookie_id, read), timeout=8)).strip()
        if not store._valid_nickname(nickname):
            nickname = ""
    except asyncio.CancelledError:
        raise
    except Exception:
        # A failed profile lookup must not hide the existing conversation/order context.
        pass
    finally:
        store.finish_buyer_profile_fetch(db, uid, cookie_id, buyer, nickname)
    return nickname, "public_profile" if nickname else "unavailable"


async def send_once(
    db,
    user_id,
    cookie_id,
    cid,
    recipient,
    request_id,
    kind,
    payload,
    run_account,
    operation,
):
    try:
        previous = store.peek_send(
            db, user_id, cookie_id, cid, recipient, request_id, kind, payload
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    if previous:
        return response(previous)
    from app.services.auth_recovery import auth_paused
    from utils.risk_control import registry

    if (
        auth_paused(cookie_id)
        or registry.get(cookie_id).is_blocked
        or not db.get_cookie_status(cookie_id)
    ):
        raise HTTPException(
            409,
            "账号认证暂停或已停用，请先检查账号状态",
            headers={"X-Chat-Send-Status": "not_sent"},
        )
    try:
        claimed, row = store.claim_send(
            db, user_id, cookie_id, cid, recipient, request_id, kind, payload
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    if not claimed:
        return response(row)
    status = "unknown"
    message_id = ""
    try:

        async def run(instance):
            from XianyuAutoAsync import pause_manager
            from app.services.auth_recovery import auth_paused
            from utils.risk_control import registry

            if auth_paused(cookie_id) or registry.get(cookie_id).is_blocked:
                raise HTTPException(409, "账号认证请求已暂停，请先在账号管理完成验证")
            if not db.get_cookie_status(cookie_id):
                raise HTTPException(409, "账号已停用")
            pause_manager.pause_chat(cid, cookie_id)
            return await operation(instance)

        result = await run_account(cookie_id, run)
        body = result.get("body", {}) if isinstance(result, dict) else {}
        code = result.get("code") if isinstance(result, dict) else None
        if code is not None and str(code) != "200":
            raise RuntimeError("消息服务未确认成功")
        if isinstance(body, dict) and (body.get("reason") or body.get("code")):
            raise RuntimeError("消息服务拒绝发送")
        message_id = (
            str(body.get("messageId") or body.get("msgId") or "")
            if isinstance(body, dict)
            else ""
        )
        if not isinstance(result, dict) or (str(code) != "200" and not message_id):
            raise RuntimeError("缺少消息服务成功回执")
        status = "sent"
    except asyncio.CancelledError:
        raise
    except Exception:
        # Transport errors can occur after the frame reached the platform. Never auto-repeat.
        status = "unknown"
    finally:
        store.finish_send(db, cookie_id, request_id, status, message_id)
    return response({"status": status, "message_id": message_id})


def create_chat_workspace_router(
    db, get_user, owned_account, run_account, invalidate_account=None
):
    router = APIRouter()

    def owned(cookie_id, user):
        owned_account(cookie_id, user)
        return user["user_id"]

    @router.get("/chat/context/{cookie_id}/{cid}")
    async def context(
        cookie_id: str, cid: str, order_id: Optional[str] = None, user=Depends(get_user)
    ):
        uid = owned(cookie_id, user)
        s = conversation(db, uid, cookie_id, cid)
        snap = s.pop("snapshot")
        buyer = str(snap.get("otherUserId") or "")
        exact = []
        candidates = []
        with db.lock:
            cur = db.conn.execute(
                "SELECT order_id FROM orders WHERE cookie_id=? AND buyer_id=? AND ((chat_id=? OR chat_id=? || '@goofish') OR (item_id=? AND ?<>'')) ORDER BY created_at DESC LIMIT 100",
                (
                    cookie_id,
                    buyer,
                    cid,
                    cid,
                    snap.get("itemId") or "",
                    snap.get("itemId") or "",
                ),
            )
            ids = [r[0] for r in cur.fetchall()]
        if order_id and order_id not in ids:
            requested = db.get_order_by_id(order_id)
            if not requested:
                raise HTTPException(404, "卡片订单尚未同步，请先在订单管理同步")
            seller_account = str(requested.get("cookie_id") or "")
            same_account = seller_account == cookie_id and str(requested.get("buyer_id")) == buyer
            reverse_owned_accounts = set(db.get_all_cookies(uid))
            buyer_view_of_owned_seller = (
                seller_account == buyer and str(requested.get("buyer_id")) == cookie_id
                and seller_account in reverse_owned_accounts
            )
            chat_match = str(requested.get("chat_id") or "").split("@", 1)[0] == cid
            item_match = bool(snap.get("itemId") and str(requested.get("item_id") or "") == str(snap.get("itemId")))
            if not (same_account or buyer_view_of_owned_seller) or not (chat_match or item_match):
                raise HTTPException(403, "卡片订单与当前会话买家、商品或账号不匹配")
            ids.append(order_id)
        from app.services.delivery_ledger import list_pending

        pending = {(v["cookie_id"], v["order_id"]): v for v in list_pending(db, uid)}
        for oid in ids:
            order = db.get_order_by_id(oid)
            if not order:
                continue
            order_cookie_id = str(order.get("cookie_id") or "")
            if order_cookie_id != cookie_id:
                if (order_cookie_id != buyer or str(order.get("buyer_id")) != cookie_id
                        or order_cookie_id not in set(db.get_all_cookies(uid))):
                    continue
            item_details = (
                db.get_item_info(order_cookie_id, order.get("item_id"))
                if order.get("item_id")
                else None
            )
            if item_details:
                for name in ("item_title", "item_image", "item_price"):
                    order[name] = item_details.get(name) or order.get(name, "")
            order["status"] = order.get("order_status", "unknown")
            order["id"] = oid
            with db.lock:
                order["interaction_attempts"] = [
                    {"action": r[0], "status": r[1]}
                    for r in db.conn.execute(
                        "SELECT action,status FROM buyer_interaction_attempts WHERE cookie_id=? AND order_id=?",
                        (order_cookie_id, oid),
                    )
                ]
                attempt = db.conn.execute(
                    "SELECT status FROM delivery_attempts WHERE cookie_id=? AND order_id=?",
                    (order_cookie_id, oid),
                ).fetchone()
            if attempt:
                order["delivery_attempt"] = pending.get(
                    (order_cookie_id, oid),
                    {"status": attempt[0], "reviewable": False, "reserved_count": 0},
                )
            (
                exact
                if str(order.get("chat_id") or "").split("@")[0] == cid
                else candidates
            ).append(order)
        item = (
            db.get_item_info(cookie_id, snap.get("itemId"))
            if snap.get("itemId")
            else {}
        )
        nickname, nickname_source = await buyer_identity(
            db, uid, cookie_id, buyer, snap, run_account, cid,
            snap.get("itemId") or "", [*exact, *candidates]
        )
        store.repair_buyer_nickname(db, uid, cookie_id, cid, buyer, nickname)
        expires = store.handover(db, cookie_id, cid)
        return {
            "success": True,
            "data": {
                "owner_id": uid,
                "buyer": {
                    "id": buyer,
                    "nickname": nickname,
                    "nickname_source": nickname_source,
                    "note": s.get("note", ""),
                },
                "item": {
                    k: (item or {}).get(k)
                    for k in ("item_id", "item_title", "item_image", "item_price")
                },
                "orders": exact,
                "candidate_orders": candidates,
                "handover": {
                    "paused": bool(expires),
                    "expires_at": expires,
                    "duration_minutes": db.get_cookie_pause_duration(cookie_id),
                },
                "features": {
                    **db.get_buyer_interaction_settings(cookie_id),
                    "auto_rate_template": db.get_system_setting("auto_rate_content")
                    or "感谢惠顾，欢迎下次光临！",
                },
                "workspace": s,
                "outgoing": store.outgoing(db, uid, cookie_id, cid),
                "source": "official_cache",
            },
        }

    @router.patch("/chat/state/{cookie_id}/{cid}")
    async def update(
        cookie_id: str, cid: str, body: StateUpdate, user=Depends(get_user)
    ):
        uid = owned(cookie_id, user)
        conversation(db, uid, cookie_id, cid)
        try:
            data = store.update_state(
                db, uid, cookie_id, cid, body.model_dump(exclude_none=True)
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        store.events.publish(cookie_id, "state", cid)
        return {"success": True, "data": data}

    @router.post("/chat/handover/{cookie_id}/{cid}")
    async def change_handover(
        cookie_id: str, cid: str, body: HandoverUpdate, user=Depends(get_user)
    ):
        uid = owned(cookie_id, user)
        conversation(db, uid, cookie_id, cid)
        duration = db.get_cookie_pause_duration(cookie_id)
        if body.action == "pause" and not duration:
            raise HTTPException(409, "此账号暂停时长为0，请先在账号设置调整")
        from XianyuAutoAsync import pause_manager

        # Persisted state is authoritative; shared manager uses account + conversation keys.
        if body.action == "pause":
            pause_manager.pause_chat(cid, cookie_id)
        else:
            pause_manager.resume_chat(cid, cookie_id)
        return {
            "success": True,
            "data": {"expires_at": store.handover(db, cookie_id, cid)},
        }

    @router.post("/chat/send-image/{cookie_id}")
    async def send_image(
        cookie_id: str,
        cid: str = Form(..., max_length=200),
        to_user_id: str = Form(..., max_length=100),
        client_request_id: str = Form(..., min_length=8, max_length=120),
        image: UploadFile = File(...),
        reply_to_message_id: Optional[str] = Form(None, max_length=200),
        user=Depends(get_user),
    ):
        uid = owned(cookie_id, user)
        conversation(db, uid, cookie_id, cid, to_user_id)
        from app.services.message_actions import resolve_source, send_reply
        if reply_to_message_id:
            raise HTTPException(400,'尚未验证原生图片回复；请取消引用后发送图片',headers={'X-Chat-Send-Status':'not_sent'})
        source = await run_account(cookie_id,lambda instance:resolve_source(instance,cid,reply_to_message_id,to_user_id)) if reply_to_message_id else None
        if image.content_type not in (
            "image/jpeg",
            "image/png",
            "image/gif",
            "image/webp",
        ):
            raise HTTPException(415, "不支持的图片格式")
        raw = await image.read(5 * 1024 * 1024 + 1)
        if not raw or len(raw) > 5 * 1024 * 1024:
            raise HTTPException(413, "图片不得超过5MB")
        from PIL import Image

        def prepare():
            with Image.open(io.BytesIO(raw)) as img:
                if (
                    img.format not in ("JPEG", "PNG", "GIF", "WEBP")
                    or img.width * img.height > 25000000
                ):
                    raise ValueError("图片格式或尺寸不受支持")
                img.seek(0)
                img.load()
                clean = img.convert("RGB")
                output = io.BytesIO()
                clean.save(output, format="JPEG", quality=90)
                return output.getvalue(), clean.width, clean.height

        try:
            clean, width, height = await asyncio.to_thread(prepare)
        except Exception:
            raise HTTPException(400, "图片无法解析或尺寸过大") from None

        async def operation(instance):
            from utils.image_uploader import ImageUploader

            with tempfile.TemporaryDirectory(prefix="bygoofish-chat-image-") as folder:
                path = Path(folder) / "attachment.jpg"
                path.write_bytes(clean)
                path.chmod(0o600)
                async with ImageUploader(instance.cookies_str) as uploader:
                    url = await uploader.upload_image(str(path))
                if not url:
                    raise RuntimeError("图片上传未成功")
                if source:
                    return await send_reply(instance,cid,to_user_id,source,image={"url":url,"width":width,"height":height,"type":0})
                return await instance.send_im_image(cid, to_user_id, url, width, height)

        return await send_once(
            db,
            uid,
            cookie_id,
            cid,
            to_user_id,
            client_request_id,
            "image",
            raw + (reply_to_message_id or "").encode(),
            run_account,
            operation,
        )

    @router.get('/chat/capabilities/{cookie_id}')
    async def native_capabilities(cookie_id: str, user=Depends(get_user)):
        owned(cookie_id,user)
        from app.services.native_capabilities import capabilities
        return capabilities(db)

    @router.get('/chat/emojis/{cookie_id}')
    async def emoji_directory(cookie_id: str, user=Depends(get_user)):
        owned(cookie_id,user)
        from app.services.chat_emojis import cached, refresh
        items,updated,retry=cached(db,cookie_id)
        if time.time()<max(updated+86400,retry):
            return {'items':items,'stale':time.time()>=updated+86400}
        try:
            return await run_account(cookie_id,lambda instance:refresh(instance,db))
        except Exception:
            return {'items':items,'stale':True}

    @router.post('/chat/forward/{cookie_id}')
    async def forward_message(cookie_id: str, body: ForwardRequest, user=Depends(get_user)):
        uid=owned(cookie_id,user)
        source_state=conversation(db,uid,cookie_id,body.source_cid)
        target_state=conversation(db,uid,cookie_id,body.target_cid)
        recipient=str(target_state['snapshot'].get('otherUserId') or '')
        if not recipient:raise HTTPException(400,'转发目标缺少参与者信息')
        from app.services.message_actions import resolve_source, forward, validate_forward_reference
        source=await run_account(cookie_id,lambda instance:resolve_source(instance,body.source_cid,body.message_id,str(source_state['snapshot'].get('otherUserId') or ''),allow_quote=True))
        await run_account(cookie_id,lambda instance:validate_forward_reference(instance,body.target_cid,recipient,source))
        from app.services.native_capabilities import require
        require(db,uid,recipient,'forward_image' if source[1]['type']=='image' else 'forward_text')
        result=await send_once(db,uid,cookie_id,body.target_cid,recipient,body.client_request_id,source[1]['type'],
            json.dumps(['forward',body.source_cid,body.message_id,body.target_cid]).encode(),run_account,
            lambda instance:forward(instance,body.target_cid,recipient,source,db=db,client_request_id=body.client_request_id))
        if invalidate_account:invalidate_account(cookie_id)
        store.events.publish(cookie_id,'outgoing',body.target_cid)
        return result

    @router.get("/chat/events/{cookie_id}")
    async def event_stream(cookie_id: str, request: Request, user=Depends(get_user)):
        uid = owned(cookie_id, user)
        token = request.headers.get("authorization", "").removeprefix("Bearer ")

        async def stream():
            from app.auth import SESSION_TOKENS

            with store.events.subscribe(cookie_id) as queue:
                yield "data: " + json.dumps({"type": "ready"}) + "\n\n"
                while not await request.is_disconnected():
                    # Logout, expiry and account transfer also terminate an already-open stream.
                    session = SESSION_TOKENS.get(token)
                    if not session or session.get("user_id") != uid:
                        break
                    if cookie_id not in db.get_all_cookies(uid):
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        event = {"type": "heartbeat"}
                    if invalidate_account and event.get("type") in (
                        "message",
                        "outgoing",
                        "reset",
                    ):
                        invalidate_account(cookie_id)
                    yield "data: " + json.dumps(event, separators=(",", ":")) + "\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
