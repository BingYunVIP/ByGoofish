"""Known transaction wording and display identity; no network or business actions."""
import re

# action, actor (business role or message sender), whether this is a platform notice.
_RULES = {
    '我已拍下，待付款': ('order_pending_payment', 'buyer', True),
    '我已付款，等待你发货': ('order_paid_pending_ship', 'buyer', True),
    '已付款，待发货': ('order_paid_pending_ship', 'buyer', True),
    '你已发货': ('seller_shipped', 'seller', True),
    '卖家已发货': ('seller_shipped', 'seller', True),
    '已发货': ('seller_shipped', 'seller', True),
    '记得及时发货': ('shipping_reminder', 'platform', True),
    '买家确认收货，交易成功': ('buyer_received', 'buyer', True),
    '你已确认收货，交易成功': ('buyer_received', 'buyer', True),
    '快给ta一个评价吧': ('review_requested', 'platform', True),
    '我完成了评价': ('review_completed', 'sender', True),
    '对方完成了评价': ('review_completed', 'peer', True),
    '亲，感谢支持！有任何问题随时找我': ('receipt_thanks', 'sender', False),
    '可以送我闲鱼小红花吗': ('flower_requested', 'sender', False),
    '可以送ta闲鱼小红花吗': ('flower_requested', 'platform', True),
    '卖家人不错？送ta闲鱼小红花': ('flower_requested', 'platform', True),
    '已求买家送我闲鱼小红花': ('flower_sent_confirmation', 'seller', True),
    '你人真不错，送你闲鱼小红花': ('flower_received', 'sender', True),
    '我发起了退款申请': ('refund_requested', 'sender', True),
    '我发起退款申请': ('refund_requested', 'sender', True),
    '我申请了退款': ('refund_requested', 'sender', True),
    '申请退款': ('refund_requested', 'sender', True),
    '买家申请退款': ('refund_requested', 'buyer', True),
    '退款成功，钱款已原路退返': ('refund_completed', 'platform', True),
    '你关闭了订单，钱款已原路退返': ('order_closed', 'platform', True),
    '交易关闭': ('order_closed', 'platform', True),
    '订单关闭': ('order_closed', 'platform', True),
}
_PLACEHOLDERS = {'卡片消息', '系统消息', '语音消息', '图片', '视频', '位置', '链接',
                 '未知用户', '发来一条消息', '发来一条新消息', '闲鱼用户', '闲鱼通知'}


def normalized_phrase(value):
    text = str(value or '').strip().rstrip('~～!！?？。 ')
    if text.startswith('[') and text.endswith(']'):
        text = text[1:-1].strip()
    return text.replace('Ta', 'ta').replace('TA', 'ta')


def transaction_event(text):
    rule = _RULES.get(normalized_phrase(text))
    return rule[0] if rule else None


def valid_nickname(value):
    text = str(value or '').strip()
    normalized = normalized_phrase(text)
    if not text or text.isdigit() or normalized in _RULES or normalized in _PLACEHOLDERS:
        return False
    # Legacy caches may contain bracketed platform status strings not in our event list.
    return not (re.fullmatch(r'\[[^\[\]]{1,40}\]', text)
                and any(word in text for word in ('交易', '发货', '付款', '收货', '评价', '退款', '关闭')))


def event_metadata(text):
    rule = _RULES.get(normalized_phrase(text))
    if not rule:
        return {}
    return {'event_type': rule[0], 'event_actor_role': rule[1],
            'event_source': 'platform' if rule[2] else 'message'}


def resolve_direction(message, my_id, peer_id='', seller_id='', buyer_id=''):
    """Keep transport sender intact; business actions can differ from that sender."""
    actor = message.get('event_actor_role') or ''
    sender = str(message.get('senderId') or '')
    receiver = str(message.get('receiverId') or '')
    text = normalized_phrase(message.get('text'))
    participant_ids = {str(my_id), str(peer_id)} - {''}
    actor_id = ''
    if actor == 'platform':
        message.update(event_direction='platform', event_from='闲鱼', event_to='我方',
                       event_label='闲鱼通知 · 面向我方')
        return
    if actor == 'buyer' and buyer_id:
        actor_id = str(buyer_id)
    elif actor == 'seller' and seller_id:
        actor_id = str(seller_id)
    elif text == '你已发货':
        # These are recipient-perspective notices; sender may be the old payment sender.
        actor_id = receiver
    elif actor in ('buyer', 'seller'):
        # A platform event can reuse a stale transport sender; without a verified
        # order role, do not equate that field with the transaction actor.
        actor_id = ''
    elif actor == 'sender':
        actor_id = sender
    elif actor == 'peer' and peer_id:
        actor_id = str(peer_id)
    if not actor_id or actor_id == '0' or (peer_id and actor_id not in participant_ids):
        message.update(event_direction='unknown', event_from='闲鱼', event_to='会话双方',
                       event_label='闲鱼通知 · 操作方待核实')
        return
    own = actor_id == str(my_id)
    other_label = '买家' if seller_id == str(my_id) else '卖家' if buyer_id == str(my_id) else '对方'
    message.update(event_direction='outbound' if own else 'inbound',
                   event_actor_id=actor_id,
                   event_from='我方' if own else other_label,
                   event_to=other_label if own else '我方')
    if message.get('event_source') == 'platform':
        actor_label = '我方' if own else other_label
        role_label = {'buyer': '买家', 'seller': '卖家'}.get(actor)
        if role_label and role_label != actor_label:
            actor_label += '（' + role_label + '）'
        message['event_label'] = '闲鱼通知 · ' + actor_label + '操作'
    else:
        message['event_label'] = message['event_from'] + ' → ' + message['event_to']
