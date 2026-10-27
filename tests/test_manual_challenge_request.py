"""人工入口签名轮换只补签一次；业务码可见，凭证和响应正文不可见。"""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from utils.manual_captcha import _fetch_live_verification_url, _challenge_response


class ChallengeRequestTests(unittest.IsolatedAsyncioTestCase):
    def context(self, payloads, cookies=None, status=200):
        responses=[SimpleNamespace(status=status,json=AsyncMock(return_value=payload),dispose=AsyncMock()) for payload in payloads]
        context=SimpleNamespace(request=SimpleNamespace(post=AsyncMock(side_effect=responses)),
                                cookies=AsyncMock(side_effect=cookies or [[{'name':'_m_h5_tk','value':'old_1'}] for _ in payloads]))
        return context,responses

    async def fetch(self,context, diagnostic=None):
        outcome={}
        with patch('utils.xianyu_utils.generate_sign',return_value='fixture-sign') as signing, \
             patch('utils.xianyu_utils.generate_device_id',return_value='fixture-device'):
            result=await _fetch_live_verification_url('fixture','unb=fixture; _m_h5_tk=old_1',context=context,outcome=outcome,diagnostic=diagnostic)
        return result,outcome,signing

    async def test_expired_signature_rotated_then_fresh_challenge(self):
        url='https://h5api.m.goofish.com/punish?x5secdata=fixture-private'
        context,responses=self.context([
            {'ret':['FAIL_SYS_TOKEN_EXOIRED::令牌过期'],'data':{}},
            {'ret':['FAIL_SYS_USER_VALIDATE::请验证'],'data':{'url':url}},
        ],cookies=[[{'name':'_m_h5_tk','value':'new_2'}],[{'name':'_m_h5_tk','value':'new_2'}]])
        result,outcome,signing=await self.fetch(context)
        self.assertEqual(result,url)
        self.assertNotIn('failure_category',outcome)
        self.assertEqual(context.request.post.await_count,2)
        self.assertEqual([call.args[1] for call in signing.call_args_list],['old','new'])
        for response in responses:response.dispose.assert_awaited_once()

    async def test_expired_signature_rotated_then_valid_token_continues_connection_check(self):
        context,_=self.context([{'ret':['FAIL_SYS_TOKEN_EXOIRED'],'data':{}},
                                {'ret':['SUCCESS::调用成功'],'data':{'accessToken':'fixture-private-token'}}],
                               cookies=[[{'name':'_m_h5_tk','value':'new_2'}],[{'name':'_m_h5_tk','value':'new_2'}]])
        result,outcome,_=await self.fetch(context)
        self.assertIsNone(result)
        self.assertTrue(outcome['already_authenticated'])
        self.assertNotIn('accessToken',outcome)

    async def test_signing_expired_without_actual_rotation_never_retries(self):
        context,_=self.context([{'ret':['FAIL_SYS_TOKEN_EXOIRED'],'data':{}}])
        _,outcome,_=await self.fetch(context)
        self.assertEqual(context.request.post.await_count,1)
        self.assertEqual(outcome['failure_category'],'signing_token_expired')

    async def test_repeated_rotation_cannot_loop(self):
        context,_=self.context([{'ret':['FAIL_SYS_TOKEN_EXOIRED'],'data':{}}]*2,
                               cookies=[[{'name':'_m_h5_tk','value':'new_2'}],[{'name':'_m_h5_tk','value':'newer_3'}]])
        _,outcome,_=await self.fetch(context)
        self.assertEqual(context.request.post.await_count,2)
        self.assertEqual(outcome['failure_category'],'signing_token_expired')

    async def test_session_expired_is_terminal_even_with_signing_expired_and_rotated(self):
        context,_=self.context([{'ret':['FAIL_SYS_TOKEN_EXOIRED','FAIL_SYS_SESSION_EXPIRED::Session过期'],'data':{}}],
                               cookies=[[{'name':'_m_h5_tk','value':'new_2'}]])
        _,outcome,_=await self.fetch(context)
        self.assertEqual(context.request.post.await_count,1)
        self.assertEqual(outcome['failure_category'],'login_session_expired')
        self.assertIn('扫码',outcome['message'])

    async def test_human_challenge_without_url_does_not_restart_authentication(self):
        context,_=self.context([{'ret':['FAIL_SYS_USER_VALIDATE','FAIL_SYS_TOKEN_EXOIRED'],'data':{}}],
                               cookies=[[{'name':'_m_h5_tk','value':'new_2'}]])
        _,outcome,_=await self.fetch(context)
        self.assertEqual(context.request.post.await_count,1)
        self.assertEqual(outcome['failure_category'],'challenge_unavailable')

    async def test_http_failure_is_not_success_even_if_body_contains_token(self):
        context,_=self.context([{'ret':['SUCCESS'],'data':{'accessToken':'fixture-private-token'}}],status=403)
        _,outcome,_=await self.fetch(context)
        self.assertNotIn('already_authenticated',outcome)
        self.assertEqual(outcome['failure_category'],'challenge_request_failed')

    async def test_missing_signature_does_not_send_network_request(self):
        context,_=self.context([])
        outcome={}
        await _fetch_live_verification_url('fixture','unb=fixture',context=context,outcome=outcome)
        context.request.post.assert_not_awaited()
        self.assertEqual(outcome['failure_category'],'signing_token_missing')

    async def test_explicit_manual_recovery_initializes_missing_signature_before_signed_token(self):
        context, responses = self.context([
            {'ret': ['FAIL_SYS_TOKEN_EMPTY'], 'data': {}},
            {'ret': ['SUCCESS'], 'data': {'accessToken': 'fixture-private-token'}},
        ], cookies=[[{'name': '_m_h5_tk', 'value': 'new_2'},
                     {'name': '_m_h5_tk_enc', 'value': 'fixture-enc'}],
                    [{'name': '_m_h5_tk', 'value': 'new_2'},
                     {'name': '_m_h5_tk_enc', 'value': 'fixture-enc'}]])
        outcome = {}
        verified = []
        await _fetch_live_verification_url('fixture', 'unb=fixture; cookie2=fixture-session',
            context=context, outcome=outcome, token_sink=verified.append)
        self.assertTrue(outcome['already_authenticated'])
        self.assertEqual(len(verified), 1)
        self.assertNotIn('fixture-private-token', str(outcome))
        calls = context.request.post.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn('/mtop.idle.web.user.page.head/', calls[0].args[0])
        self.assertIn('/mtop.taobao.idlemessage.pc.login.token/', calls[1].args[0])
        for response in responses:
            response.dispose.assert_awaited_once()

    async def test_safe_codes_and_shape_without_response_body_or_cookies(self):
        self.assertEqual(_challenge_response({'ret':['FAIL_SYS_TOKEN_EXOIRED::private-cookie-contents'],'data':{}}),(['FAIL_SYS_TOKEN_EXOIRED'],{}))
        self.assertEqual(_challenge_response({'ret':['token=private-value'],'data':{}}),(['UNRECOGNIZED_CODE'],{}))
        self.assertEqual(_challenge_response({'ret':'wrong','data':{}}),(['INVALID_RESPONSE'],{}))
        self.assertEqual(_challenge_response({'ret':[],'data':{}}),(['INVALID_RESPONSE'],{}))
        diagnostic=SimpleNamespace(secrets=(),stage=MagicMock(),emit=MagicMock())
        context,_=self.context([{'ret':['FAIL_SYS_REJECTED::private-response-body'],'data':{'unrelated':'private-response-body'}}],
                               cookies=[[{'name':'_m_h5_tk','value':'new-private-signing-value'}]])
        _,outcome,_=await self.fetch(context,diagnostic)
        emitted=str(diagnostic.emit.call_args_list)
        self.assertIn('FAIL_SYS_REJECTED',emitted)
        self.assertNotIn('private-response-body',emitted)
        self.assertNotIn('new-private-signing-value',emitted)
        self.assertIn('new-private-signing-value',diagnostic.secrets)
        self.assertEqual(outcome['failure_category'],'challenge_unavailable')

    async def test_request_error_exposes_only_exception_type_and_disposes_response(self):
        context,responses=self.context([{}])
        responses[0].json.side_effect=ValueError('private-response-body')
        diagnostic=SimpleNamespace(secrets=(),stage=MagicMock(),emit=MagicMock())
        _,outcome,_=await self.fetch(context,diagnostic)
        self.assertEqual(outcome['failure_category'],'challenge_request_failed')
        self.assertNotIn('private-response-body',str(diagnostic.emit.call_args_list))
        responses[0].dispose.assert_awaited_once()
