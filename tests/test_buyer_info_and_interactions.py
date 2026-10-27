"""买家信息落库、只读来源诊断、延时互动与跨重启防重回归。"""
import asyncio
import base64
import json
import io
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch
from collections import defaultdict

from loguru import logger
from app.db_manager import DBManager
from XianyuAutoAsync import XianyuLive
from utils.order_buyer_info import (
    buyer_info_complete, field_state, log_buyer_info_capture,
    merge_buyer_info, parse_privacy_address,
)
from utils.seller_order_sync import _save_order, supplement_buyer_info, fetch_order_detail_direct
from utils.xianyu_seller_api import SellerApiError, parse_consign_render
from utils.buyer_interaction_rules import completed_after


class BuyerDatabaseTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'buyer.db')
        self.db = DBManager(self.path)
        owner = self.db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        self.db.save_cookie('fixture-seller', 'fixture=value', owner)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: self.db.close())

    def test_all_buyer_fields_survive_list_detail_and_restart(self):
        fields = dict(buyer_id='fixture-buyer', buyer_nick='示例昵称', receiver_name='测试收件人',
                      receiver_phone='13800000000', receiver_address='示例省示例市测试路1号', buyer_info_private=True)
        self.assertTrue(self.db.insert_or_update_order('fixture-order', cookie_id='fixture-seller', **fields))
        for row in [self.db.get_order_by_id('fixture-order'), self.db.get_orders_by_cookie('fixture-seller')[0]]:
            for key, value in fields.items():
                self.assertEqual(row[key], value)
        self.db.close(); self.db = DBManager(self.path)
        self.assertEqual(self.db.get_order_by_id('fixture-order')['buyer_nick'], fields['buyer_nick'])

    def test_created_at_insert_keeps_all_original_column_positions(self):
        self.assertTrue(self.db.insert_or_update_order('fixture-order', 'fixture-item', 'fixture-buyer', '面额', '五元',
                                                      cookie_id='fixture-seller', created_at='2026-10-20 12:00:00', buyer_nick='示例昵称'))
        row = self.db.get_order_by_id('fixture-order')
        self.assertEqual((row['spec_name'], row['spec_value'], row['created_at']), ('面额', '五元', '2026-10-20 12:00:00'))

    def test_empty_or_masked_sync_does_not_erase_known_buyer_fields(self):
        base = dict(order_id='fixture-order', item_id='fixture-item', buyer_id='fixture-buyer', buyer_nick='示例昵称',
                    receiver_name='示例收件人', receiver_phone='13800000000', receiver_address='示例完整地址')
        self.assertTrue(_save_order(self.db, 'fixture-seller', base))
        partial = dict(base, buyer_nick='', receiver_phone='138****0000', receiver_address='')
        self.assertTrue(_save_order(self.db, 'fixture-seller', partial))
        row = self.db.get_order_by_id('fixture-order')
        self.assertEqual(row['buyer_nick'], base['buyer_nick'])
        self.assertEqual(row['receiver_phone'], base['receiver_phone'])
        self.assertEqual(row['receiver_address'], base['receiver_address'])

    def test_claim_is_once_per_account_order_and_action_across_restart(self):
        self.assertTrue(self.db.claim_buyer_interaction('fixture-seller', 'fixture-order', 'flower'))
        self.assertFalse(self.db.claim_buyer_interaction('fixture-seller', 'fixture-order', 'flower'))
        self.assertTrue(self.db.finish_buyer_interaction('fixture-seller', 'fixture-order', 'flower', 'sent'))
        activated = self.db.buyer_interaction_activated_at('fixture-seller', 'flower')
        self.db.close(); self.db = DBManager(self.path)
        self.assertFalse(self.db.claim_buyer_interaction('fixture-seller', 'fixture-order', 'flower'))
        self.assertEqual(self.db.buyer_interaction_activated_at('fixture-seller', 'flower'), activated)
        self.assertTrue(self.db.claim_buyer_interaction('fixture-seller', 'fixture-order', 'rate'))

    def test_upgrade_baselines_completed_orders_without_sending(self):
        self.db.insert_or_update_order('fixture-old', cookie_id='fixture-seller', order_status='completed')
        self.db.conn.execute('DROP TABLE buyer_interaction_attempts'); self.db.conn.commit()
        self.db.close(); self.db = DBManager(self.path)
        for action in ('flower', 'rate', 'thanks'):
            self.assertFalse(self.db.claim_buyer_interaction('fixture-seller', 'fixture-old', action))
        self.assertTrue(self.db.claim_buyer_interaction('fixture-seller', 'fixture-new', 'flower'))

    def test_existing_account_flags_are_not_reset_and_unknown_stays_off(self):
        self.db.update_buyer_interaction_settings('fixture-seller', auto_flower_enabled=False)
        self.db.save_cookie('fixture-seller', 'fixture=updated')
        self.assertFalse(self.db.get_buyer_interaction_settings('fixture-seller')['auto_flower_enabled'])
        self.assertFalse(self.db.update_buyer_interaction_settings('missing', auto_flower_enabled=True))
        self.assertIsNone(self.db.buyer_interaction_activated_at('missing', 'flower'))

    def test_thanks_upgrade_preserves_original_activation_and_existing_attempts(self):
        original = self.db.buyer_interaction_activated_at('fixture-seller', 'flower')
        self.db.claim_buyer_interaction('fixture-seller', 'fixture-order', 'thanks')
        self.db.conn.execute("DELETE FROM buyer_interaction_activation WHERE action='thanks'")
        self.db.conn.commit()
        self.db.close(); self.db = DBManager(self.path)
        self.assertEqual(self.db.buyer_interaction_activated_at('fixture-seller', 'thanks'), original)
        self.assertTrue(self.db.has_buyer_interaction_attempt('fixture-seller', 'fixture-order', 'thanks'))

    def test_reenabling_thanks_starts_new_window_without_resetting_claims(self):
        original = self.db.buyer_interaction_activated_at('fixture-seller', 'thanks')
        self.db.claim_buyer_interaction('fixture-seller', 'fixture-order', 'thanks')
        self.db.update_buyer_interaction_settings('fixture-seller', auto_thanks_enabled=False)
        self.db.update_buyer_interaction_settings('fixture-seller', auto_thanks_enabled=True)
        self.assertGreaterEqual(self.db.buyer_interaction_activated_at('fixture-seller', 'thanks'), original)
        self.assertTrue(self.db.has_buyer_interaction_attempt('fixture-seller', 'fixture-order', 'thanks'))


class BuyerCaptureTests(TestCase):
    def test_diagnostics_contain_only_presence_and_masking(self):
        info = dict(buyer_id='private-buyer-identity', buyer_nick='私密昵称不得记录',
                    receiver_name='私密收件人不得记录', receiver_phone='13912345678',
                    receiver_address='私密地址不得记录', is_privacy=True)
        sink = io.StringIO(); handle = logger.add(sink, format='{message}')
        try: log_buyer_info_capture('fixture-seller', 'fixture-order', info, 'sold.get', {'buyerInfoVO': {}})
        finally: logger.remove(handle)
        message = sink.getvalue()
        for key in ('buyer_id', 'buyer_nick', 'receiver_name', 'receiver_phone', 'receiver_address'):
            self.assertNotIn(info[key], message)
        self.assertIn('buyer_nick=present', message)
        self.assertIn('privacy=true', message)
        self.assertEqual(field_state('138****0000'), 'masked')
        self.assertEqual(field_state(''), 'missing')

    def test_privacy_address_supports_wrappers_without_guessing_unrelated_names(self):
        data = {'module': {'receiverName': '示例收件人', 'receiverMobile': '138****0000',
                           'province': '示例省', 'city': '示例市', 'detailAddress': '示例路1号'}}
        parsed = parse_privacy_address(data)
        self.assertEqual(parsed['receiver_name'], '示例收件人')
        self.assertEqual(parsed['receiver_phone'], '138****0000')
        self.assertEqual(parsed['receiver_address'], '示例省 示例市 示例路1号')
        self.assertEqual(parse_privacy_address({'buyerInfo': {'name': '不能猜作收货人'}})['receiver_name'], '')

    def test_consign_render_can_supply_missing_buyer_and_address(self):
        parsed = parse_consign_render({'buyerInfoVO': {'buyerId': 'fixture-buyer', 'userNick': '示例昵称'},
                                      'addressInfo': {'receiverName': '示例收件人', 'receiverMobile': '13800000000', 'fullAddress': '示例地址'}})
        self.assertTrue(buyer_info_complete(parsed))

    def test_merge_does_not_downgrade_full_data_to_mask(self):
        data = {'receiver_phone': '13800000000'}
        merge_buyer_info(data, {'receiver_phone': '138****0000', 'buyer_nick': '示例昵称'})
        self.assertEqual(data['receiver_phone'], '13800000000')
        self.assertEqual(data['buyer_nick'], '示例昵称')

    def test_activation_time_uses_platform_timezone(self):
        self.assertTrue(completed_after('2026-10-20 20:00:00', 1792497599))
        self.assertFalse(completed_after('', time.time()))
        self.assertFalse(completed_after('2020-01-01 00:00:00', time.time()))


class BuyerSourceTests(IsolatedAsyncioTestCase):
    async def test_detail_reuses_consign_response_for_spec_and_buyer_without_second_request(self):
        buyer = dict(buyerId='fixture-buyer', userNick='示例昵称', name='示例收件人',
                     phone='13800000000', address='示例地址')
        api = SimpleNamespace(
            get_sold_orders=AsyncMock(return_value={'items':[{'commonData':{'orderId':'fixture-order'},'buyerInfoVO':{'buyerId':'fixture-buyer'}}]}),
            get_order_detail=AsyncMock(return_value={'components':[]}),
            get_consign_render=AsyncMock(return_value={'buyerInfoVO':buyer, 'bizOrderInfoList':[{
                'commonData':{'orderId':'fixture-order'},'itemVO':{'itemInfoLines':[{'key':'面额','value':'五元'}]}}]}),
            get_privacy_address=AsyncMock(), close=AsyncMock(),
        )
        with patch('utils.seller_order_sync.XianyuSellerAPI',return_value=api):
            result=await fetch_order_detail_direct('fixture-seller','fixture=value','fixture-order')
        self.assertTrue(buyer_info_complete(result)); self.assertEqual(result['spec_value'],'五元')
        api.get_consign_render.assert_awaited_once(); api.get_privacy_address.assert_not_awaited(); api.close.assert_awaited_once()

    async def test_detail_with_spec_still_supplements_missing_receiver_fields(self):
        api = SimpleNamespace(
            get_sold_orders=AsyncMock(return_value={'items':[{'commonData':{'orderId':'fixture-order'},'buyerInfoVO':{'buyerId':'fixture-buyer','userNick':'示例昵称'}}]}),
            get_order_detail=AsyncMock(return_value={'components':[{'render':'orderInfoVO','data':{'itemInfo':{'skuInfo':'面额:五元'}}}]}),
            get_consign_render=AsyncMock(return_value={}),
            get_privacy_address=AsyncMock(return_value={'module':{'receiverName':'示例收件人','receiverMobile':'13800000000','fullAddress':'示例地址'}}),
            close=AsyncMock(),
        )
        with patch('utils.seller_order_sync.XianyuSellerAPI',return_value=api):
            result=await fetch_order_detail_direct('fixture-seller','fixture=value','fixture-order')
        self.assertTrue(buyer_info_complete(result));self.assertEqual(result['buyer_nick'],'示例昵称')
        api.get_consign_render.assert_awaited_once();api.get_privacy_address.assert_awaited_once()

    async def test_missing_receiver_queries_each_source_once_without_loop(self):
        api = SimpleNamespace(get_consign_render=AsyncMock(return_value={'buyerInfoVO': {'userNick': '示例昵称'}}),
                              get_privacy_address=AsyncMock(return_value={'data': {'receiverName': '示例收件人', 'receiverMobile': '13800000000', 'fullAddress': '示例地址'}}))
        info = {'buyer_id': 'fixture-buyer'}
        await supplement_buyer_info(api, 'fixture-seller', 'fixture-order', info)
        self.assertTrue(buyer_info_complete(info))
        api.get_consign_render.assert_awaited_once(); api.get_privacy_address.assert_awaited_once()

    async def test_complete_source_does_not_make_extra_platform_requests(self):
        api = SimpleNamespace(get_consign_render=AsyncMock(), get_privacy_address=AsyncMock())
        data = dict(buyer_id='fixture-buyer', buyer_nick='示例昵称', receiver_name='示例收件人', receiver_phone='13800000000', receiver_address='示例地址')
        await supplement_buyer_info(api, 'fixture-seller', 'fixture-order', data)
        api.get_consign_render.assert_not_awaited(); api.get_privacy_address.assert_not_awaited()

    async def test_source_failure_does_not_erase_or_invent_fields(self):
        api = SimpleNamespace(get_consign_render=AsyncMock(side_effect=SellerApiError('fixture', ['FIXTURE_ERROR'])), get_privacy_address=AsyncMock(return_value={}))
        data = {'buyer_id': 'fixture-buyer'}
        await supplement_buyer_info(api, 'fixture-seller', 'fixture-order', data)
        self.assertEqual(data, {'buyer_id': 'fixture-buyer'})


class BuyerInteractionTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.db = DBManager(str(Path(self.temp.name) / 'buyer.db'))
        self.owner = self.db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        self.db.save_cookie('fixture-seller', 'fixture=value', self.owner)
        self.db.insert_or_update_order('fixture-order', cookie_id='fixture-seller', buyer_id='fixture-buyer', order_status='completed')
        self.live = XianyuLive.__new__(XianyuLive); self.live.cookie_id='fixture-seller'; self.live.myid='fixture-seller'; self.live.cookies_str='fixture=value'
        self.live._interruptible_sleep = AsyncMock(); self.live.send_im_text = AsyncMock()
        self.item = {'commonData': {'orderId': 'fixture-order', 'orderStatus': '交易成功', 'finishTime': time.time()+5, 'sellerRateStatus': 4},
                     'buyerInfoVO': {'buyerId': 'fixture-buyer'}}
        self.api = SimpleNamespace(get_sold_orders=AsyncMock(return_value={'items':[self.item]}), require_flower=AsyncMock(return_value={}),
                                   create_rate=AsyncMock(return_value={'success':True}), close=AsyncMock(), cookies_str='fixture=value')
        self.patches = [patch('app.db_manager.db_manager', self.db), patch('utils.xianyu_seller_api.XianyuSellerAPI', return_value=self.api),
                        patch('app.services.auth_recovery.auth_paused', return_value=False),
                        patch('utils.risk_control.registry.get', return_value=SimpleNamespace(is_blocked=False))]
        for item in self.patches: item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches): item.stop()
        self.db.close(); self.temp.cleanup()

    async def test_completed_order_requests_flower_once_without_page_click(self):
        self.assertEqual(await self.live._run_buyer_interactions(False, True), {'rated':0,'flowered':1})
        await self.live._run_buyer_interactions(False, True)
        self.api.require_flower.assert_awaited_once_with('fixture-order')
        delay = self.live._interruptible_sleep.await_args_list[0].args[0]
        self.assertGreaterEqual(delay, 30); self.assertLessEqual(delay, 60)
        self.assertEqual(self.db.conn.execute("SELECT status FROM buyer_interaction_attempts WHERE action='flower'").fetchone()[0], 'sent')

    async def test_unknown_send_result_is_not_retried(self):
        self.api.require_flower.side_effect = TimeoutError('fixture')
        await self.live._run_buyer_interactions(False, True); await self.live._run_buyer_interactions(False, True)
        self.api.require_flower.assert_awaited_once()
        self.assertEqual(self.db.conn.execute("SELECT status FROM buyer_interaction_attempts WHERE action='flower'").fetchone()[0], 'needs_review')

    async def test_disabled_account_skips_query_and_send(self):
        self.db.update_buyer_interaction_settings('fixture-seller', auto_flower_enabled=False, auto_thanks_enabled=False)
        await self.live._run_buyer_interactions(False, True)
        self.api.get_sold_orders.assert_not_awaited(); self.api.require_flower.assert_not_awaited()

    async def test_disabled_account_never_sends_any_interaction(self):
        self.db.save_cookie_status('fixture-seller', False)
        await self.live._run_buyer_interactions(True, True)
        self.assertFalse(await self.live.send_post_receipt_thanks(None,'fixture-chat','fixture-buyer','fixture-order'))
        self.api.get_sold_orders.assert_not_awaited();self.live.send_im_text.assert_not_awaited()

    async def test_switch_off_during_delay_cancels_action(self):
        self.live._interruptible_sleep.side_effect=lambda _:self.db.update_buyer_interaction_settings('fixture-seller',auto_flower_enabled=False)
        await self.live._run_buyer_interactions(False, True)
        self.api.require_flower.assert_not_awaited()

    async def test_old_or_pending_orders_are_never_auto_contacted(self):
        self.item['commonData']['finishTime']='2020-01-01 00:00:00'
        await self.live._run_buyer_interactions(False, True)
        self.item['commonData'].update(orderStatus='待发货',finishTime=time.time()+5)
        await self.live._run_buyer_interactions(False, True)
        self.api.require_flower.assert_not_awaited()

    async def test_cancellation_after_request_marks_review(self):
        self.api.require_flower.side_effect=asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError): await self.live._run_buyer_interactions(False, True)
        self.assertEqual(self.db.conn.execute("SELECT status FROM buyer_interaction_attempts WHERE action='flower'").fetchone()[0], 'needs_review')

    async def test_rating_and_flower_have_separate_delays_and_claims(self):
        self.item['commonData']['sellerRateStatus']=0
        result=await self.live._run_buyer_interactions(True, True)
        self.assertEqual(result, {'rated':1,'flowered':1})
        self.assertEqual(self.live._interruptible_sleep.await_count, 2)
        self.api.create_rate.assert_awaited_once();self.api.require_flower.assert_awaited_once()

    async def test_thanks_has_delay_and_persisted_dedup(self):
        self.assertTrue(await self.live.send_post_receipt_thanks(None,'fixture-chat','fixture-buyer','fixture-order'))
        self.assertFalse(await self.live.send_post_receipt_thanks(None,'fixture-chat','fixture-buyer','fixture-order'))
        self.live.send_im_text.assert_awaited_once()
        first=self.live._interruptible_sleep.await_args_list[0].args[0]
        self.assertGreaterEqual(first,15);self.assertLessEqual(first,30)

    async def test_buyer_side_cannot_thank_itself_or_foreign_seller(self):
        self.assertFalse(await self.live.send_post_receipt_thanks(None,'fixture-chat','fixture-seller','fixture-order'))
        self.live.send_im_text.assert_not_awaited()

    async def test_completed_message_dispatches_before_generic_system_filter(self):
        self.live.order_status_handler = None
        self.live._order_detail_locks = defaultdict(asyncio.Lock)
        self.live._extract_order_id = Mock(return_value='fixture-order')
        self.live.extract_item_id_from_message = Mock(return_value='fixture-item')
        self.live.fetch_order_real_values = AsyncMock(return_value={})
        self.live._save_order_event_snapshot = Mock()
        self.live.fetch_order_detail_info = AsyncMock(return_value={})
        self.live.send_notification = AsyncMock()
        self.live._is_system_or_order_event = Mock(return_value=True)
        self.live.send_post_receipt_thanks = AsyncMock()
        self.live.trigger_buyer_interactions_now = AsyncMock()
        websocket = SimpleNamespace(send=AsyncMock())
        for text in ('[买家确认收货，交易成功]', '快给ta一个评价吧~', '快给ta一个评价吧～'):
            payload = {'1': {'2': 'fixture-chat@goofish', '5': 1, '10': {
                'senderUserId': 'fixture-buyer', 'reminderContent': text,
            }}}
            envelope = {'headers': {}, 'body': {'syncPushPackage': {'data': [{
                'data': base64.b64encode(json.dumps(payload).encode()).decode(),
            }]}}}
            with patch('app.cookie_manager.manager', None):
                await self.live.handle_message(envelope, websocket)
                await asyncio.sleep(0)
        self.assertEqual(self.live.trigger_buyer_interactions_now.await_count, 3)
        self.assertEqual(self.live.send_post_receipt_thanks.await_count, 3)
        self.live._is_system_or_order_event.assert_not_called()
        # 同样的提醒落到另一个本机账号时，不能处理卖家订单。
        self.live.cookie_id = 'fixture-other-account'
        with patch('app.cookie_manager.manager', None):
            await self.live.handle_message(envelope, websocket)
            await asyncio.sleep(0)
        self.assertEqual(self.live.trigger_buyer_interactions_now.await_count, 3)

    async def test_restart_poll_recovers_only_new_completed_thanks_once(self):
        self.live.ws = object()
        self.db.insert_or_update_order('fixture-order', cookie_id='fixture-seller', chat_id='fixture-chat')
        await self.live._run_buyer_interactions(False, False)
        await self.live._run_buyer_interactions(False, False)
        self.live.send_im_text.assert_awaited_once()
        self.assertEqual(self.db.conn.execute("SELECT status FROM buyer_interaction_attempts WHERE action='thanks'").fetchone()[0], 'sent')
        self.assertEqual(self.live._interruptible_sleep.await_count, 1)

    async def test_old_history_missing_chat_or_foreign_buyer_cannot_be_thanked(self):
        self.live.ws = object()
        await self.live._run_buyer_interactions(False, False)
        self.db.insert_or_update_order('fixture-order', cookie_id='fixture-seller', chat_id='fixture-chat')
        self.item['buyerInfoVO']['buyerId'] = 'fixture-other-buyer'
        await self.live._run_buyer_interactions(False, False)
        self.item['buyerInfoVO']['buyerId'] = 'fixture-buyer'
        self.item['commonData']['finishTime'] = '2020-01-01 00:00:00'
        await self.live._run_buyer_interactions(False, False)
        self.live.send_im_text.assert_not_awaited()

    async def test_thanks_receipt_timeout_is_reviewed_never_repeated(self):
        self.live.send_im_text.side_effect = TimeoutError('fixture')
        self.assertFalse(await self.live.send_post_receipt_thanks(None, 'fixture-chat', 'fixture-buyer', 'fixture-order'))
        self.assertFalse(await self.live.send_post_receipt_thanks(None, 'fixture-chat', 'fixture-buyer', 'fixture-order'))
        self.live.send_im_text.assert_awaited_once()
        self.assertEqual(self.db.conn.execute("SELECT status FROM buyer_interaction_attempts WHERE action='thanks'").fetchone()[0], 'needs_review')
