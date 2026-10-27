"""同一订单的买卖两端、明确商品标识和北京时间边界回归。"""
import asyncio
from datetime import datetime, timezone
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch
import unittest

from app.db_manager import DBManager
from app.order_status_handler import OrderStatusHandler
from XianyuAutoAsync import XianyuLive
from utils.message_identity import extract_item_id
from utils.order_time import platform_order_time, fetched_order_time
from utils.seller_order_sync import _save_order
from utils.xianyu_seller_api import parse_sold_order, parse_refund_order


class ItemIdentityTests(unittest.TestCase):
    def test_session_user_and_order_numbers_are_not_products(self):
        for message in ({'1':'123456789012@goofish'}, {'id':'123456789012'},
                        {'orderId':'123456789012'}, {'3':{'content':'订单123456789012'}}):
            self.assertIsNone(extract_item_id(message))

    def test_only_explicit_fields_or_item_links_work(self):
        self.assertEqual(extract_item_id({'3':{'itemId':'item-real'}}),'item-real')
        self.assertEqual(extract_item_id({'data':'{"item_id":"item-json"}'}),'item-json')
        self.assertEqual(extract_item_id({'reminderUrl':'fleamarket://order_detail?id=123&itemId=item-real'}),'item-real')
        self.assertEqual(extract_item_id({'url':'https://www.goofish.com/item?id=item-real'}),'item-real')
        self.assertIsNone(extract_item_id({'url':'https://www.goofish.com/order_detail?id=123456789012'}))

    def test_order_extraction_does_not_guess_generic_id(self):
        live=XianyuLive.__new__(XianyuLive);live.cookie_id='seller'
        self.assertIsNone(live._extract_order_id({'id':'123456789012'}))
        self.assertEqual(live._extract_order_id({'3':{'orderId':'123456789012'}}),'123456789012')


class OrderIdentityDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=DBManager(str(Path(self.temp.name)/'orders.db'))
        self.owner=self.db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        for cid in ('seller','buyer'):
            self.db.save_cookie(cid,'unb=fixture',self.owner)
        self.db.conn.execute("INSERT INTO item_info(cookie_id,item_id,item_title) VALUES('seller','item-real','fixture product')")
        self.db.conn.commit()
        self.patch=patch('app.db_manager.db_manager',self.db);self.patch.start()
        self.addCleanup(self.temp.cleanup);self.addCleanup(self.db.close);self.addCleanup(self.patch.stop)

    def live(self,cid):
        instance=XianyuLive.__new__(XianyuLive);instance.cookie_id=cid;instance.myid=cid
        instance._pending_order_real_values={}
        return instance

    def test_buyer_own_paid_event_never_prepares_seller_order(self):
        message={'1':{'10':{'senderUserId':'buyer','reminderContent':'[我已付款，等待你发货]'},'2':'123456789012@goofish'}}
        self.assertFalse(self.live('buyer')._is_seller_order_event('order-paid',message))
        self.assertTrue(self.live('seller')._is_seller_order_event('order-paid',message))

    def test_nonchat_item_event_only_belongs_to_listing_owner(self):
        message={'1':'123456789012@goofish','3':{'itemId':'item-real','orderId':'1234567890123'}}
        self.assertFalse(self.live('buyer')._is_seller_order_event('order-paid',message))
        self.assertTrue(self.live('seller')._is_seller_order_event('order-paid',message))

    def test_buyer_cannot_create_own_sell_order_or_replace_verified_owner(self):
        self.assertFalse(self.db.insert_or_update_order('o',cookie_id='buyer',buyer_id='buyer',item_id='chat-id'))
        self.assertTrue(self.db.insert_or_update_order('o',cookie_id='seller',buyer_id='buyer',item_id='item-real',order_status='pending_ship'))
        self.assertFalse(self.db.insert_or_update_order('o',cookie_id='buyer',item_id='chat-id'))
        row=self.db.get_order_by_id('o')
        self.assertEqual((row['cookie_id'],row['item_id'],row['buyer_id']),('seller','item-real','buyer'))
        self.assertFalse(self.live('buyer')._is_seller_order_event('o',{'3':{'itemId':'item-real'}}))

    def test_authoritative_sold_sync_can_repair_legacy_wrong_owner_and_preserve_title(self):
        self.db.insert_or_update_order('o',cookie_id='buyer',buyer_id='old-peer',item_id='wrong-chat')
        self.assertTrue(_save_order(self.db,'seller',{'order_id':'o','item_id':'item-real','buyer_id':'buyer','item_title':'real title','status_text':'待发货','created_at':'2026-10-20 11:52:05'}))
        row=self.db.get_order_by_id('o')
        self.assertEqual((row['cookie_id'],row['item_id']),('seller','item-real'))
        self.assertEqual(self.db.get_item_info('seller','item-real')['item_title'],'fixture product')

    def test_snapshot_prefers_real_item_id_over_message_hint(self):
        live=self.live('seller')
        live._pending_order_real_values={'o':{'item_id':'item-real','buyer_id':'buyer','created_at':'2026-10-20 11:52:05'}}
        self.assertTrue(live._save_order_event_snapshot('o',{'1':{'10':{'reminderContent':'[我已付款，等待你发货]'}}},item_id='wrong-chat',buyer_id='buyer'))
        row=self.db.get_order_by_id('o')
        self.assertEqual((row['item_id'],row['created_at']),('item-real','2026-10-20 11:52:05'))

    def test_buyer_status_update_cannot_mutate_seller_order(self):
        self.db.insert_or_update_order('o',cookie_id='seller',buyer_id='buyer',item_id='item-real',order_status='pending_ship')
        handler=OrderStatusHandler()
        self.assertFalse(handler.update_order_status('o','cancelled','buyer'))
        self.assertEqual(self.db.get_order_by_id('o')['order_status'],'pending_ship')

    def test_guard_failure_is_visible_and_does_not_reserve_or_send(self):
        self.db.insert_or_update_order('o',cookie_id='seller',buyer_id='buyer',item_id='real-other',order_status='pending_ship')
        live=self.live('seller');live._extract_order_id=lambda _: 'o'
        socket=AsyncMock()
        asyncio.run(live._handle_auto_delivery(socket,{},'fixture','buyer','item-real','chat','fixture-time'))
        socket.send.assert_not_awaited()
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM delivery_attempts').fetchone()[0],0)
        row=self.db.get_risk_control_logs(cookie_id='seller')[0]
        self.assertEqual(row['event_type'],'delivery_failed')
        self.assertIn('商品归属',row['processing_result'])

    def test_platform_local_time_survives_today_date_filter(self):
        parsed=parse_sold_order({'commonData':{'orderId':'o','itemId':'item-real','createTime':'2026-10-20 19:52:05','orderStatus':'待发货'},'buyerInfoVO':{'buyerId':'buyer'}})
        self.assertEqual(parsed['created_at'],'2026-10-20 11:52:05')
        _save_order(self.db,'seller',parsed)
        rows=self.db.get_orders_by_cookie('seller',start_time='2026-10-20T00:00:00+08:00',end_time='2026-10-21T00:00:00+08:00')
        self.assertEqual([r['order_id'] for r in rows],['o'])
        self.assertEqual(parse_refund_order({'commonData':{'createTime':'2026-10-20 00:00:00'}})['created_at'],'2026-10-19 16:00:00')


class OrderTimeTests(unittest.TestCase):
    def test_boundaries_timezone_and_epoch(self):
        self.assertEqual(platform_order_time('2026-10-20 00:00:00'),'2026-10-19 16:00:00')
        self.assertEqual(platform_order_time('2026-10-20T19:52:05+08:00'),'2026-10-20 11:52:05')
        self.assertEqual(platform_order_time('2026-10-20T11:52:05Z'),'2026-10-20 11:52:05')
        stamp=datetime(2026,10,20,11,52,5,tzinfo=timezone.utc).timestamp()
        self.assertEqual(platform_order_time(int(stamp*1000)),'2026-10-20 11:52:05')
        self.assertIsNone(platform_order_time('invalid'))


class FetchedOrderTimeTests(unittest.TestCase):
    def test_cached_and_seller_times_are_not_converted_twice(self):
        for flag in ('from_cache','from_seller_api'):
            self.assertEqual(fetched_order_time({'order_time':'2026-10-20 11:52:05',flag:True}),'2026-10-20 11:52:05')
        self.assertEqual(fetched_order_time({'order_time':'2026-10-20 19:52:05'}),'2026-10-20 11:52:05')


class OrderDeliveryIdempotencyTests(unittest.TestCase):
    setUp = OrderIdentityDatabaseTests.setUp
    live = OrderIdentityDatabaseTests.live
    def test_fixed_identity_reaches_delivery_once_without_any_real_network(self):
        from collections import defaultdict
        from app.services import delivery_ledger
        async def exercise():
            self.db.insert_or_update_order('paid-once',cookie_id='seller',buyer_id='buyer',item_id='item-real',order_status='pending_ship')
            card=self.db.conn.execute("INSERT INTO cards(name,type,data_content,enabled,user_id) VALUES('fixture','data','fake-code-one\nfake-code-two',1,?)",(self.owner,)).lastrowid
            self.db.conn.commit()
            live=self.live('seller');live.delivery_cooldown=60
            live._extract_order_id=lambda _: 'paid-once'
            live._order_locks=defaultdict(asyncio.Lock);live._lock_usage_times={};live._lock_hold_info={}
            live.last_delivery_time={};live.delivery_sent_orders=set();live.delivery_blocked_orders=set();live.confirmed_orders={}
            live.order_status_handler=None;live.is_lock_held=lambda _: False
            live.apply_delivery_block_rules=AsyncMock(return_value={'action':'allow'})
            live._delayed_lock_release=AsyncMock()
            live.send_msg=AsyncMock();live.send_delivery_failure_notification=AsyncMock()
            live.auto_confirm=AsyncMock(return_value={'success':True})
            live.is_auto_confirm_enabled=lambda: False
            async def content(*args,delivery_context,**kwargs):
                token=delivery_ledger.claim(self.db,'seller','paid-once','buyer','item-real')
                self.assertTrue(token)
                delivery_context.update(attempt_token=token,delivery_count=1)
                return delivery_ledger.reserve(self.db,token,card,1)[0]
            live._auto_delivery=content
            await live._handle_auto_delivery(AsyncMock(),{},'fixture','buyer','item-real','chat','fixture-time')
            await live._handle_auto_delivery(AsyncMock(),{},'fixture','buyer','item-real','chat','fixture-time')
            live.send_msg.assert_awaited_once()
            self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM card_shipments').fetchone()[0],1)
            self.assertEqual(self.db.conn.execute('SELECT data_content FROM cards WHERE id=?',(card,)).fetchone()[0],'fake-code-two')
            self.assertEqual(self.db.conn.execute("SELECT system_shipped FROM orders WHERE order_id='paid-once'").fetchone()[0],1)
            await asyncio.sleep(0)
        asyncio.run(exercise())
