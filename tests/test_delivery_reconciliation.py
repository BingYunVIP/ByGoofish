"""No platform traffic: paid fallback, ownership, history cutoff and durable once-only scheduling."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.db_manager import DBManager
from app.services import auth_recovery, delivery_reconciliation as reconciliation


class DeliveryReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = DBManager(str(Path(self.temp.name) / 'orders.db'))
        self.owner = self.db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        self.cookie = 'unb=fixture-seller; _m_h5_tk=fixture-sign_1'
        for cid in ('fixture-seller', 'fixture-other'):
            self.db.save_cookie(cid, self.cookie, self.owner)
        self.now = time.time()
        self.db.conn.execute('INSERT INTO delivery_reconciliation_activation VALUES(?,?)',
                             ('fixture-seller', self.now - 60))
        self.db.conn.execute('INSERT INTO item_delivery_configs(user_id,cookie_id,item_id,enabled) VALUES(?,?,?,1)',
                             (self.owner, 'fixture-seller', 'fixture-item'))
        self.db.conn.commit()
        self.parsed = {'order_id': '900000000000000001', 'buyer_id': 'fixture-buyer', 'item_id': 'fixture-item',
                       'created_at': self.date(self.now - 30), 'status_text': '待发货', 'buy_num': 1, 'amount': '0.82'}
        self.api = SimpleNamespace(cookies_str=self.cookie, close=AsyncMock(),
                                  get_sold_orders=AsyncMock(side_effect=lambda **_: {'items': [dict(self.parsed)]}))
        self.live = SimpleNamespace(cookie_id='fixture-seller', myid='fixture-seller',
            cookies_str=self.cookie, cookies={}, ws=SimpleNamespace(closed=False),
            get_im_conversations=AsyncMock(return_value={'userConvs': []}), _handle_auto_delivery=AsyncMock())
        self.api_patch = patch('utils.xianyu_seller_api.XianyuSellerAPI', return_value=self.api)
        self.parse_patch = patch('utils.xianyu_seller_api.parse_sold_order', side_effect=lambda x: x)
        self.api_class = self.api_patch.start(); self.parse_patch.start()
        self.add_chat('fixture-chat')

    async def asyncTearDown(self):
        self.parse_patch.stop(); self.api_patch.stop()
        self.db.close(); self.temp.cleanup()

    @staticmethod
    def date(stamp):
        return datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    def add_chat(self, cid, cookie='fixture-seller', buyer='fixture-buyer', item='fixture-item'):
        self.db.conn.execute('INSERT INTO chat_workspace_state(user_id,cookie_id,cid,snapshot) VALUES(?,?,?,?)',
            (self.owner, cookie, cid, json.dumps({'otherUserId': buyer, 'itemId': item})))
        self.db.conn.commit()

    async def test_new_paid_order_uses_same_dispatch_and_schedules_once_across_reinitialize(self):
        result = await reconciliation.reconcile(self.live, self.db)
        self.assertEqual(result, {'checked': 1, 'dispatched': 1})
        args = self.live._handle_auto_delivery.await_args.args
        self.assertEqual(args[1], {'3': {'orderId': '900000000000000001'}})
        self.assertEqual(args[3:6], ('fixture-buyer', 'fixture-item', 'fixture-chat'))
        reconciliation.initialize(self.db)
        await reconciliation.reconcile(self.live, self.db)
        self.live._handle_auto_delivery.assert_awaited_once()
        self.assertEqual(self.db.conn.execute('SELECT status FROM delivery_reconciliation_runs').fetchone()[0], 'review')
        self.api.get_sold_orders.assert_called_with(query_code='NOT_SHIP', rows_per_page=20)

    async def test_success_marks_scheduling_complete(self):
        async def complete(*_):
            self.db.insert_or_update_order('900000000000000001', system_shipped=True)
        self.live._handle_auto_delivery.side_effect = complete
        await reconciliation.reconcile(self.live, self.db)
        self.assertEqual(self.db.conn.execute('SELECT status FROM delivery_reconciliation_runs').fetchone()[0], 'done')

    async def test_historical_future_missing_and_unpaid_orders_never_send(self):
        for update in ({'created_at': self.date(self.now - 120)}, {'created_at': self.date(self.now + 120)},
                       {'created_at': ''}, {'status_text': '等待买家付款'}, {'buyer_id': ''}):
            original = dict(self.parsed); self.parsed.update(update)
            await reconciliation.reconcile(self.live, self.db)
            self.parsed = original
        self.live._handle_auto_delivery.assert_not_awaited()
        self.assertEqual(self.db.conn.execute('SELECT count(*) FROM delivery_reconciliation_runs').fetchone()[0], 0)

    async def test_authentication_pause_never_requests_platform(self):
        auth_recovery.set_state(self.db, 'fixture-seller', 'required', 'message_verification_required')
        await reconciliation.reconcile(self.live, self.db)
        self.api_class.assert_not_called()
        self.live._handle_auto_delivery.assert_not_awaited()

    async def test_authentication_pause_during_request_stops_before_dispatch(self):
        async def pause(**_):
            auth_recovery.set_state(self.db, 'fixture-seller', 'required', 'message_verification_required')
            return {'items': [self.parsed]}
        self.api.get_sold_orders.side_effect = pause
        await reconciliation.reconcile(self.live, self.db)
        self.api.close.assert_awaited_once()
        self.live._handle_auto_delivery.assert_not_awaited()

    async def test_existing_unknown_delivery_is_never_retried(self):
        from app.services.delivery_ledger import claim
        self.db.insert_or_update_order('900000000000000001', cookie_id='fixture-seller', buyer_id='fixture-buyer',
                                      item_id='fixture-item', order_status='pending_ship')
        claim(self.db, 'fixture-seller', '900000000000000001', 'fixture-buyer', 'fixture-item')
        await reconciliation.reconcile(self.live, self.db)
        self.live._handle_auto_delivery.assert_not_awaited()

    async def test_disabled_product_is_not_a_fallback_candidate(self):
        self.db.conn.execute('UPDATE item_delivery_configs SET enabled=0'); self.db.conn.commit()
        await reconciliation.reconcile(self.live, self.db)
        self.live._handle_auto_delivery.assert_not_awaited()

    async def test_ambiguous_or_cross_account_conversations_cannot_receive_card(self):
        self.add_chat('fixture-second-chat')
        self.add_chat('fixture-other-chat', cookie='fixture-other')
        self.assertIsNone(reconciliation.resolve_chat_id(self.db, 'fixture-seller', 'fixture-buyer', 'fixture-item'))
        await reconciliation.reconcile(self.live, self.db)
        self.live._handle_auto_delivery.assert_not_awaited()

    async def test_missing_chat_leaves_order_for_later_resolution_without_claim(self):
        self.db.conn.execute('DELETE FROM chat_workspace_state'); self.db.conn.commit()
        await reconciliation.reconcile(self.live, self.db)
        self.live._handle_auto_delivery.assert_not_awaited()
        self.assertEqual(self.db.conn.execute('SELECT count(*) FROM delivery_reconciliation_runs').fetchone()[0], 0)

    async def test_concurrent_polls_claim_only_one_dispatch(self):
        async def pending(*_):
            await asyncio.sleep(.01)
        self.live._handle_auto_delivery.side_effect = pending
        await asyncio.gather(reconciliation.reconcile(self.live, self.db), reconciliation.reconcile(self.live, self.db))
        self.live._handle_auto_delivery.assert_awaited_once()

    async def test_stale_response_cannot_replace_manually_updated_credentials(self):
        async def change(**_):
            self.db.update_cookie_account_info('fixture-seller', cookie_value='unb=fixture-seller; cookie2=fixture-new')
            self.api.cookies_str = 'unb=fixture-seller; _m_h5_tk=fixture-rotated_2'
            return {'items': [self.parsed]}
        self.api.get_sold_orders.side_effect = change
        await reconciliation.reconcile(self.live, self.db)
        self.assertEqual(self.db.get_cookie('fixture-seller'), 'unb=fixture-seller; cookie2=fixture-new')
        self.live._handle_auto_delivery.assert_not_awaited()

    async def test_existing_activation_is_not_advanced_on_restart(self):
        before = reconciliation.activate(self.db, 'fixture-seller')
        reconciliation.initialize(self.db)
        self.assertEqual(reconciliation.activate(self.db, 'fixture-seller'), before)

    async def test_locally_sent_or_completed_order_cannot_be_regressed_by_stale_list(self):
        self.db.insert_or_update_order('900000000000000001', cookie_id='fixture-seller', buyer_id='fixture-buyer',
            item_id='fixture-item', order_status='completed', system_shipped=True)
        await reconciliation.reconcile(self.live, self.db)
        row = self.db.get_order_by_id('900000000000000001')
        self.assertTrue(row['system_shipped'])
        self.assertEqual(row['order_status'], 'completed')
        self.live._handle_auto_delivery.assert_not_awaited()

    async def test_restored_activation_cannot_reenable_old_orders(self):
        from app.services.user_backup import export_user, import_user
        payload = export_user(self.db.conn, self.owner)
        self.db.conn.execute("DELETE FROM delivery_reconciliation_activation WHERE cookie_id='fixture-seller'")
        self.db.conn.commit()
        before = time.time()
        import_user(self.db.conn, payload, self.owner)
        self.assertGreaterEqual(reconciliation.activate(self.db, 'fixture-seller'), before)

    async def test_fallback_uses_real_stock_ledger_and_cannot_send_twice(self):
        from collections import defaultdict
        from app.services import delivery_ledger
        from XianyuAutoAsync import XianyuLive
        self.db.conn.execute("INSERT INTO item_info(cookie_id,item_id,item_title) VALUES('fixture-seller','fixture-item','fixture')")
        card = self.db.conn.execute("INSERT INTO cards(name,type,data_content,enabled,user_id) VALUES('fixture','data','fake-code-one\nfake-code-two',1,?)",
                                   (self.owner,)).lastrowid
        self.db.conn.commit()
        live = XianyuLive.__new__(XianyuLive)
        live.__dict__.update(vars(self.live))
        del live._handle_auto_delivery
        live.delivery_cooldown = 60
        live._order_locks = defaultdict(asyncio.Lock)
        live._lock_usage_times = {}; live._lock_hold_info = {}; live.last_delivery_time = {}
        live.delivery_sent_orders = set(); live.delivery_blocked_orders = set(); live.confirmed_orders = {}
        live.order_status_handler = None
        live.is_lock_held = lambda _: False
        live.apply_delivery_block_rules = AsyncMock(return_value={'action': 'allow'})
        live._delayed_lock_release = AsyncMock()
        live.send_msg = AsyncMock(); live.send_delivery_failure_notification = AsyncMock()
        live.is_auto_confirm_enabled = lambda: False
        async def content(*_, delivery_context, **kwargs):
            token = delivery_ledger.claim(self.db, live.cookie_id, '900000000000000001', 'fixture-buyer', 'fixture-item')
            self.assertTrue(token)
            delivery_context.update(attempt_token=token, delivery_count=1)
            return delivery_ledger.reserve(self.db, token, card, 1)[0]
        live._auto_delivery = content
        with patch('app.db_manager.db_manager', self.db):
            await reconciliation.reconcile(live, self.db)
            await reconciliation.reconcile(live, self.db)
            await asyncio.sleep(0)
        live.send_msg.assert_awaited_once()
        self.assertEqual(self.db.conn.execute('SELECT count(*) FROM card_shipments').fetchone()[0], 1)
        self.assertEqual(self.db.conn.execute('SELECT data_content FROM cards WHERE id=?', (card,)).fetchone()[0], 'fake-code-two')
        self.assertTrue(self.db.get_order_by_id('900000000000000001')['system_shipped'])
        self.assertEqual(self.db.conn.execute('SELECT status FROM delivery_reconciliation_runs').fetchone()[0], 'done')
