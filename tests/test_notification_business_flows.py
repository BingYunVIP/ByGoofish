"""Formal event handlers, reconciliation, SMTP filtering and durable dedup."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from app.db_manager import DBManager
from app.notification_events import EVENT_IDS
from app.services.notification_reconciliation import reconcile_once, claim, message_key
from scripts.notification_scenarios import make_instance, setup_account, run_scenario, incoming_packet, ACCOUNT, BUYER


class NotificationBusinessFlows(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DBManager(str(Path(self.tmp.name) / 'fixture.db'))
        self.owner = self.db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        setup_account(self.db, self.owner)
        self.channel = self.db.create_notification_channel('fixture', 'email', json.dumps({
            'smtp_server':'smtp.example.test','smtp_port':465,'email_user':'sender@example.test',
            'email_password':'fixture','recipient_email':'recipient@example.test'}), self.owner)
        self.rule = self.db.create_notification_rule(ACCOUNT, self.channel, event_types=EVENT_IDS)
        self.smtp = patch('app.services.smtp_delivery.send_message')
        self.sent = self.smtp.start()
        self.db_patch = patch('app.db_manager.db_manager', self.db)
        self.db_patch.start()
        self.manager_patch = patch('app.cookie_manager.manager', None)
        self.manager_patch.start()
        self.live = make_instance(self.db)

    async def asyncTearDown(self):
        self.manager_patch.stop(); self.db_patch.stop(); self.smtp.stop()
        self.db.close(); self.tmp.cleanup()

    async def test_all_eleven_business_scenarios_emit_once_without_channel_test(self):
        for event in EVENT_IDS:
            with self.subTest(event=event):
                before = self.sent.call_count
                with patch.object(self.db, "get_account_notifications", wraps=self.db.get_account_notifications) as lookup:
                    await run_scenario(event, self.live, self.db, self.owner)
                self.assertEqual(lookup.call_args.kwargs["event_type"], event)
                self.assertEqual(self.sent.call_count - before, 1)
                message = self.sent.call_args.args[0]
                self.assertTrue(message.get_payload())
        self.assertEqual(self.sent.call_count, len(EVENT_IDS))

    async def test_each_unsubscribed_event_causes_zero_smtp_calls(self):
        self.db.update_notification_rule(self.rule, event_types=[])
        for event in EVENT_IDS:
            await run_scenario(event, make_instance(self.db), self.db, self.owner)
        self.sent.assert_not_called()

    async def test_disabled_rule_and_channel_cause_zero_send(self):
        self.db.update_notification_rule(self.rule, enabled=False)
        await run_scenario('buyer_message', self.live, self.db, self.owner)
        self.sent.assert_not_called()
        self.db.update_notification_rule(self.rule, enabled=True)
        self.db.conn.execute('UPDATE notification_channels SET enabled=0 WHERE id=?', (self.channel,));self.db.conn.commit()
        await run_scenario('buyer_message', self.live, self.db, self.owner)
        self.sent.assert_not_called()

    async def test_push_duplicate_and_repeated_text_new_timestamp(self):
        packet = incoming_packet('fixture', int(time.time()*1000))
        from types import SimpleNamespace
        ws = SimpleNamespace(send=AsyncMock())
        await self.live.handle_message(packet, ws)
        await self.live.handle_message(packet, ws)
        self.assertEqual(self.sent.call_count, 1)
        await self.live.handle_message(incoming_packet('fixture', int(time.time()*1000)+1000), ws)
        self.assertEqual(self.sent.call_count, 2)

    async def test_failed_smtp_is_not_success_or_automatically_resent(self):
        self.sent.side_effect = TimeoutError()
        result = await self.live.send_notification('buyer', BUYER, 'fixture', chat_id='cid', message_time=123)
        self.assertEqual(result, 0)
        self.assertEqual(self.db.conn.execute('SELECT status FROM buyer_notification_receipts').fetchone()[0], 'failed_or_unknown')
        await self.live.send_notification('buyer', BUYER, 'fixture', chat_id='cid', message_time=123)
        self.assertEqual(self.sent.call_count, 1)

    async def test_account_isolation_and_hash_only_persistence(self):
        self.assertTrue(claim(self.db, ACCOUNT, 'cid', BUYER, 'private-message', 123))
        self.assertTrue(claim(self.db, BUYER, 'cid', BUYER, 'private-message', 123))
        self.assertFalse(claim(self.db, ACCOUNT, 'cid', BUYER, 'private-message', 123))
        self.assertNotIn('private-message', str(self.db.conn.execute('SELECT * FROM buyer_notification_receipts').fetchall()))
        self.assertEqual(message_key('cid@goofish', BUYER, 'x', 123), message_key('cid', BUYER, 'x', 123))

    def history(self, now, *, self_message=False, old=False):
        stamp = int((now - (500 if old else 1))*1000)
        model = {'message': {'messageId':'fixture-im', 'createAt':stamp,
            'extension': {'senderUserId': ACCOUNT if self_message else BUYER, 'senderNick':'fixture'},
            'content': {'custom': {'data': json.dumps({'contentType':1, 'text':{'text':'new incoming'}})}}}}
        conv = {'singleChatUserConversation': {'modifyTime':int(now*1000),
            'singleChatConversation': {'cid':'cid@goofish', 'pairFirst':ACCOUNT,'pairSecond':BUYER},
            'lastMessage': model}}
        self.live.get_im_conversations = AsyncMock(return_value={'userConvs':[conv]})
        self.live.get_im_messages = AsyncMock(return_value={'data':{'userMessageModels':[model]}})

    async def test_missing_push_reconciles_and_restart_does_not_duplicate(self):
        now = time.time();self.history(now)
        self.db.conn.execute('INSERT INTO buyer_notification_activation VALUES(?,?)', (ACCOUNT, now-10));self.db.conn.commit()
        self.assertEqual(await reconcile_once(self.live, self.db, now),1)
        self.assertEqual(await reconcile_once(self.live, self.db, now),0)
        self.assertEqual(self.sent.call_count,1)
        self.assertEqual(self.live.get_im_conversations.call_count,2)

    async def test_first_enable_old_history_outgoing_and_auth_pause_do_not_notify(self):
        now=time.time();self.history(now)
        self.assertEqual(await reconcile_once(self.live,self.db,now),0)
        self.live.get_im_conversations.assert_not_called()
        for options in ({'old':True},{'self_message':True}):
            self.history(now+2,**options)
            self.assertEqual(await reconcile_once(self.live,self.db,now+2),0)
        with patch('app.services.auth_recovery.auth_paused',return_value=True):
            self.live.get_im_conversations.reset_mock()
            await reconcile_once(self.live,self.db,now+3)
            self.live.get_im_conversations.assert_not_called()
        self.sent.assert_not_called()

    async def test_logistics_wrong_owner_rejected_without_email(self):
        from app.routers.logistics_quote import create_logistics_quote_router, QuoteParseRequest
        from fastapi import HTTPException
        endpoint = create_logistics_quote_router(lambda:None,self.db).routes[0].endpoint
        with self.assertRaises(HTTPException) as e:
            await endpoint(QuoteParseRequest(text='bad',cookie_id=ACCOUNT),{'user_id':self.owner+1})
        self.assertEqual(e.exception.status_code,404)
        self.sent.assert_not_called()

    async def test_concurrent_push_and_history_same_message_only_one_submission(self):
        stamp = int(time.time()*1000)
        await asyncio.gather(*[self.live.send_notification('fixture',BUYER,'same',chat_id='cid',message_time=stamp) for _ in range(8)])
        self.assertEqual(self.sent.call_count,1)

    async def test_receipt_survives_reopen_and_expired_rows_are_pruned(self):
        claim(self.db,ACCOUNT,'cid',BUYER,'text',123)
        reopened = DBManager(self.db.db_path)
        try:
            self.assertFalse(claim(reopened,ACCOUNT,'cid',BUYER,'text',123))
        finally:
            reopened.close()
        self.db.conn.execute('UPDATE buyer_notification_receipts SET created_at=?',(time.time()-8*86400,));self.db.conn.commit()
        self.assertTrue(claim(self.db,ACCOUNT,'cid',BUYER,'text',123))
        self.assertEqual(self.db.conn.execute('SELECT count(*) FROM buyer_notification_receipts').fetchone()[0],1)

    async def test_disabled_subscription_does_not_poll_or_claim(self):
        self.db.update_notification_rule(self.rule,event_types=[])
        self.live.get_im_conversations=AsyncMock()
        self.assertEqual(await reconcile_once(self.live,self.db),0)
        self.live.get_im_conversations.assert_not_called()
        self.assertEqual(self.db.conn.execute('SELECT count(*) FROM buyer_notification_receipts').fetchone()[0],0)

    async def test_platform_order_notice_does_not_generate_buyer_email(self):
        from types import SimpleNamespace
        await self.live.handle_message(incoming_packet('[买家确认收货，交易成功]'),SimpleNamespace(send=AsyncMock()))
        self.sent.assert_not_called()

    async def test_history_failure_never_uses_cached_text_as_new_message(self):
        now=time.time();self.history(now)
        self.db.conn.execute('INSERT INTO buyer_notification_activation VALUES(?,?)',(ACCOUNT,now-10));self.db.conn.commit()
        self.live.get_im_messages=AsyncMock(return_value={'reason':'request limited'})
        with self.assertRaises(RuntimeError):
            await reconcile_once(self.live,self.db,now)
        self.sent.assert_not_called()

    async def test_nested_rate_limit_response_is_error(self):
        from app.services.notification_reconciliation import payload
        with self.assertRaises(RuntimeError):
            payload({'data':{'code':429}})

    async def test_bounded_history_reads_rotate_across_six_active_threads(self):
        now=time.time();self.history(now)
        raw=self.live.get_im_conversations.return_value['userConvs'][0]
        import copy
        rows=[]
        for i in range(6):
            item=copy.deepcopy(raw)
            item['singleChatUserConversation']['singleChatConversation']['cid']=f'cid{i}@goofish'
            rows.append(item)
        self.live.get_im_conversations.return_value={'userConvs':rows}
        self.live.get_im_messages=AsyncMock(return_value={'userMessageModels':[]})
        self.db.conn.execute('INSERT INTO buyer_notification_activation VALUES(?,?)',(ACCOUNT,now-10));self.db.conn.commit()
        await reconcile_once(self.live,self.db,now)
        self.assertEqual(self.live.get_im_messages.call_count,5)
        await reconcile_once(self.live,self.db,now+60)
        seen=[c.args[0] for c in self.live.get_im_messages.call_args_list]
        self.assertIn('cid5',seen)
        self.assertEqual(len(seen),10)
