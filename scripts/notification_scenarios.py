"""Reusable synthetic business scenarios; external trading operations are stubs.

The formal notification handlers and SMTP sender are intentionally left real.
This module neither reads live credentials nor starts account connections.
"""
import asyncio
import base64
from collections import defaultdict
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from XianyuAutoAsync import XianyuLive, ConnectionState

ACCOUNT = 'notification-fixture-seller'
BUYER = 'notification-fixture-buyer'
ITEM = 'notification-fixture-item'
ORDER = '900000000000000099'


def make_instance(db):
    live = XianyuLive.__new__(XianyuLive)
    live.cookie_id = live.myid = ACCOUNT
    live.cookies_str = 'unb=fixture'
    live.connection_state = ConnectionState.CONNECTED
    live.order_status_handler = None
    live.last_notification_time = {}
    live.notification_lock = asyncio.Lock()
    live.notification_cooldown = live.token_refresh_notification_cooldown = 0
    live._pending_order_real_values = {}
    live._order_detail_locks = defaultdict(asyncio.Lock)
    live._order_locks = defaultdict(asyncio.Lock)
    live._lock_usage_times = {}
    live._lock_hold_info = {}
    live.confirmed_orders = {}
    live.delivery_sent_orders = set()
    live.last_delivery_time = {}
    live.last_auto_delivery_time = {}
    live._delivery_timeout_alerted = set()
    live.fetch_order_real_values = AsyncMock(return_value={})
    live.fetch_order_detail_info = AsyncMock(return_value={'quantity': 1, 'order_status': 'pending_ship'})
    live._schedule_debounced_reply = AsyncMock()
    live.is_lock_held = lambda _: False
    live.can_auto_delivery = lambda _: True
    live.is_auto_confirm_enabled = lambda: True
    live.apply_delivery_block_rules = AsyncMock(return_value={'action': 'allow'})
    live._delayed_lock_release = AsyncMock()
    live.send_msg = AsyncMock()  # Never send a real IM or card.
    live.auto_confirm = AsyncMock(return_value={'success': True})
    return live


def setup_account(db, owner):
    db.save_cookie(ACCOUNT, 'unb=fixture', owner)
    db.save_cookie(BUYER, 'unb=fixture', owner)
    db.conn.execute('INSERT OR IGNORE INTO item_info(cookie_id,item_id,item_title,item_price) VALUES(?,?,?,?)',
                    (ACCOUNT, ITEM, '自动场景验收虚拟商品', '1'))
    db.conn.commit()


def incoming_packet(text, stamp=None):
    message = {'1': {'2': 'notification-fixture-chat@goofish', '5': stamp or int(time.time() * 1000),
                    '10': {'senderUserId': BUYER, 'senderNick': '自动场景验收虚拟买家',
                           'sessionType': '1', 'reminderContent': text}},
               '3': {'itemId': ITEM}}
    return {'headers': {'mid': 'fixture-notification'}, 'body': {'syncPushPackage': {'data': [
        {'data': base64.b64encode(json.dumps(message).encode()).decode()}]}}}


async def run_scenario(event, live, db, owner):
    """Inject a business outcome, never use a channel-test API or notifier mock."""
    label = '【自动场景验收·虚拟数据·无需处理】'
    if event == 'account_offline':
        from app.services import account_offline, auth_recovery
        auth_recovery.set_state(db, ACCOUNT, 'none')
        now=time.time()
        await account_offline.check_once(db, SimpleNamespace(instances={}), now)
        await account_offline.check_once(db, SimpleNamespace(instances={}), now+account_offline.GRACE_SECONDS+1)
        auth_recovery.set_state(db, ACCOUNT, 'none')
    elif event == 'buyer_message':
        await live.handle_message(incoming_packet(label + '买家发来一条新私聊'), SimpleNamespace(send=AsyncMock()))
    elif event in ('delivery_failed', 'delivery_success', 'delivery_confirm_failed'):
        db.insert_or_update_order(ORDER, cookie_id=ACCOUNT, buyer_id=BUYER, item_id=ITEM,
                                  order_status='pending_ship', system_shipped=False)
        live._auto_delivery = AsyncMock(return_value=None if event == 'delivery_failed' else label + '虚拟卡券')
        live.auto_confirm = AsyncMock(return_value={'success': event != 'delivery_confirm_failed', 'error': label + '模拟平台拒绝确认'})
        await live._handle_auto_delivery(SimpleNamespace(send=AsyncMock()), {'3': {'orderId': ORDER, 'itemId': ITEM}},
                                          label + '虚拟买家', BUYER, ITEM, 'notification-fixture-chat', 'fixture-time')
    elif event == 'delivery_timeout':
        api = SimpleNamespace(cookies_str=live.cookies_str, close=AsyncMock(),
            get_sold_orders=AsyncMock(side_effect=[{'items': [{'order_id': ORDER, 'item_title': label + '虚拟订单'}]}, {'items': []}]))
        with patch('utils.xianyu_seller_api.XianyuSellerAPI', return_value=api), patch('utils.xianyu_seller_api.parse_sold_order', side_effect=lambda x: x):
            await live._check_delivery_timeout()
    elif event == 'logistics_quote':
        from fastapi import HTTPException
        from app.routers.logistics_quote import create_logistics_quote_router, QuoteParseRequest
        router = create_logistics_quote_router(lambda: None, db)
        endpoint = next(r.endpoint for r in router.routes if r.path == '/logistics-quote/parse')
        try:
            await endpoint(QuoteParseRequest(text=label + '模拟无有效报价', cookie_id=ACCOUNT), {'user_id': owner})
        except HTTPException as error:
            assert error.status_code == 422
    elif event == 'instance_restart_failed':
        def fail_restart(*args, **kwargs):
            raise RuntimeError('自动场景验收：模拟实例管理器拒绝重启')
        with patch('app.cookie_manager.manager', SimpleNamespace(update_cookie=fail_restart)):
            worker = await live._restart_instance()
            await asyncio.to_thread(worker.join, 40)
            if worker.is_alive():
                raise RuntimeError('模拟重启通知未在期限内结束')
    elif event == 'login_failed':
        # This is the exact password-login failure callback used by slider_patch.
        from utils.slider_patch import send_notification
        await asyncio.to_thread(send_notification, ACCOUNT, label + '登录失败', label + '模拟密码错误', 'error', event_type='login_failed')
    else:
        from app.services import auth_recovery
        auth_categories = {
            'login_verification_required':'login_verification_required',
            'login_session_expired':'login_session_expired',
            'message_verification_required':'message_verification_required',
            'credentials_invalid':'signing_token_missing',
        }
        if event in auth_categories:
            auth_recovery.set_state(db, ACCOUNT, 'required', auth_categories[event])
        await live.send_token_refresh_notification(label + '模拟业务结果：' + event, event)
    await asyncio.sleep(0)  # Allow only local stub task finalizers to complete.
