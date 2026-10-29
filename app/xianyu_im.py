import base64
import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit
from app.chat_events import event_metadata, resolve_direction, transaction_event, valid_nickname as is_valid_participant_nickname


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}
    return {}


def _strip_domain(value: Any) -> str:
    return str(value or "").split("@", 1)[0]


# 闲鱼的会话接口不返回买家头像（avatar / avatarUrl / senderAvatar 都是空）。
# 曾经用 DiceBear 按用户 ID 生成占位头像，但其官方 API 在国内基本连不通，
# 浏览器反复加载失败后全部回落成同一个灰块。现在不再生成外部地址，
# 占位头像由前端按用户 ID 本地哈希出颜色 + 首字母，零网络依赖。


def _load_content(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except (TypeError, json.JSONDecodeError):
        pass
    try:
        parsed = json.loads(base64.b64decode(raw).decode("utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _interpret_content(decoded: Dict[str, Any]) -> Tuple[str, List[str], str]:
    content_type = decoded.get("contentType")
    text_value = decoded.get("text")
    if content_type == 1 or text_value is not None:
        if isinstance(text_value, dict):
            return str(text_value.get("text", "")), [], "text"
        return str(text_value or ""), [], "text"

    image = decoded.get("image")
    if content_type == 2 or isinstance(image, dict):
        pics = image.get("pics", []) if isinstance(image, dict) else []
        images = [
            str(pic.get("url"))
            for pic in pics
            if isinstance(pic, dict) and pic.get("url")
        ]
        legacy_url = decoded.get("picUrl")
        if legacy_url and not images:
            images.append(str(legacy_url))
        return "", images, "image"

    if content_type == 3 or decoded.get("audio"):
        return "[语音消息]", [], "system"

    if decoded.get("dxCard"):
        main = _as_dict(_as_dict(_as_dict(decoded.get("dxCard")).get("item")).get("main"))
        extra = _as_dict(main.get("exContent"))
        for candidate in (extra.get("title"), extra.get("desc"), main.get("title")):
            if isinstance(candidate, str) and candidate:
                return candidate[:500], [], "card"
    if decoded.get("title") or decoded.get("template"):
        title=decoded.get('title')
        template=_as_dict(decoded.get('template'))
        if not isinstance(title,str):title=template.get('title') or template.get('subTitle')
        return (title[:200] if isinstance(title,str) else '[卡片消息]'), [], 'card'
    return "", [], ""


def _find_order_id(node, depth=0):
    """Find an order id in structured fields or nested platform jump URLs."""
    if depth > 9:
        return ""
    if isinstance(node, dict):
        for key in ("orderId", "order_id", "bizOrderId"):
            value = str(node.get(key) or "")
            if re.fullmatch(r"\d{10,}", value):
                return value
        for value in node.values():
            if isinstance(value, str) and ("order" in value.lower() or "bizOrder" in value):
                try:
                    parsed = urlsplit(value)
                    query = parse_qs(parsed.query)
                    names = ["orderId", "bizOrderId"]
                    if "order" in (parsed.netloc + parsed.path).lower():
                        names.append("id")
                    for name in names:
                        candidate = (query.get(name) or [""])[0]
                        if re.fullmatch(r"\d{10,}", candidate):
                            return candidate
                except ValueError:
                    pass
            found = _find_order_id(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_order_id(value, depth + 1)
            if found:
                return found
    elif isinstance(node, str) and node.startswith("{"):
        try:
            return _find_order_id(json.loads(node), depth + 1)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ""
    return ""


def parse_card(decoded):
    def find(node,depth=0):
        if not isinstance(node,dict) or depth>4:return None
        for key,kind in (('orderId','order'),('order_id','order'),('itemId','item'),('item_id','item')):
            value=str(node.get(key) or '')
            if re.fullmatch(r'[A-Za-z0-9_-]{1,80}',value):return {'kind':kind,'orderId' if kind=='order' else 'itemId':value}
        for key in ('data','card','order','item','content','template'):
            found=find(_as_dict(node.get(key)),depth+1)
            if found:return found
        return None
    order_id = _find_order_id(decoded)
    if order_id:
        return {"kind": "order", "orderId": order_id}
    return find(decoded)


def extract_message_summary(message: Dict[str, Any]) -> str:
    content = _as_dict(message.get("content"))
    custom = _as_dict(content.get("custom"))
    summary = custom.get("summary") or custom.get("degrade")
    if summary:
        return str(summary)[:80]
    decoded = _load_content(custom.get("data"))
    if decoded:
        text, images, msg_type = _interpret_content(decoded)
        if text:
            return text[:80]
        if images or msg_type == "image":
            return "[图片]"
    if content.get('contentType') in (1,2,10):
        from app.services.message_actions import native_display
        inner = _as_dict(content.get('replyContent')).get('replyContent') if content.get('contentType')==10 else content
        view = native_display(inner)
        if view:return str(view['text'] or ('[图片]' if view['images'] else ''))[:80]
    return ""


def parse_conversation(raw: Dict[str, Any], my_id: str) -> Optional[Dict[str, Any]]:
    try:
        conversation = _as_dict(raw.get("singleChatConversation"))
        raw_cid = str(conversation.get("cid") or "")
        cid = _strip_domain(raw_cid)
        if not cid:
            return None

        first = _strip_domain(conversation.get("pairFirst"))
        second = _strip_domain(conversation.get("pairSecond"))
        if not my_id or str(my_id) not in (first, second) or first == second:
            return None
        other_id = second if first == str(my_id) else first
        if not other_id or other_id == "0":
            return None

        extension = _as_dict(conversation.get("extension"))
        last_message_wrapper = _as_dict(raw.get("lastMessage"))
        last_message = _as_dict(last_message_wrapper.get("message"))
        last_extension = _as_dict(last_message.get("extension"))
        parsed_last = parse_message(last_message_wrapper, my_id)
        sender_id = (parsed_last or {}).get("senderId") or _strip_domain(last_extension.get("senderUserId"))
        sender_name = ""
        if sender_id == other_id:
            candidates = [last_extension.get("senderNick"), last_extension.get("senderNickName"),
                          last_message_wrapper.get("senderNick")]
            if not (parsed_last or {}).get("event_type") and not _as_dict(last_extension.get("bizTag")).get("taskName"):
                candidates.append(last_extension.get("reminderTitle"))
            sender_name = next((str(v).strip() for v in candidates if is_valid_participant_nickname(v)), "")

        last_type=(parsed_last or {}).get('type','') if sender_id in (other_id,str(my_id)) else 'system'
        return {
            "cid": cid,
            "rawCid": raw_cid,
            "otherUserId": other_id,
            "otherUserName": sender_name,
            "otherUserAvatar": str(
                extension.get("avatar")
                or extension.get("avatarUrl")
                or (last_extension.get("senderAvatar") if sender_id == other_id else "")
                or ""
            ),
            "itemId": str(extension.get("itemId") or ""),
            "itemTitle": str(extension.get("itemTitle") or ""),
            "itemImage": str(
                extension.get("itemPic")
                or extension.get("itemImage")
                or extension.get("picUrl")
                or ""
            ),
            "lastMessageSummary": extract_message_summary(last_message),
            "lastMessageSenderId": sender_id,
            "lastMessageType": last_type,
            "lastMessageTime": int(raw.get("modifyTime") or 0),
            "unreadCount": int(raw.get("redPoint") or 0),
        }
    except (TypeError, ValueError):
        return None


# 订单状态提醒（[卖家已发货]、[我已拍下，待付款] 等）在协议里同样是 contentType=1
# 纯文本，与真人聊天无法按发送方区分，只能按词面启发式归类：含订单动作字眼
# 的事件句、平台通用占位词保留系统类型；已识别交易事件另带方向和动作信息。[流泪] 这类表情短词和未知
# 方括号文本仍走普通气泡——新文案漏判只是回到气泡样式，误伤表情才是笑话
_ORDER_EVENT_HINTS = (
    "已", "记得", "请", "交易", "付款", "发货", "收货", "评价", "退款", "取消", "关闭", "成功",
)
_SYSTEM_PLACEHOLDERS = {"卡片消息", "系统消息", "语音消息", "图片", "视频", "位置", "链接"}

def _is_order_placeholder(text: str) -> bool:
    if not re.fullmatch(r"\[[^\[\]]{1,16}\]", text):
        return False
    inner = text[1:-1]
    return inner in _SYSTEM_PLACEHOLDERS or any(hint in inner for hint in _ORDER_EVENT_HINTS)


def parse_message(model: Dict[str, Any], my_id: str) -> Optional[Dict[str, Any]]:
    try:
        message = _as_dict(model.get("message"))
        extension = _as_dict(message.get("extension"))
        sender = message.get("sender")
        sender_id = _strip_domain(extension.get("senderUserId") or (sender.get("uid") if isinstance(sender,dict) else sender))
        content = _as_dict(message.get("content"))
        custom = _as_dict(content.get("custom"))
        decoded = _load_content(custom.get("data"))

        text = ""
        images: List[str] = []
        message_type = "system"
        if decoded:
            text, images, message_type = _interpret_content(decoded)
        quote = None
        if content.get('contentType') == 10:
            from app.services.message_actions import native_display, reference_view
            quote = reference_view(content)
            view = native_display(_as_dict(content.get('replyContent')).get('replyContent'))
            if view:
                text, images, message_type = view['text'], view['images'], view['type']
        elif content.get('contentType') in (1,2) and not decoded:
            from app.services.message_actions import native_display
            view = native_display(content)
            if view:
                text, images, message_type = view['text'], view['images'], view['type']
        app_reply_id = str(extension.get('replyMessageId') or _as_dict(extension.get('extJson')).get('replyMessageId') or '')
        if app_reply_id and len(app_reply_id)<=200 and not quote:
            quote = {'messageId':app_reply_id,'senderId':'','senderName':'','time':0,'text':'原消息未加载','images':[],'type':'unknown'}
        if not text and not images:
            text = str(custom.get("summary") or custom.get("degrade") or "[系统消息]")
        if not message_type:
            message_type = "image" if images else "text"
        if not sender_id or sender_id=="0":message_type="system"
        if message_type == "text" and _is_order_placeholder(text):
            message_type = "system"

        event_meta = event_metadata(text)
        if message_type == "card":
            for candidate in (extension.get("reminderContent"), custom.get("summary"), custom.get("degrade")):
                candidate_meta = event_metadata(candidate)
                # A self-only confirmation card can contain the buyer-facing request as its body.
                if candidate_meta and (not event_meta or
                        (event_meta.get("event_source") == "message" and candidate_meta.get("event_source") == "platform")):
                    text = str(candidate)
                    event_meta = candidate_meta
                    break
        event_type = event_meta.get("event_type")
        if event_type and event_meta.get("event_source") == "platform":
            message_type = "system"
        sender_name = ""
        if message_type != "system" and not event_type:
            for candidate in (
                extension.get("senderNick"),
                extension.get("senderNickName"),
                extension.get("senderName"),
                extension.get("reminderTitle"),
            ):
                if is_valid_participant_nickname(candidate):
                    sender_name = str(candidate).strip()
                    break
        receiver = extension.get("receiverUserId") or extension.get("receiverId") or extension.get("receiver") or ""
        if isinstance(receiver, dict):
            receiver = receiver.get("userId") or receiver.get("id") or ""
        if isinstance(receiver, list):
            receiver = receiver[0] if receiver else ""
        if isinstance(receiver, dict):
            receiver = receiver.get("userId") or receiver.get("id") or ""
        order_id = _find_order_id(decoded)
        card = parse_card(decoded) if message_type == "card" or event_type else None
        if card and order_id:
            card = {"kind": "order", "orderId": order_id}
        result = {
            "messageId": str(message.get("messageId") or ""),
            "reply": quote,
            "canForward": bool(message.get("messageId") and not event_type and message_type in ("text","image") and content.get("contentType") in (1,2,101,10)),
            "canReference": bool(message.get("messageId") and not quote and not event_type and message_type in ("text","image") and content.get("contentType") in (1,2,101)),
            "card": card,
            "senderId": sender_id,
            "senderName": sender_name,
            "receiverId": _strip_domain(receiver),
            "isSelf": sender_id == str(my_id),
            "type": message_type,
            "event_type": event_type,
            **event_meta,
            "event_order_id": order_id,
            "text": text,
            "images": images,
            "time": int(message.get("createAt") or message.get("time") or 0),
        }
        if event_type:
            resolve_direction(result, str(my_id))
        return result
    except (TypeError, ValueError):
        return None


def hydrate_replies(messages):
    """Resolve native App references only by exact message ID in the same loaded thread."""
    lookup = {message['messageId']:message for message in messages if message.get('messageId')}
    for message in messages:
        quote = message.get('reply')
        original = lookup.get((quote or {}).get('messageId'))
        if not quote or not original or original is message:
            continue
        message['reply'] = {key:original.get(key) for key in ('messageId','senderId','senderName','time','text','images','type')}
    return messages
