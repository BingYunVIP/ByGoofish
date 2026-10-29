"""Native reply/forward wire shapes, authorization and offline emoji caching."""
import asyncio
import base64
import copy
import io
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from app.db_manager import DBManager
from app.services import message_actions as actions,chat_emojis
from app.services import chat_workspace as store
from app.routers.chat_workspace import create_chat_workspace_router
from app.xianyu_im import parse_message, parse_conversation, hydrate_replies
from XianyuAutoAsync import ConnectionState


def model(mid='source',text='官方表情[微笑]'):
    return {'message':{'messageId':mid,'cid':'source-chat@goofish','sender':'buyer@goofish','createAt':1700000000000,
        'content':actions.custom_content(text=text),'extension':{'senderUserId':'buyer','senderNick':'测试买家'}}}


class NativeProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.live=SimpleNamespace(myid='seller',cookie_id='seller',get_im_messages=AsyncMock(return_value={'userMessageModels':[model()],'hasMore':False}),_send_im_request=AsyncMock(return_value={'code':200,'body':{'messageId':'reply'}}))

    async def test_native_reference_has_platform_id_and_original_content(self):
        source=await actions.resolve_source(self.live,'source-chat','source','buyer')
        await actions.send_reply(self.live,'source-chat','buyer',source,text='原生回复')
        lwp,body=self.live._send_im_request.await_args.args
        self.assertEqual(lwp,'/r/MessageSend/sendByReceiverScope')
        quote=body[0]['content'];self.assertEqual(quote['contentType'],101)
        self.assertEqual(body[0]['extension']['replyMessageId'],'source')
        self.assertEqual(json.loads(body[0]['extension']['extJson'])['replyMessageId'],'source')
        self.assertRegex(body[0]['uuid'],r'^-\d+$')
        parsed=parse_message({'message':{'messageId':'reply','sender':'seller@goofish','createAt':1700000001000,'content':quote,'extension':body[0]['extension']}},'seller')
        self.assertEqual(parsed['type'],'text');self.assertEqual(parsed['text'],'原生回复')
        self.assertEqual(parsed['reply']['messageId'],'source')
        original=parse_message(model(),'seller');hydrate_replies([original,parsed])
        self.assertEqual(parsed['reply']['text'],'官方表情[微笑]')
        self.assertFalse(parsed['canReference'])
        conv=parse_conversation({'singleChatConversation':{'cid':'source-chat','pairFirst':'seller','pairSecond':'buyer'},'lastMessage':{'message':{'messageId':'reply','sender':'seller','content':quote,'extension':body[0]['extension']}},'modifyTime':1700000001000},'seller')
        self.assertEqual(conv['lastMessageSummary'],'原生回复');self.assertEqual(conv['lastMessageSenderId'],'seller')

    async def test_native_image_reply_and_image_reference_parse(self):
        source=model();source['message']['content']=actions.custom_content(image={'url':'https://img.alicdn.com/source.png','width':100,'height':200})
        self.live.get_im_messages.return_value={'userMessageModels':[source]}
        resolved=await actions.resolve_source(self.live,'source-chat','source','buyer')
        await actions.send_reply(self.live,'source-chat','buyer',resolved,text='引用图片的文字回复')
        body=self.live._send_im_request.await_args.args[1]
        parsed=parse_message({'message':{'messageId':'new','sender':'seller','content':body[0]['content'],'extension':body[0]['extension']}},'seller')
        self.assertEqual(parsed['text'],'引用图片的文字回复')
        hydrate_replies([parse_message(source,'seller'),parsed])
        self.assertEqual(parsed['reply']['images'],['https://img.alicdn.com/source.png'])
        with self.assertRaises(HTTPException):
            await actions.send_reply(self.live,'source-chat','buyer',resolved,image={'url':'https://img.alicdn.com/reply.png'})

    async def test_forwarding_a_native_quote_keeps_app_reference_extension(self):
        source=await actions.resolve_source(self.live,'source-chat','source','buyer')
        await actions.send_reply(self.live,'source-chat','buyer',source,text='功能验收引用')
        sent=self.live._send_im_request.await_args.args[1][0]
        row=model('quoted');row['message']['content']=sent['content'];row['message']['extension']=sent['extension']
        self.live.get_im_messages.return_value={'userMessageModels':[row]}
        with self.assertRaises(HTTPException):await actions.resolve_source(self.live,'source-chat','quoted','buyer')
        resolved=await actions.resolve_source(self.live,'source-chat','quoted','buyer',allow_quote=True)
        self.live._send_im_request.return_value={'code':200,'body':{}}
        with self.assertRaises(TimeoutError):await actions.forward(self.live,'target','target-buyer',resolved)
        forwarded=self.live._send_im_request.await_args.args[1][0]['forwardMessages'][0]
        self.assertEqual(forwarded['extension']['replyMessageId'],'source')
        decoded=json.loads(base64.b64decode(forwarded['content']['custom']['data']))
        self.assertEqual(decoded['text']['text'],'功能验收引用')

    async def test_app_reference_only_in_ext_json_and_missing_source_are_not_guessed(self):
        row=model('reply','原生引用样本');row['message']['extension']['extJson']=json.dumps({'replyMessageId':'source'})
        parsed=parse_message(row,'seller');self.assertEqual(parsed['reply']['type'],'unknown')
        hydrate_replies([parse_message(model('other','不同消息'),'seller'),parsed])
        self.assertEqual(parsed['reply']['text'],'原消息未加载')
        hydrate_replies([parse_message(model(),'seller'),parsed])
        self.assertEqual(parsed['reply']['messageId'],'source');self.assertEqual(parsed['reply']['senderId'],'buyer')

    async def test_unknown_source_id_is_never_synthesized(self):
        with self.assertRaises(HTTPException) as e:await actions.resolve_source(self.live,'source-chat','missing','buyer')
        self.assertEqual(e.exception.status_code,404);self.live._send_im_request.assert_not_awaited()

    async def test_wrong_cid_and_participant_rejected(self):
        for changes in ({'cid':'other-chat'},{'sender':'intruder','extension':{'senderUserId':'intruder'}}):
            row=model();row['message'].update(changes);self.live.get_im_messages.return_value={'userMessageModels':[row]}
            with self.assertRaises(HTTPException) as e:await actions.resolve_source(self.live,'source-chat','source','buyer')
            self.assertEqual(e.exception.status_code,403)

    async def test_order_events_and_unknown_custom_types_are_not_forwarded(self):
        for row in (model(text='[我已付款，等待你发货]'),model()):
            if row['message']['content']['custom']['data']==model()['message']['content']['custom']['data']:
                row['message']['content']={'contentType':101,'custom':{'data':json.dumps({'dxCard':{'item':{'main':{'exContent':{'title':'卡片'}}}}})}}
            self.live.get_im_messages.return_value={'userMessageModels':[row]}
            with self.assertRaises(HTTPException):await actions.resolve_source(self.live,'source-chat','source','buyer')

    async def test_source_paging_is_bounded_at_five(self):
        self.live.get_im_messages.side_effect=[{'userMessageModels':[],'hasMore':True,'nextCursor':i} for i in range(6)]
        with self.assertRaises(HTTPException):await actions.resolve_source(self.live,'source-chat','missing','buyer')
        self.assertEqual(self.live.get_im_messages.await_count,5)

    async def test_native_forward_requires_per_message_receipt(self):
        source=await actions.resolve_source(self.live,'source-chat','source','buyer')
        async def frame(lwp,body):
            self.assertEqual(lwp,'/r/MessageForward/batchForward')
            self.assertRegex(body[0]['forwardMessages'][0]['uuid'],r'^-\d+$')
            payload=body[0];self.assertEqual(payload['toCid'],'target@goofish');self.assertEqual(len(payload['forwardMessages']),1)
            entry=payload['forwardMessages'][0];self.assertEqual(entry['extension'],{})
            return {'code':200,'body':{'sendResults':{entry['uuid']:{'messageId':'forwarded'}}}}
        self.live._send_im_request.side_effect=frame
        self.assertEqual((await actions.forward(self.live,'target','target-buyer',source))['body']['messageId'],'forwarded')
        self.live._send_im_request.side_effect=None;self.live._send_im_request.return_value={'code':200,'body':{'sendResults':{}}}
        with self.assertRaises(TimeoutError):await actions.forward(self.live,'target','target-buyer',source)


class MessageActionRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=DBManager(str(Path(self.tmp.name)/'actions.db'))
        self.uid=self.db.get_user_by_username('admin')['id'];self.db.save_cookie('seller','unb=fixture',self.uid)
        store.observe_conversations(self.db,self.uid,'seller',[{'cid':'source-chat','otherUserId':'buyer'},{'cid':'target-chat','otherUserId':'other-buyer'}])
        self.live=SimpleNamespace(myid='seller',cookie_id='seller',get_im_messages=AsyncMock(return_value={'userMessageModels':[model()]}),_send_im_request=AsyncMock())
        self.sends=0
        async def wire(lwp,body):
            self.sends+=1
            return {'code':200,'body':{'sendResults':{body[0]['forwardMessages'][0]['uuid']:{'messageId':'forwarded'}}}}
        self.live._send_im_request.side_effect=wire
        async def run(account,op):return await op(self.live)
        def owned(account,user):
            if account not in self.db.get_all_cookies(user['user_id']):raise HTTPException(403,'owner')
        app=FastAPI();app.include_router(create_chat_workspace_router(self.db,lambda:{'user_id':self.uid},owned,run))
        self.client=TestClient(app)
        self.db.conn.execute("UPDATE chat_native_capabilities SET status='verified',reason=''");self.db.conn.commit()
        self.patches=[patch('app.db_manager.db_manager',self.db),patch('app.reply_server.db_manager',self.db),patch('app.services.auth_recovery.auth_paused',return_value=False)]
        for p in self.patches:p.start()
        self.payload={'source_cid':'source-chat','message_id':'source','target_cid':'target-chat','client_request_id':'test-forward-id'}

    def tearDown(self):
        for p in self.patches:p.stop()
        self.client.close();self.db.close();self.tmp.cleanup()

    def test_forward_dedup_different_target_conflict_and_no_body_persistence(self):
        for _ in range(2):self.assertTrue(self.client.post('/chat/forward/seller',json=self.payload).json()['success'])
        self.assertEqual(self.sends,1)
        changed={**self.payload,'target_cid':'source-chat'}
        self.assertEqual(self.client.post('/chat/forward/seller',json=changed).status_code,409)
        self.assertNotIn('官方表情',str(self.db.conn.execute('select * from chat_send_attempts').fetchall()))

    def test_foreign_account_unknown_target_and_forged_participant_rejected(self):
        self.assertEqual(self.client.post('/chat/forward/foreign',json=self.payload).status_code,403)
        self.assertEqual(self.client.post('/chat/forward/seller',json={**self.payload,'target_cid':'not-loaded'}).status_code,404)
        self.assertEqual(self.sends,0)

    def test_timeout_unknown_is_never_repeated(self):
        self.live._send_im_request.side_effect=TimeoutError()
        for _ in range(2):self.assertEqual(self.client.post('/chat/forward/seller',json=self.payload).json()['data']['status'],'unknown')
        self.assertEqual(self.live._send_im_request.await_count,1)

    def test_unverified_native_actions_are_disabled_for_customer_targets(self):
        self.db.conn.execute("UPDATE chat_native_capabilities SET status='unverified'");self.db.conn.commit()
        caps=self.client.get('/chat/capabilities/seller').json()
        self.assertFalse(caps['reply_text']['supported'])
        self.assertEqual(self.client.post('/chat/forward/seller',json=self.payload).status_code,409)
        self.assertEqual(self.sends,0)

    def test_only_owned_second_account_may_receive_an_unverified_probe(self):
        from app.services.native_capabilities import require
        self.db.conn.execute("UPDATE chat_native_capabilities SET status='unverified'");self.db.conn.commit()
        self.db.save_cookie('buyer','unb=fixture',self.uid)
        require(self.db,self.uid,'buyer','reply_text')
        with self.assertRaises(HTTPException):require(self.db,self.uid,'other-buyer','reply_text')

    def test_protocol_diagnostics_keep_code_but_not_cookie_or_entire_response(self):
        from app.services.native_capabilities import record
        secret='x'*24
        record(self.db,'seller','diagnostic-fixture',{'code':400,'body':{'reason':'cookie2='+secret,'payload':'never retain this body'}})
        row=self.db.conn.execute('SELECT code,reason FROM chat_action_diagnostics').fetchone()
        self.assertEqual(row[0],'400');self.assertNotIn(secret,row[1]);self.assertNotIn('payload',str(row))

    def test_reply_api_ignores_client_source_fields_and_hashes_reference_id(self):
        from app import reply_server
        from app.reply_server import ChatSendMessageRequest
        async def run(account,operation,**kwargs):return await operation(self.live)
        self.live._send_im_request.side_effect=None
        self.live._send_im_request.return_value={'code':200,'body':{'messageId':'quote-result'}}
        body=ChatSendMessageRequest(cid='source-chat',to_user_id='buyer',text='原生引用',reply_to_message_id='source',client_request_id='reply-request-id')
        with patch.object(reply_server,'_run_on_account_loop',run):
            for _ in range(2):
                result=asyncio.run(reply_server.send_chat_message('seller',body,{'user_id':self.uid}))
                self.assertTrue(result['success'])
        self.assertEqual(self.live._send_im_request.await_count,1)
        with patch.object(reply_server,'_run_on_account_loop',run):
            forged=body.model_copy(update={'reply_to_message_id':'nonexistent','client_request_id':'new-forged-reference'})
            with self.assertRaises(HTTPException):asyncio.run(reply_server.send_chat_message('seller',forged,{'user_id':self.uid}))
        self.assertEqual(self.live._send_im_request.await_count,1)

    def test_cached_emoji_directory_is_owner_scoped_and_never_contacts_platform(self):
        self.db.conn.execute('insert into chat_emoji_cache values(?,?,?,0)',('seller',json.dumps([{'alias':'[微笑]','name':'微笑','url':'/static/chat-emojis/fixture.png'}]),time.time()));self.db.conn.commit()
        self.assertEqual(self.client.get('/chat/emojis/seller').json()['items'][0]['alias'],'[微笑]')
        self.assertEqual(self.client.get('/chat/emojis/other').status_code,403)
        self.assertEqual(self.sends,0)


class EmojiCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=DBManager(str(Path(self.tmp.name)/'emojis.db'))
        uid=self.db.get_user_by_username('admin')['id'];self.db.save_cookie('emoji-fixture','unb=fixture',uid)
        self.live=SimpleNamespace(cookie_id='emoji-fixture',cookies_str='unb=fixture',connection_state=ConnectionState.CONNECTED)
        self.patch=patch('app.services.auth_recovery.auth_paused',return_value=False);self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop();self.db.close();self.tmp.cleanup()

    async def test_failure_retains_existing_official_names_with_six_hour_retry(self):
        items=[{'alias':'[微笑]','name':'微笑','url':''}]
        self.db.conn.execute('insert into chat_emoji_cache values(?,?,?,0)',('emoji-fixture',json.dumps(items),time.time()-90000));self.db.conn.commit()
        with patch('utils.xianyu_seller_api.XianyuSellerAPI._call',AsyncMock(side_effect=TimeoutError())) as call:
            self.assertEqual((await chat_emojis.refresh(self.live,self.db))['items'],items)
            self.assertEqual((await chat_emojis.refresh(self.live,self.db))['items'],items)
            call.assert_awaited_once()

    async def test_auth_pause_keeps_cache_without_a_platform_request(self):
        with patch('app.services.auto_reply.available',return_value=False),patch('utils.xianyu_seller_api.XianyuSellerAPI._call',AsyncMock()) as call:
            await chat_emojis.refresh(self.live,self.db);call.assert_not_awaited()
