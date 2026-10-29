"""Real automatic-reply ingress/selection with isolated DB and simulated IM receipts."""
import asyncio
import base64
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.db_manager import DBManager
from app.services import auto_reply as replies
from app.services.notification_reconciliation import reconcile_once
from scripts.notification_scenarios import make_instance, setup_account, incoming_packet, ACCOUNT, BUYER, ITEM
from XianyuAutoAsync import XianyuLive, ConnectionState, pause_manager


class AutoReplyFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DBManager(str(Path(self.tmp.name) / 'test.db'))
        self.uid = self.db.get_user_by_username('admin')['id']
        setup_account(self.db, self.uid)
        self.patches = [patch('app.db_manager.db_manager', self.db), patch('XianyuAutoAsync.db_manager', self.db),
                        patch('app.cookie_manager.manager', None)]
        for p in self.patches:p.start()
        self.live = self.instance()
        self.db.save_default_reply(ACCOUNT, True, '配置的回复 {send_user_name}', False, 'https://img.alicdn.com/fixture-reply.png')
        self.msg = {'cid':'notification-fixture-chat','senderId':BUYER,'senderName':'测试买家',
                    'text':'问一下','time':int((time.time()+1)*1000),'messageId':'manual-1','type':'text','itemId':ITEM}

    def instance(self, account=ACCOUNT):
        live = make_instance(self.db)
        live.cookie_id = live.myid = account
        live.message_debounce_delay = 0
        live.background_tasks = set()
        live._schedule_debounced_reply = XianyuLive._schedule_debounced_reply.__get__(live)
        live._get_image_size_from_url = AsyncMock(return_value=(100,200))
        live.get_ai_reply = AsyncMock(return_value=None)
        live.send_im_text = AsyncMock(return_value={'code':200,'body':{'messageId':'auto-text'}})
        live.send_im_image = AsyncMock(return_value={'code':200,'body':{'messageId':'auto-image'}})
        live.send_notification = AsyncMock(return_value=0)
        return live

    async def drain(self, live=None):
        live=live or self.live
        for _ in range(8):
            if not live.background_tasks:break
            results=await asyncio.gather(*list(live.background_tasks),return_exceptions=True)
            for result in results:
                if isinstance(result,Exception):raise result
            await asyncio.sleep(0)
        self.assertFalse(live.background_tasks)

    def last(self, account=ACCOUNT):
        return self.db.get_auto_reply_logs(self.uid,cookie_id=account)[0]

    async def asyncTearDown(self):
        await self.drain()
        pause_manager.paused_chats.clear()
        for p in reversed(self.patches):p.stop()
        self.db.close();self.tmp.cleanup()

    async def test_real_push_default_text_and_image_wait_for_receipts(self):
        await self.live.handle_message(incoming_packet('想了解一下',self.msg['time']),SimpleNamespace(send=AsyncMock()))
        await self.drain()
        self.live.send_im_image.assert_awaited_once()
        self.live.send_im_text.assert_awaited_once()
        self.assertIn('配置的回复',self.live.send_im_text.await_args.args[-1])
        result=self.last()
        self.assertEqual((result['text_send_status'],result['image_send_status'],result['process_status']),('success','success','success'))
        self.assertEqual(result['message_source'],'push')

    def history(self, msg=None):
        msg=msg or self.msg
        model={'message':{'messageId':msg['messageId'],'createAt':msg['time'],
            'extension':{'senderUserId':msg['senderId'],'senderNick':msg['senderName']},
            'content':{'custom':{'data':json.dumps({'contentType':1,'text':{'text':msg['text']}})}}}}
        conv={'singleChatUserConversation':{'modifyTime':msg['time'],
            'singleChatConversation':{'cid':msg['cid']+'@goofish','pairFirst':ACCOUNT,'pairSecond':BUYER,
                                     'extension':json.dumps({'itemId':ITEM})},'lastMessage':model}}
        self.live.get_im_conversations=AsyncMock(return_value={'userConvs':[conv]})
        self.live.get_im_messages=AsyncMock(return_value={'userMessageModels':[model]})

    async def test_missing_push_history_works_with_zero_email_subscriptions(self):
        self.history()
        await reconcile_once(self.live,self.db,now=time.time()+2)
        await self.drain()
        self.live.send_im_text.assert_awaited_once()
        self.live.send_notification.assert_not_awaited()
        self.assertEqual(self.last()['message_source'],'history')

    async def test_push_history_concurrency_restart_and_missing_id_deduplicate(self):
        await asyncio.gather(*[replies.accept(self.live,self.db,self.msg) for _ in range(10)])
        await self.drain()
        await replies.accept(self.live,self.db,{**self.msg,'messageId':''},'history')
        self.history();await reconcile_once(self.live,self.db,now=time.time()+2)
        await self.drain()
        reopened=DBManager(self.db.db_path)
        try:self.assertFalse(await replies.accept(self.instance(),reopened,self.msg,'history'))
        finally:reopened.close()
        self.live.send_im_text.assert_awaited_once();self.live.send_im_image.assert_awaited_once()

    async def test_same_platform_id_with_changed_summary_is_not_sent_twice(self):
        await replies.accept(self.live,self.db,self.msg);await self.drain()
        self.assertFalse(await replies.accept(self.live,self.db,{**self.msg,'text':'摘要有差异'}))
        self.live.send_im_text.assert_awaited_once()

    async def test_old_undated_future_system_and_self_messages_never_reply(self):
        for changes in ({'time':0},{'time':(time.time()-600)*1000},{'time':(time.time()+120)*1000},
                        {'senderId':ACCOUNT},{'text':'[我已付款，等待你发货]'},{'type':'card'}):
            self.assertFalse(await replies.accept(self.live,self.db,{**self.msg,**changes}))
        self.live.send_im_text.assert_not_awaited()

    async def test_enable_watermark_does_not_reply_to_pre_enable_history(self):
        self.db.save_default_reply(ACCOUNT,False,'关闭',False,'')
        before=int(time.time()*1000)-1000
        self.db.save_default_reply(ACCOUNT,True,'刚开启',False,'')
        self.assertFalse(await replies.accept(self.live,self.db,{**self.msg,'time':before}))
        await replies.accept(self.live,self.db,{**self.msg,'time':int((time.time()+1)*1000)})
        await self.drain();self.assertEqual(self.live.send_im_text.await_args.args[-1],'刚开启')

    async def test_latest_saved_settings_and_cross_account_item_reply_isolation(self):
        self.db.update_item_reply(BUYER,ITEM,'其他账号商品文案')
        self.db.save_default_reply(ACCOUNT,True,'最新配置 {item_id}',False,'')
        await replies.accept(self.live,self.db,self.msg);await self.drain()
        self.assertEqual(self.live.send_im_text.await_args.args[-1],f'最新配置 {ITEM}')
        self.assertTrue(self.last()['config_updated_at'])

    async def test_priority_keyword_then_ai_then_scoped_item_then_default(self):
        self.db.update_item_reply(ACCOUNT,ITEM,'本账号商品文案')
        self.db.save_keywords(ACCOUNT,[('问','关键词优先')])
        await replies.accept(self.live,self.db,self.msg);await self.drain()
        self.assertEqual(self.live.send_im_text.await_args.args[-1],'关键词优先')
        self.db.save_keywords(ACCOUNT,[])
        self.live.get_ai_reply.return_value='AI优先'
        await replies.accept(self.live,self.db,{**self.msg,'messageId':'priority2','time':self.msg['time']+1});await self.drain()
        self.assertEqual(self.live.send_im_text.await_args.args[-1],'AI优先')
        self.live.get_ai_reply.return_value=None
        await replies.accept(self.live,self.db,{**self.msg,'messageId':'priority3','time':self.msg['time']+2});await self.drain()
        self.assertEqual(self.live.send_im_text.await_args.args[-1],'本账号商品文案')

    async def test_pause_is_scoped_and_checked_again_after_image_prepare(self):
        from app.services.chat_workspace import set_handover
        set_handover(self.db,BUYER,self.msg['cid'],time.time()+600)
        async def slow_image(*args):
            set_handover(self.db,ACCOUNT,self.msg['cid'],time.time()+600)
            return 100,100
        self.live._get_image_size_from_url.side_effect=slow_image
        await replies.accept(self.live,self.db,self.msg);await self.drain()
        self.live.send_im_image.assert_not_awaited();self.live.send_im_text.assert_awaited_once()
        self.assertEqual(self.last()['decision_reason'],'chat_paused_before_send')

    async def test_existing_pause_and_reply_once_have_explicit_reasons(self):
        from app.services.chat_workspace import set_handover
        set_handover(self.db,ACCOUNT,self.msg['cid'],time.time()+600)
        await replies.accept(self.live,self.db,self.msg);await self.drain()
        self.assertEqual(self.last()['decision_reason'],'chat_paused')
        self.assertGreater(self.last()['remaining_pause_seconds'],0)
        set_handover(self.db,ACCOUNT,self.msg['cid'],0)
        self.db.save_default_reply(ACCOUNT,True,'仅一次',True,'')
        self.db.add_default_reply_record(ACCOUNT,self.msg['cid'])
        await replies.accept(self.live,self.db,{**self.msg,'messageId':'next','time':self.msg['time']+2});await self.drain()
        self.assertEqual(self.last()['decision_reason'],'reply_once')
        self.live.send_im_text.assert_not_awaited()

    async def test_image_prepare_failure_does_not_hide_text_success_or_send_error_to_buyer(self):
        self.live._get_image_size_from_url.side_effect=ValueError('fixture')
        await replies.accept(self.live,self.db,self.msg);await self.drain()
        self.live.send_im_image.assert_not_awaited();self.live.send_im_text.assert_awaited_once()
        self.assertEqual((self.last()['image_send_status'],self.last()['text_send_status']),('failed','success'))
        self.assertEqual(self.last()['decision_reason'],'image_prepare_failed')

    async def test_timeout_unknown_never_retries_and_reserves_single_attempt(self):
        self.db.save_default_reply(ACCOUNT,True,'一次',True,'')
        self.live.send_im_text.side_effect=TimeoutError()
        await replies.accept(self.live,self.db,self.msg);await self.drain()
        self.assertEqual(self.last()['process_status'],'unknown')
        self.assertTrue(self.db.has_default_reply_record(ACCOUNT,self.msg['cid']))
        await replies.accept(self.live,self.db,self.msg,'history');await self.drain()
        self.live.send_im_text.assert_awaited_once()

    async def test_empty_receipt_and_platform_rejection_are_distinct(self):
        self.db.save_default_reply(ACCOUNT,True,'回复',False,'')
        for i,(response,status) in enumerate([({},'unknown'),({'code':500},'failed'),({'code':200,'body':{'reason':'no'}},'failed')]):
            self.live.send_im_text.return_value=response
            await replies.accept(self.live,self.db,{**self.msg,'messageId':f'receipt{i}','time':self.msg['time']+i});await self.drain()
            self.assertEqual(self.last()['process_status'],status)

    async def test_automation_echo_between_owned_accounts_stops_loop_but_manual_message_works(self):
        self.db.save_default_reply(BUYER,True,'小号回复',False,'')
        other=self.instance(BUYER)
        await replies.accept(self.live,self.db,self.msg);await self.drain()
        echo={**self.msg,'senderId':ACCOUNT,'text':'配置的回复 测试买家','messageId':'auto-text'}
        self.assertFalse(await replies.accept(other,self.db,echo))
        other.send_im_text.assert_not_awaited()
        # Same text, new platform ID: a new manual message is allowed.
        self.assertTrue(await replies.accept(other,self.db,{**echo,'messageId':'manual-different'}))
        await self.drain(other);other.send_im_text.assert_awaited_once()

    async def test_keyword_filter_and_auth_pause_zero_send(self):
        self.db.create_message_filter(ACCOUNT,'问','skip_reply',self.uid)
        await replies.accept(self.live,self.db,self.msg);await self.drain()
        self.assertEqual(self.last()['decision_reason'],'skip_reply_filter')
        with patch('app.services.auth_recovery.auth_paused',return_value=True):
            self.assertFalse(await replies.accept(self.live,self.db,{**self.msg,'messageId':'paused'}))
        self.live.send_im_text.assert_not_awaited()

    async def test_debounce_replaces_only_queued_messages(self):
        self.live.message_debounce_delay=.02
        await replies.accept(self.live,self.db,self.msg)
        await asyncio.sleep(.001)
        await replies.accept(self.live,self.db,{**self.msg,'messageId':'last','time':self.msg['time']+1,'text':'最后的问题'})
        await self.drain()
        self.live.send_im_text.assert_awaited_once()
        self.assertEqual(self.last()['source_message'],'最后的问题')

    async def test_new_receipt_tables_store_no_plaintext(self):
        await replies.accept(self.live,self.db,self.msg);await self.drain()
        for table in ('auto_reply_receipts','auto_reply_outbound'):
            rows=str(self.db.conn.execute('SELECT * FROM '+table).fetchall())
            self.assertNotIn(self.msg['text'],rows);self.assertNotIn('配置的回复',rows)

    async def test_fallback_receipt_upgrades_to_platform_id_even_if_later_summary_changes(self):
        first={**self.msg,'messageId':''}
        await replies.accept(self.live,self.db,first);await self.drain()
        self.assertFalse(await replies.accept(self.live,self.db,self.msg,'history'))
        self.assertFalse(await replies.accept(self.live,self.db,{**self.msg,'text':'不同摘要'},'history'))
        self.live.send_im_text.assert_awaited_once()

    async def test_actual_im_request_stays_sending_until_platform_acknowledges(self):
        self.db.save_default_reply(ACCOUNT,True,'真实协议回执',False,'')
        frames=[]
        self.live.ws=SimpleNamespace(closed=False,send=AsyncMock(side_effect=lambda frame:frames.append(json.loads(frame))))
        self.live._im_pending={};self.live._im_request_lock=asyncio.Lock()
        self.live.send_im_text=XianyuLive.send_im_text.__get__(self.live)
        await replies.accept(self.live,self.db,self.msg)
        for _ in range(20):
            if frames:break
            await asyncio.sleep(.001)
        self.assertEqual(self.last()['text_send_status'],'sending')
        self.assertNotEqual(self.last()['process_status'],'success')
        self.live._resolve_im_response({'headers':{'mid':frames[0]['headers']['mid']},'code':200,'body':{'messageId':'real-protocol-id'}})
        await self.drain()
        self.assertEqual(self.last()['text_send_status'],'success')
        self.assertEqual(self.db.conn.execute('SELECT platform_id FROM auto_reply_outbound').fetchone()[0],'real-protocol-id')

    async def test_image_only_and_empty_enabled_config(self):
        self.db.save_default_reply(ACCOUNT,True,'',False,'https://img.alicdn.com/fixture-reply.png')
        await replies.accept(self.live,self.db,self.msg);await self.drain()
        self.live.send_im_image.assert_awaited_once();self.live.send_im_text.assert_not_awaited()
        self.assertEqual(self.last()['process_status'],'success')
        self.db.save_default_reply(ACCOUNT,True,'',False,'')
        await replies.accept(self.live,self.db,{**self.msg,'messageId':'empty','time':self.msg['time']+1});await self.drain()
        self.assertEqual(self.last()['decision_reason'],'empty_reply')

    async def test_disable_during_image_prepare_stops_both_parts(self):
        async def image(*args):
            self.db.save_default_reply(ACCOUNT,False,'停止',False,'')
            return 100,100
        self.live._get_image_size_from_url.side_effect=image
        await replies.accept(self.live,self.db,self.msg);await self.drain()
        self.live.send_im_image.assert_not_awaited();self.live.send_im_text.assert_awaited_once()
        self.assertEqual(self.last()['decision_reason'],'auto_reply_disabled')

    async def test_disabled_incoming_has_clear_reason_without_sending(self):
        self.db.save_default_reply(ACCOUNT,False,'关闭',False,'')
        self.assertFalse(await replies.accept(self.live,self.db,self.msg))
        self.assertEqual(self.last()['decision_reason'],'auto_reply_disabled')
        self.live.send_im_text.assert_not_awaited()

    async def test_interrupted_attempt_is_unknown_after_reopen_and_never_replayed(self):
        key=replies.claim(self.db,ACCOUNT,self.msg)
        self.db.add_auto_reply_log(cookie_id=ACCOUNT,process_status='pending',text_send_status='sending')
        reopened=DBManager(self.db.db_path)
        try:
            self.assertFalse(await replies.accept(self.live,reopened,self.msg))
            self.assertEqual(reopened.get_auto_reply_logs(self.uid)[0]['process_status'],'unknown')
        finally:reopened.close()

    async def test_persistent_automatic_self_echo_does_not_pause_manual_new_message_does(self):
        self.db.save_default_reply(ACCOUNT,True,'固定回复',False,'')
        await replies.accept(self.live,self.db,self.msg);await self.drain()
        # No in-memory programmatic marker: model an echo after a reconnect/restart.
        def packet(mid):
            data={'1':{'2':self.msg['cid']+'@goofish','5':self.msg['time'],
                '10':{'senderUserId':ACCOUNT,'reminderContent':'固定回复','sessionType':'1',
                       'bizTag':json.dumps({'messageId':mid})}},'3':{'itemId':ITEM}}
            return {'headers':{},'body':{'syncPushPackage':{'data':[{'data':base64.b64encode(json.dumps(data).encode()).decode()}]}}}
        await self.live.handle_message(packet('auto-text'),SimpleNamespace(send=AsyncMock()))
        self.assertFalse(pause_manager.is_chat_paused(self.msg['cid'],ACCOUNT))
        await self.live.handle_message(packet('manual-fresh'),SimpleNamespace(send=AsyncMock()))
        self.assertTrue(pause_manager.is_chat_paused(self.msg['cid'],ACCOUNT))

    async def test_replayed_old_manual_echo_does_not_restart_pause(self):
        pause_manager.pause_chat(self.msg['cid'],ACCOUNT,message_time=int((time.time()-1200)*1000))
        self.assertFalse(pause_manager.is_chat_paused(self.msg['cid'],ACCOUNT))

    async def test_config_readback_and_log_access_are_owner_scoped(self):
        from app.reply_server import update_default_reply, get_default_reply, DefaultReplyIn, list_auto_reply_logs
        from fastapi import HTTPException
        response=update_default_reply(ACCOUNT,DefaultReplyIn(enabled=True,reply_content='接口保存',reply_once=False,reply_image_url=''),{'user_id':self.uid})
        self.assertTrue(response['success'])
        self.assertEqual(get_default_reply(ACCOUNT,{'user_id':self.uid})['reply_content'],'接口保存')
        with self.assertRaises(HTTPException):get_default_reply(ACCOUNT,{'user_id':self.uid+1})
        with self.assertRaises(HTTPException):list_auto_reply_logs(cookie_id=ACCOUNT,current_user={'user_id':self.uid+1})
