"""A paid event after a conversation update must reach the seller delivery path."""
import asyncio
import base64
from collections import defaultdict
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.db_manager import DBManager
from XianyuAutoAsync import XianyuLive


def packet(*messages):
    return {'headers': {'mid': 'fixture-batch'}, 'body': {'syncPushPackage': {'data': [
        {'data': base64.b64encode(json.dumps(m).encode()).decode()} for m in messages
    ]}}}


class SyncMessageBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = DBManager(str(Path(self.temp.name) / 'orders.db'))
        owner = self.db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        for account in ('fixture-seller', 'fixture-buyer'):
            self.db.save_cookie(account, 'unb=fixture', owner)
        self.db.insert_or_update_order('900000000000000001', cookie_id='fixture-seller',
                                      buyer_id='fixture-buyer', item_id='fixture-item',
                                      order_status='pending_ship')
        self.db.conn.execute('INSERT INTO delivery_reconciliation_activation VALUES(?,?)',
                             ('fixture-seller', time.time() - 60))
        self.db.conn.commit()
        self.db_patch = patch('app.db_manager.db_manager', self.db)
        self.manager_patch = patch('app.cookie_manager.manager', None)
        self.db_patch.start(); self.manager_patch.start()
        self.live = XianyuLive.__new__(XianyuLive)
        self.live.cookie_id = self.live.myid = 'fixture-seller'
        self.live.order_status_handler = None
        self.live._pending_order_real_values = {}
        self.live._order_detail_locks = defaultdict(asyncio.Lock)
        self.live.fetch_order_real_values = AsyncMock(return_value={})
        self.live.fetch_order_detail_info = AsyncMock(return_value={})
        self.live.send_notification = AsyncMock()
        self.live._handle_auto_delivery = AsyncMock()
        self.live._schedule_debounced_reply = AsyncMock()
        self.ws = SimpleNamespace(send=AsyncMock())

    async def asyncTearDown(self):
        self.manager_patch.stop(); self.db_patch.stop()
        self.db.close(); self.temp.cleanup()

    def paid(self):
        return {'1': {'2': 'fixture-chat@goofish', '5': int(time.time() * 1000),
                      '10': {'senderUserId': 'fixture-buyer', 'senderNick': 'fixture',
                             'reminderContent': '[我已付款，等待你发货]'}},
                '3': {'orderId': '900000000000000001', 'itemId': 'fixture-item'}}

    async def test_paid_event_after_nonchat_record_reaches_delivery_and_ack_once(self):
        source = packet({'1': []}, self.paid())
        original = json.dumps(source)
        await self.live.handle_message(source, self.ws)
        self.live._handle_auto_delivery.assert_awaited_once()
        self.assertEqual(self.live._handle_auto_delivery.await_args.args[3:6],
                         ('fixture-buyer', 'fixture-item', 'fixture-chat'))
        self.ws.send.assert_awaited_once()
        self.assertEqual(json.loads(self.ws.send.await_args.args[0])['headers']['mid'], 'fixture-batch')
        self.assertEqual(json.dumps(source), original)

    async def test_bad_record_does_not_drop_later_paid_event(self):
        source = packet({'1': []}, self.paid())
        source['body']['syncPushPackage']['data'][0] = {'unexpected': 'fixture'}
        await self.live.handle_message(source, self.ws)
        self.live._handle_auto_delivery.assert_awaited_once()
        self.ws.send.assert_awaited_once()

    async def test_buyer_self_echo_never_delivers_seller_order(self):
        self.live.cookie_id = self.live.myid = 'fixture-buyer'
        self.live._consume_programmatic_echo = lambda *_: True
        await self.live.handle_message(packet({'1': []}, self.paid()), self.ws)
        self.live._handle_auto_delivery.assert_not_awaited()
        self.assertEqual(self.db.get_order_by_id('900000000000000001')['cookie_id'], 'fixture-seller')

    async def test_single_record_keeps_same_delivery_path(self):
        await self.live.handle_message(packet(self.paid()), self.ws)
        self.live._handle_auto_delivery.assert_awaited_once()
        self.ws.send.assert_awaited_once()

    async def test_disabled_account_acknowledges_and_sends_nothing(self):
        manager = SimpleNamespace(get_cookie_status=lambda _: False)
        with patch('app.cookie_manager.manager', manager):
            await self.live.handle_message(packet({'1': []}, self.paid()), self.ws)
        self.live._handle_auto_delivery.assert_not_awaited()
        self.ws.send.assert_not_awaited()

    async def test_malformed_body_does_not_escape_handler(self):
        await self.live.handle_message({'headers': {}, 'body': 'fixture-malformed'}, self.ws)
        self.live._handle_auto_delivery.assert_not_awaited()

    async def test_replayed_old_payment_in_batch_never_delivers_or_regresses_order(self):
        old = self.paid()
        old['1']['5'] = int((time.time() - 300) * 1000)
        self.db.insert_or_update_order('900000000000000001', order_status='completed', system_shipped=True)
        await self.live.handle_message(packet({'1': []}, old), self.ws)
        self.live._handle_auto_delivery.assert_not_awaited()
        self.assertEqual(self.db.get_order_by_id('900000000000000001')['order_status'], 'completed')
