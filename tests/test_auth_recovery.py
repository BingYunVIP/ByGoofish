"""人工优先认证恢复：持久化、单次探测、会话取消与远端注册确认。"""
import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from app.db_manager import DBManager
from app.services import auth_recovery as recovery
from app import reply_server as server
from utils import risk_control, browser_limit
from utils import xianyu_slider_stealth as slider
from XianyuAutoAsync import XianyuLive, ConnectionState


class RecoveryDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, 'recovery.db')
        self.db = DBManager(self.path)
        self.owner = self.db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        self.db.save_cookie('fixture-recovery', 'unb=fixture; _m_h5_tk=fake_1', self.owner)
        self.user = {'user_id': self.owner, 'username': 'fixture'}
        self.patches = [patch('app.db_manager.db_manager', self.db), patch.object(server, 'db_manager', self.db),
                        patch.object(recovery, 'default_db', return_value=self.db),
                        patch.object(risk_control, 'registry', risk_control.RiskControlRegistry())]
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: self.db.close())
        server._manual_captcha_tasks.clear()
        server._manual_captcha_cancelled.clear()

    def make_live(self):
        instance = XianyuLive.__new__(XianyuLive)
        instance.cookie_id = 'fixture-recovery'
        instance.user_id = self.owner
        instance.cookies_str = self.db.get_cookie(instance.cookie_id)
        instance.cookies = {'unb': 'fixture'}
        instance.current_token = None
        instance.max_captcha_verification_count = 3
        instance.last_message_received_time = 0
        instance.message_cookie_refresh_cooldown = 60
        instance.device_id = 'fixture-device'
        instance.last_token_refresh_time = 0
        instance.token_refresh_interval = 3600
        instance.connection_state = ConnectionState.CONNECTING
        instance.ws = None
        instance.needs_relogin = False
        instance.relogin_reason = ''
        instance.send_token_refresh_notification = AsyncMock()
        return instance

    def mock_http(self, payload):
        response = MagicMock()
        response.status = 200
        response.headers = {'content-type': 'application/json'}
        response.json = AsyncMock(return_value=payload)
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.post.return_value = response
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        return patch('XianyuAutoAsync.aiohttp.ClientSession', return_value=session), session

    def require(self):
        return recovery.require_manual('fixture-recovery', 'message_verification_required')

    def test_restart_preserves_requirement_and_interrupts_active_sessions(self):
        self.require()
        self.db.close()
        self.db = DBManager(self.path)
        self.assertEqual(recovery.get(self.db, 'fixture-recovery')['recovery_state'], 'required')
        recovery.begin_verification(self.db, 'fixture-recovery', check_only=True)
        recovery.initialize(self.db)
        self.assertEqual(recovery.get(self.db, 'fixture-recovery')['failure_category'], 'message_verification_required')
        recovery.set_state(self.db, 'fixture-recovery', 'checking')
        recovery.initialize(self.db)
        self.assertEqual(recovery.get(self.db, 'fixture-recovery')['recovery_state'], 'required')

    def test_only_one_verification_and_one_token_probe_per_attempt(self):
        self.require()
        active = recovery.begin_verification(self.db, 'fixture-recovery', check_only=True)
        self.assertIsNone(recovery.begin_verification(self.db, 'fixture-recovery', check_only=True))
        checking = recovery.set_state(self.db, 'fixture-recovery', 'checking', expected_attempt=active['attempt_id'])
        self.assertTrue(recovery.claim_token_probe(self.db, 'fixture-recovery'))
        self.assertFalse(recovery.claim_token_probe(self.db, 'fixture-recovery'))
        self.assertIsNone(recovery.set_state(self.db, 'fixture-recovery', 'required', expected_attempt=active['attempt_id']))
        self.assertEqual(recovery.get(self.db, 'fixture-recovery')['attempt_id'], checking['attempt_id'])

    def test_page_error_blocks_repeated_manual_challenge_until_credentials_update(self):
        recovery.require_manual('fixture-recovery', 'challenge_page_error')
        current = recovery.get(self.db, 'fixture-recovery')
        self.assertFalse(current['can_start_verification'])
        self.assertIsNone(recovery.begin_verification(self.db, 'fixture-recovery'))
        status = server.get_risk_control_status(self.user)['accounts'][0]
        self.assertFalse(status['can_start_verification'])
        self.assertIn('官网消息页', status['verification_message'])
        self.assertTrue(status['can_check_connection'])

        from utils.manual_captcha import open_manual_session
        with patch('utils.manual_captcha.open_manual_session', new=AsyncMock()) as opened:
            with self.assertRaises(HTTPException) as error:
                asyncio.run(server.start_manual_captcha(
                    'fixture-recovery', 60, 'page-error-retry', self.user))
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn('账号卡片', error.exception.detail)
        opened.assert_not_awaited()

        saved = recovery.save_explicit_credentials(
            self.db, 'fixture-recovery',
            lambda: self.db.update_cookie_account_info(
                'fixture-recovery', cookie_value='official-login-fixture'))
        self.assertTrue(saved)
        updated = recovery.get(self.db, 'fixture-recovery')
        self.assertEqual(updated['recovery_state'], 'checking')
        self.assertEqual(updated['failure_category'], 'challenge_page_error')
        self.assertFalse(updated['can_start_verification'])

    def test_reset_and_expired_cooldown_cannot_clear_manual_pause(self):
        self.require()
        guard = risk_control.registry.get('fixture-recovery')
        guard.blocked_until = time.monotonic() - 1
        guard.reset()
        self.assertTrue(guard.is_blocked)
        factory = MagicMock()
        with self.assertRaises(risk_control.RiskControlBlocked):
            asyncio.run(risk_control.guarded_call('fixture-recovery', factory))
        factory.assert_not_called()

    def test_platform_response_is_logged_once_without_automatic_solver_or_name_error(self):
        live = self.make_live()
        http, session = self.mock_http({'ret': ['FAIL_SYS_USER_VALIDATE'], 'data': {'url': 'https://example.test/punish?fixture=private'}})
        with http, patch('utils.xianyu_slider_stealth.XianyuSliderStealth') as automatic:
            self.assertIsNone(asyncio.run(live.refresh_token()))
            self.assertIsNone(asyncio.run(live.refresh_token()))
        automatic.assert_not_called()
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(live.last_token_refresh_status, 'manual_verification_required')
        self.assertEqual(risk_control.registry.get(live.cookie_id).consecutive_hits, 1)
        logs = self.db.get_risk_control_logs(cookie_id=live.cookie_id)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['error_message'], 'message_verification_required')
        self.assertNotIn('private', json.dumps(logs))

    def test_expired_session_durably_pauses_without_repeated_password_or_token_requests(self):
        live = self.make_live()
        live.current_token = 'fixture-old-token'
        http, session = self.mock_http({'ret': ['FAIL_SYS_SESSION_EXPIRED::Session过期'], 'data': {}})
        with http, patch.object(live, '_try_password_login_refresh', new=AsyncMock()) as password:
            self.assertIsNone(asyncio.run(live.refresh_token()))
            self.assertIsNone(asyncio.run(live.refresh_token()))
        self.assertEqual(recovery.status(live.cookie_id)['failure_category'], 'login_session_expired')
        self.assertTrue(live.needs_relogin)
        self.assertIsNone(live.current_token)
        self.assertEqual(session.post.call_count, 1)
        password.assert_not_awaited()
        recovery.initialize(self.db)
        self.assertEqual(recovery.status(live.cookie_id)['recovery_state'], 'required')

    def test_plain_throttling_is_not_misreported_as_human_verification(self):
        live = self.make_live()
        self.assertFalse(live._need_captcha_verification({'ret': ['RGV587_ERROR::请稍后重试'], 'data': {}}))
        self.assertTrue(live._need_captcha_verification({'data': {'url': 'https://example.test/punish'}}))

    def test_old_probe_cannot_consume_a_new_recovery_attempt(self):
        first = recovery.set_state(self.db, 'fixture-recovery', 'checking')
        second = recovery.set_state(self.db, 'fixture-recovery', 'checking')
        live = self.make_live()
        with patch('XianyuAutoAsync.aiohttp.ClientSession') as http:
            self.assertIsNone(asyncio.run(live.refresh_token(_recovery_attempt=first['attempt_id'])))
        http.assert_not_called()
        self.assertEqual(recovery.status(live.cookie_id)['attempt_id'], second['attempt_id'])
        self.assertFalse(recovery.status(live.cookie_id)['token_claimed'])

    def test_paused_account_does_not_start_password_or_cookie_refresh(self):
        self.require()
        live = self.make_live()
        with patch('utils.xianyu_slider_stealth.run_password_login', new=AsyncMock()) as login:
            self.assertFalse(asyncio.run(live._try_password_login_refresh()))
            self.assertFalse(asyncio.run(live._refresh_cookies_via_browser()))
            self.assertFalse(asyncio.run(live._execute_cookie_refresh(time.time())))
            self.assertFalse(asyncio.run(live._verify_cookie_validity())['valid'])
        login.assert_not_awaited()

    def test_client_identity_survives_instance_replacement_and_database_reopen(self):
        from app.services.account_auth_client import device_id
        with patch.object(XianyuLive, '_register_instance'):
            first = XianyuLive(self.db.get_cookie('fixture-recovery'), 'fixture-recovery', self.owner)
            second = XianyuLive(self.db.get_cookie('fixture-recovery'), 'fixture-recovery', self.owner)
        self.assertEqual(first.device_id, second.device_id)
        self.assertFalse(first.cookie_refresh_enabled)
        self.assertEqual(first.device_id, device_id(self.db, 'fixture-recovery', 'fixture'))
        other = DBManager(self.path)
        try:
            self.assertEqual(first.device_id, device_id(other, 'fixture-recovery', 'fixture'))
        finally:
            other.close()

    def test_client_identity_separates_accounts_and_is_removed_with_account(self):
        from app.services.account_auth_client import device_id
        self.db.save_cookie('fixture-client-b', 'unb=other', self.owner)
        self.assertNotEqual(device_id(self.db,'fixture-recovery','fixture'),
                            device_id(self.db,'fixture-client-b','other'))
        self.db.delete_cookie('fixture-client-b')
        self.assertIsNone(self.db.conn.execute('SELECT 1 FROM account_auth_clients WHERE cookie_id=?',
                                              ('fixture-client-b',)).fetchone())

    def test_cookie_compare_and_swap_preserves_new_credentials_and_account_binding(self):
        from app.services.account_auth_client import save_cookie_delta
        before = self.db.get_cookie('fixture-recovery')
        self.db.update_cookie_account_info('fixture-recovery', cookie_value='unb=fixture; _m_h5_tk=updated_2')
        self.assertIsNone(save_cookie_delta(self.db,'fixture-recovery',before,{'_m_h5_tk':'stale_3'}))
        current = self.db.get_cookie('fixture-recovery')
        self.assertIsNone(save_cookie_delta(self.db,'fixture-recovery',current,{'unb':'different-account'}))
        self.assertEqual(current,self.db.get_cookie('fixture-recovery'))
        saved = save_cookie_delta(self.db,'fixture-recovery',current,{'_m_h5_tk':'rotated_4'})
        self.assertIn('rotated_4',saved)
        self.assertEqual(saved,self.db.get_cookie('fixture-recovery'))

    def test_rotated_credentials_update_manager_cache_without_instance_restart(self):
        from app.services.account_auth_client import save_cookie_delta
        before=self.db.get_cookie('fixture-recovery')
        manager=SimpleNamespace(cookies={'fixture-recovery':before},update_cookie=MagicMock())
        with patch('app.cookie_manager.manager',manager):
            value=save_cookie_delta(self.db,'fixture-recovery',before,{'_m_h5_tk':'rotated_2'})
        self.assertEqual(manager.cookies['fixture-recovery'],value)
        manager.update_cookie.assert_not_called()

    def test_websocket_handshake_uses_same_user_agent_as_token_and_registration(self):
        from utils.user_agents import CHROME_UA
        live=self.make_live()
        live.base_url='ws://example.test/socket'
        original={'User-Agent':'old-version','user-agent':'duplicate-version','Cookie':'fixture'}
        with patch('websockets.connect',return_value='fixture-connect') as connect:
            self.assertEqual(asyncio.run(live._create_websocket_connection(original)),'fixture-connect')
        headers=connect.call_args.kwargs['extra_headers']
        self.assertEqual([(k,v) for k,v in headers.items() if k.lower()=='user-agent'],[('User-Agent',CHROME_UA)])
        self.assertEqual(original['User-Agent'],'old-version')

    def test_token_request_reloads_actual_database_value_field(self):
        live = self.make_live()
        live.needs_relogin = True
        live.relogin_reason = 'fixture old login failure'
        fresh = 'unb=fixture; _m_h5_tk=updated_2'
        self.db.update_cookie_account_info('fixture-recovery',cookie_value=fresh)
        http, session = self.mock_http({'ret':['SUCCESS::调用成功'],'data':{'accessToken':'fixture-token'}})
        with http:
            self.assertEqual(asyncio.run(live.refresh_token()),'fixture-token')
        self.assertEqual(session.post.call_args.kwargs['headers']['cookie'],fresh)
        self.assertFalse(live.needs_relogin)
        self.assertEqual(live.relogin_reason,'')

    def test_old_token_challenge_cannot_override_user_updated_credentials(self):
        live = self.make_live()
        http, session = self.mock_http({})
        async def replaced():
            self.db.update_cookie_account_info('fixture-recovery',cookie_value='unb=fixture; _m_h5_tk=user-updated_2')
            return {'ret':['FAIL_SYS_USER_VALIDATE'],'data':{'url':'https://example.test/punish'}}
        session.post.return_value.json.side_effect = replaced
        with http:
            self.assertIsNone(asyncio.run(live.refresh_token()))
        self.assertEqual(live.last_token_refresh_status,'stale_credentials')
        self.assertFalse(recovery.auth_paused('fixture-recovery'))
        self.assertIn('user-updated_2',self.db.get_cookie('fixture-recovery'))

    def test_concurrent_token_refreshes_coalesce_to_one_request(self):
        async def run():
            live = self.make_live()
            async def fetch(*args):
                await asyncio.sleep(.02)
                live.current_token = 'fixture-token'
                live.last_token_refresh_time = time.time()
                return live.current_token
            live._refresh_token_attempt = AsyncMock(side_effect=fetch)
            values = await asyncio.gather(*(live.refresh_token() for _ in range(5)))
            self.assertEqual(values,['fixture-token']*5)
            live._refresh_token_attempt.assert_awaited_once()
        asyncio.run(run())

    def test_optional_browser_and_token_refresh_are_serial_and_preserve_heartbeat(self):
        async def run():
            live = self.make_live()
            live.cookie_refresh_lock = asyncio.Lock()
            live.heartbeat_task = MagicMock()
            live._verify_cookie_validity = AsyncMock()
            entered, release = asyncio.Event(), asyncio.Event()
            async def browse():
                entered.set()
                await release.wait()
                return True
            live._refresh_cookies_via_browser = AsyncMock(side_effect=browse)
            live._refresh_token_attempt = AsyncMock(return_value='fixture-token')
            browser_task = asyncio.create_task(live._execute_cookie_refresh(time.time()))
            await entered.wait()
            token_task = asyncio.create_task(live.refresh_token())
            await asyncio.sleep(.02)
            live._refresh_token_attempt.assert_not_awaited()
            release.set()
            self.assertTrue(await browser_task)
            self.assertEqual(await token_task,'fixture-token')
            live.heartbeat_task.cancel.assert_not_called()
            live._verify_cookie_validity.assert_not_awaited()
        asyncio.run(run())

    def test_signing_retry_requires_actual_rotation_and_never_reacquires_lock(self):
        from multidict import CIMultiDict
        async def run():
            live = self.make_live()
            http, session = self.mock_http({})
            response = session.post.return_value
            response.headers = CIMultiDict({'content-type':'application/json','set-cookie':'_m_h5_tk=rotated_2; Path=/'})
            response.json.side_effect = [{'ret':['FAIL_SYS_TOKEN_EXOIRED'],'data':{}},
                                        {'ret':['SUCCESS::调用成功'],'data':{'accessToken':'fixture-token'}}]
            with http:
                self.assertEqual(await asyncio.wait_for(live.refresh_token(),2),'fixture-token')
            self.assertEqual(session.post.call_count,2)
            self.assertIn('rotated_2',session.post.call_args.kwargs['headers']['cookie'])
        asyncio.run(run())

    def test_signing_expired_without_rotation_only_requests_once(self):
        live = self.make_live()
        http, session = self.mock_http({'ret':['FAIL_SYS_TOKEN_EXOIRED'],'data':{}})
        with http:
            self.assertIsNone(asyncio.run(live.refresh_token()))
        self.assertEqual(session.post.call_count,1)

    def test_missing_signing_cookie_never_sends_unsigned_authentication_request(self):
        self.db.update_cookie_account_info('fixture-recovery',cookie_value='unb=fixture; cookie2=fixture-session')
        live=self.make_live()
        http,session=self.mock_http({})
        with http:
            self.assertIsNone(asyncio.run(live.refresh_token()))
        session.post.assert_not_called()
        self.assertEqual(recovery.status('fixture-recovery')['failure_category'],'signing_token_missing')

    def test_manual_resigning_reuses_persisted_device_identity(self):
        from utils.manual_captcha import _fetch_live_verification_url
        from app.services.account_auth_client import device_id
        expected = device_id(self.db,'fixture-recovery','fixture')
        responses = [SimpleNamespace(status=200,json=AsyncMock(return_value={'ret':['FAIL_SYS_TOKEN_EXOIRED'],'data':{}}),dispose=AsyncMock()),
                     SimpleNamespace(status=200,json=AsyncMock(return_value={'ret':['SUCCESS'],'data':{'accessToken':'fixture-token'}}),dispose=AsyncMock())]
        context = SimpleNamespace(request=SimpleNamespace(post=AsyncMock(side_effect=responses)),
                                  cookies=AsyncMock(return_value=[{'name':'_m_h5_tk','value':'rotated_2'}]))
        asyncio.run(_fetch_live_verification_url('fixture-recovery',self.db.get_cookie('fixture-recovery'),context=context,outcome={}))
        self.assertEqual(context.request.post.await_count,2)
        identities=[json.loads(call.kwargs['form']['data'])['deviceId'] for call in context.request.post.call_args_list]
        self.assertEqual(identities,[expected,expected])

    def test_required_account_pauses_product_and_order_jobs_without_changing_settings(self):
        async def run():
            self.require()
            for name in ('item_sync_loop','order_sync_loop','item_polish_loop'):
                live = self.make_live()
                calls = 0
                async def wait(_):
                    nonlocal calls
                    calls += 1
                    if calls > (0 if name=='item_sync_loop' else 1):
                        raise asyncio.CancelledError
                live._interruptible_sleep = AsyncMock(side_effect=wait)
                with patch.object(self.db,'get_system_setting',side_effect=AssertionError('paused job read settings')):
                    with self.assertRaises(asyncio.CancelledError):
                        await getattr(live,name)()
        asyncio.run(run())

    def test_old_user_backup_cannot_rotate_an_existing_client_identity(self):
        from app.services.account_auth_client import device_id
        from app.services.user_backup import export_user, import_user
        expected = device_id(self.db,'fixture-recovery','fixture')
        payload = export_user(self.db.conn,self.owner)
        part = payload['data']['account_auth_clients']
        part['rows'][0][part['columns'].index('device_id')] = 'fixture-old-device'
        import_user(self.db.conn,payload,self.owner)
        self.assertEqual(expected,device_id(self.db,'fixture-recovery','fixture'))

    def test_virtual_hour_keeps_required_token_refresh_and_disables_periodic_browser(self):
        async def run():
            clock = [0.0]
            with patch.object(XianyuLive,'_register_instance'):
                live = XianyuLive(self.db.get_cookie('fixture-recovery'),'fixture-recovery',self.owner)
            live.token_refresh_interval = 3600
            live.last_token_refresh_time = 0
            live.ws = None
            async def wait(seconds):
                clock[0] += seconds
            async def token():
                self.assertEqual(clock[0],3600)
                return 'fixture-token'
            live._interruptible_sleep = AsyncMock(side_effect=wait)
            live.refresh_token = AsyncMock(side_effect=token)
            with patch('XianyuAutoAsync.time.time',side_effect=lambda:clock[0]):
                await live.token_refresh_loop()
            live.refresh_token.assert_awaited_once()
            live._execute_cookie_refresh = AsyncMock()
            clock[0] = 0
            async def wait_disabled(seconds):
                clock[0] += seconds
                if clock[0]>=3600:
                    raise asyncio.CancelledError
            live._interruptible_sleep = AsyncMock(side_effect=wait_disabled)
            with patch('XianyuAutoAsync.time.time',side_effect=lambda:clock[0]):
                with self.assertRaises(asyncio.CancelledError):
                    await live.cookie_refresh_loop()
            live._execute_cookie_refresh.assert_not_awaited()
        asyncio.run(run())

    def test_pause_during_browser_navigation_blocks_subrequests_and_cookie_write(self):
        async def run():
            live = self.make_live()
            live.last_qr_cookie_refresh_time = 0
            live.qr_cookie_refresh_cooldown = 0
            live._get_playwright_launch_options = MagicMock(return_value={})
            live.update_config_cookies = AsyncMock()
            live._async_close_browser = AsyncMock()
            page = MagicMock()
            context = MagicMock()
            context.route = AsyncMock()
            context.add_cookies = AsyncMock()
            context.new_page = AsyncMock(return_value=page)
            context.cookies = AsyncMock()
            browser = MagicMock()
            browser.new_context = AsyncMock(return_value=context)
            playwright = MagicMock()
            starter = MagicMock(start=AsyncMock(return_value=playwright))
            async def navigate(*args, **kwargs):
                self.require()  # 模拟其他Token任务在导航等待时报告平台验证要求。
                route = SimpleNamespace(abort=AsyncMock(), continue_=AsyncMock())
                await context.route.call_args.args[1](route)
                route.abort.assert_awaited_once()
                route.continue_.assert_not_awaited()
            page.goto = AsyncMock(side_effect=navigate)
            page.reload = AsyncMock()
            with patch('playwright.async_api.async_playwright', return_value=starter), \
                 patch.object(browser_limit, 'launch_browser', new=AsyncMock(return_value=browser)):
                before = live.cookies_str
                self.assertFalse(await live._refresh_cookies_via_browser())
            page.reload.assert_not_awaited()
            context.cookies.assert_not_awaited()
            live.update_config_cookies.assert_not_awaited()
            live._async_close_browser.assert_awaited_once()
            self.assertEqual(live.cookies_str, before)
        asyncio.run(run())

    def test_pause_during_refresh_skips_validation_upload(self):
        async def run():
            live = self.make_live()
            live.cookie_refresh_lock = asyncio.Lock()
            live.heartbeat_task = None
            async def refresh():
                self.require()
                return True
            live._refresh_cookies_via_browser = AsyncMock(side_effect=refresh)
            live._verify_cookie_validity = AsyncMock()
            await live._execute_cookie_refresh(time.time())
            live._verify_cookie_validity.assert_not_awaited()
        asyncio.run(run())

    def test_paused_token_loop_never_enters_refresh(self):
        async def run():
            self.require()
            live = self.make_live()
            live.refresh_token = AsyncMock()
            live._interruptible_sleep = AsyncMock(side_effect=asyncio.CancelledError)
            with self.assertRaises(asyncio.CancelledError):
                await live.token_refresh_loop()
            live.refresh_token.assert_not_awaited()
        asyncio.run(run())

    def test_token_success_alone_does_not_mark_recovered_or_repeat_probe(self):
        self.require()
        recovery.set_state(self.db, 'fixture-recovery', 'checking')
        live = self.make_live()
        http, session = self.mock_http({'ret': ['SUCCESS::调用成功'], 'data': {'accessToken': 'fixture-token'}})
        with http:
            self.assertEqual(asyncio.run(live.refresh_token()), 'fixture-token')
            self.assertIsNone(asyncio.run(live.refresh_token()))
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(recovery.status(live.cookie_id)['recovery_state'], 'checking')

    def test_probe_failure_returns_to_manual_wait(self):
        recovery.set_state(self.db, 'fixture-recovery', 'checking')
        live = self.make_live()
        http, session = self.mock_http({'ret': ['FAIL_BIZ_FIXTURE'], 'data': {}})
        with http:
            self.assertIsNone(asyncio.run(live.refresh_token()))
            self.assertIsNone(asyncio.run(live.refresh_token()))
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(recovery.status(live.cookie_id)['failure_category'], 'token_probe_failed')

    def test_websocket_ack_required_before_recovered_and_pushes_are_preserved(self):
        async def exercise():
            recovery.set_state(self.db, 'fixture-recovery', 'checking')
            live = self.make_live()
            live.current_token = 'fixture-token'
            live.last_token_refresh_time = time.time()
            ws = SimpleNamespace(send=AsyncMock())
            push = json.dumps({'body': {'fixturePush': True}})
            messages = [push]
            async def recv():
                if messages:
                    return messages.pop(0)
                first = json.loads(ws.send.call_args_list[0].args[0])
                return json.dumps({'code': 200, 'headers': {'mid': first['headers']['mid']}})
            ws.recv = recv
            await live.init(ws)
            self.assertEqual(recovery.status(live.cookie_id)['recovery_state'], 'recovered')
            self.assertEqual(live._recovery_prefetched_messages, [push])
        asyncio.run(exercise())

    def test_rejected_websocket_registration_does_not_mark_recovered(self):
        async def exercise():
            recovery.set_state(self.db, 'fixture-recovery', 'checking')
            live = self.make_live()
            live.current_token = 'fixture-token'
            live.last_token_refresh_time = time.time()
            ws = SimpleNamespace(send=AsyncMock())
            async def recv():
                first = json.loads(ws.send.call_args_list[0].args[0])
                return json.dumps({'code': 401, 'headers': {'mid': first['headers']['mid']}})
            ws.recv = recv
            with self.assertRaises(ConnectionError):
                await live.init(ws)
            self.assertNotEqual(recovery.status(live.cookie_id)['recovery_state'], 'recovered')
        asyncio.run(exercise())

    def test_scheduled_reconnect_also_requires_correlated_registration_ack(self):
        async def run():
            live = self.make_live()
            live.current_token = 'fixture-token'
            live.last_token_refresh_time = time.time()
            ws = SimpleNamespace(send=AsyncMock())
            async def recv():
                reg = json.loads(ws.send.call_args_list[0].args[0])
                self.assertEqual(reg['headers']['ua'], __import__('utils.user_agents',fromlist=['CHROME_UA']).CHROME_UA)
                return json.dumps({'code':200,'headers':{'mid':reg['headers']['mid']}})
            ws.recv = AsyncMock(side_effect=recv)
            await live.init(ws)
            ws.recv.assert_awaited_once()
        asyncio.run(run())

    def test_regular_registration_auth_rejection_pauses_future_authentication(self):
        async def run():
            live = self.make_live()
            live.current_token = 'fixture-token'
            live.last_token_refresh_time = time.time()
            ws = SimpleNamespace(send=AsyncMock())
            async def recv():
                reg=json.loads(ws.send.call_args_list[0].args[0])
                return json.dumps({'code':403,'headers':{'mid':reg['headers']['mid']}})
            ws.recv=AsyncMock(side_effect=recv)
            with self.assertRaises(ConnectionError):
                await live.init(ws)
            self.assertTrue(recovery.auth_paused(live.cookie_id))
            self.assertEqual(recovery.status(live.cookie_id)['failure_category'],'registration_rejected')
        asyncio.run(run())

    def make_manager(self, enabled=True):
        instance = SimpleNamespace(cookies_str='old', cookies={'old': 'fixture'}, current_token='stale')
        return SimpleNamespace(instances={'fixture-recovery': instance}, cookies={},
                               get_cookie_status=MagicMock(return_value=enabled), update_cookie=MagicMock(), update_cookie_status=MagicMock())

    def test_manual_verified_token_is_private_and_new_worker_registers_without_token_request(self):
        from app.services.account_auth_client import VerifiedToken, device_id
        from app.cookie_manager import CookieManager
        self.require()
        manager = self.make_manager()
        secret_token = 'fixture-handoff-private-token'
        verified = VerifiedToken('fixture-recovery',device_id(self.db,'fixture-recovery','fixture'),secret_token,time.time())
        result = {'success':True,'cookies_str':self.db.get_cookie('fixture-recovery'),
                  'message':'fixture','session_id':'fixture-recovery','verified_auth':verified}
        with patch.object(server.cookie_manager,'manager',manager), \
             patch('utils.manual_captcha.open_manual_session',new=AsyncMock(return_value=result)):
            response=asyncio.run(server.start_manual_captcha('fixture-recovery',60,'handoff-request',self.user, check_only=True))
        handoff=manager.update_cookie.call_args.kwargs['verified_auth']
        self.assertNotIn(secret_token,json.dumps(response))
        self.assertNotIn(secret_token,repr(handoff))
        self.assertNotIn(secret_token,'\n'.join(self.db.conn.iterdump()))
        async def run():
            live=self.make_live()
            live.device_id=verified.device_id
            live.refresh_token=AsyncMock(side_effect=AssertionError('duplicate token request'))
            ws=SimpleNamespace(send=AsyncMock())
            async def recv():
                reg=json.loads(ws.send.call_args_list[0].args[0])
                self.assertEqual(reg['headers']['token'],secret_token)
                return json.dumps({'code':200,'headers':{'mid':reg['headers']['mid']}})
            ws.recv=AsyncMock(side_effect=recv)
            async def main():
                await live.init(ws)
            live.main=AsyncMock(side_effect=main)
            worker=CookieManager.__new__(CookieManager)
            worker.instances={}
            with patch('app.cookie_manager.db_manager',self.db), \
                 patch('XianyuAutoAsync.XianyuLive',return_value=live):
                await worker._run_xianyu('fixture-recovery',live.cookies_str,self.owner,verified_auth=handoff)
            # The worker catches its exceptions; checking state proves init really ran.
            self.assertEqual(recovery.status('fixture-recovery')['recovery_state'],'recovered')
            live.refresh_token.assert_not_awaited()
        asyncio.run(run())

    def test_verified_handoff_rejects_stale_or_mismatched_context_without_claim(self):
        import hashlib
        from dataclasses import replace
        from app.services.account_auth_client import VerifiedToken, seed_verified_token
        for changes in ({'issued_at':time.time()-61},{'device_id':'different-device'},
                        {'recovery_attempt':'old-attempt'},{'cookie_id':'different-account'},
                        {'cookie_digest':'old-cookie'}):
            state=recovery.set_state(self.db,'fixture-recovery','checking')
            live=self.make_live()
            verified=VerifiedToken(live.cookie_id,live.device_id,'fixture-private-token',time.time(),
                state['attempt_id'],hashlib.sha256(live.cookies_str.encode()).hexdigest())
            self.assertFalse(seed_verified_token(self.db,live,replace(verified,**changes)))
            self.assertFalse(recovery.get(self.db,live.cookie_id)['token_claimed'])
            self.assertIsNone(live.current_token)

    def test_manual_in_progress_cannot_consume_new_worker_probe(self):
        recovery.set_state(self.db,'fixture-recovery','checking')
        live=self.make_live()
        live.manual_captcha_in_progress=True
        self.assertIsNone(asyncio.run(live.refresh_token()))
        self.assertFalse(recovery.get(self.db,live.cookie_id)['token_claimed'])
        self.assertEqual(recovery.status(live.cookie_id)['recovery_state'],'checking')

    def test_rejected_handoff_unregisters_worker_before_main(self):
        from app.cookie_manager import CookieManager
        from app.services.account_auth_client import VerifiedToken
        state = recovery.set_state(self.db, 'fixture-recovery', 'checking')
        live = self.make_live()
        live.main = AsyncMock()
        live._unregister_instance = MagicMock()
        verified = VerifiedToken(live.cookie_id, live.device_id, 'fixture-token',
                                 time.time() - 61, state['attempt_id'])
        worker = CookieManager.__new__(CookieManager)
        worker.instances = {}
        with patch('app.cookie_manager.db_manager', self.db), \
             patch('XianyuAutoAsync.XianyuLive', return_value=live):
            asyncio.run(worker._run_xianyu(live.cookie_id, live.cookies_str,
                                         self.owner, verified_auth=verified))
        live.main.assert_not_awaited()
        live._unregister_instance.assert_called_once()
        self.assertNotIn(live.cookie_id, worker.instances)
        self.assertEqual(recovery.status(live.cookie_id)['recovery_state'], 'required')

    def test_manual_and_scheduled_requests_share_body_and_http_headers(self):
        from app.services.account_auth_client import device_id
        from utils.manual_captcha import _fetch_live_verification_url
        live=self.make_live()
        live.device_id=device_id(self.db,'fixture-recovery','fixture')
        payload={'ret':['SUCCESS::调用成功'],'data':{'accessToken':'fixture-token'}}
        http, session=self.mock_http(payload)
        with http:
            asyncio.run(live.refresh_token())
        context=SimpleNamespace(request=SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(
            status=200,json=AsyncMock(return_value=payload),dispose=AsyncMock()))),
            cookies=AsyncMock(return_value=[{'name':'_m_h5_tk','value':'fake_1'}]))
        received=[]
        asyncio.run(_fetch_live_verification_url('fixture-recovery',live.cookies_str,context=context,
                    outcome={},token_sink=received.append))
        normal=session.post.call_args.kwargs;manual=context.request.post.call_args.kwargs
        self.assertEqual(normal['data']['data'],manual['form']['data'])
        self.assertEqual({k:v for k,v in normal['headers'].items() if k!='cookie'},manual['headers'])
        self.assertEqual(received[0].token,'fixture-token')
        self.assertEqual(received[0].device_id,live.device_id)

    def test_non_success_http_response_never_hands_off_a_token(self):
        from utils.manual_captcha import _fetch_live_verification_url
        context=SimpleNamespace(request=SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(
            status=403,json=AsyncMock(return_value={'ret':['SUCCESS'],'data':{'accessToken':'fixture-invalid-token'}}),
            dispose=AsyncMock()))),cookies=AsyncMock(return_value=[]))
        received=[]
        asyncio.run(_fetch_live_verification_url('fixture-recovery',self.db.get_cookie('fixture-recovery'),
                    context=context,outcome={},token_sink=received.append))
        self.assertEqual(received,[])

    def test_manual_success_updates_both_cookie_forms_but_waits_for_connection(self):
        self.require()
        manager = self.make_manager()
        result = {'success': True, 'cookies_str': 'unb=fixture; x5sec=pass', 'message': 'fixture', 'session_id': 'fixture-recovery'}
        with patch.object(server.cookie_manager, 'manager', manager), patch('utils.manual_captcha.open_manual_session', new=AsyncMock(return_value=result)):
            response = asyncio.run(server.start_manual_captcha('fixture-recovery', 60, 'request-success', self.user, check_only=True))
        self.assertEqual(response['recovery_state'], 'checking')
        self.assertEqual(manager.instances['fixture-recovery'].cookies['x5sec'], 'pass')
        self.assertIsNone(manager.instances['fixture-recovery'].current_token)
        self.assertEqual(recovery.status('fixture-recovery')['recovery_state'], 'checking')
        manager.update_cookie.assert_called_once()

    def test_replaced_credentials_invalidate_late_manual_result(self):
        self.require()
        manager = self.make_manager()
        async def replace_during_verification(*args, **kwargs):
            recovery.save_explicit_credentials(self.db, 'fixture-recovery',
                lambda: self.db.save_cookie('fixture-recovery', 'unb=fixture; cookie2=new-user-session', self.owner))
            return {'success': True, 'cookies_str': 'unb=fixture; cookie2=stale-session', 'message': 'fixture'}
        with patch.object(server.cookie_manager, 'manager', manager), patch('utils.manual_captcha.open_manual_session', side_effect=replace_during_verification):
            result = asyncio.run(server.start_manual_captcha('fixture-recovery', 60, 'old-request', self.user, check_only=True))
        self.assertFalse(result['success'])
        self.assertIn('new-user-session', self.db.get_cookie('fixture-recovery'))
        self.assertEqual(recovery.status('fixture-recovery')['recovery_state'], 'checking')
        manager.update_cookie.assert_not_called()

    def test_scanning_existing_account_waits_for_signing_fields_before_probe(self):
        self.require()
        manager = self.make_manager()
        with patch.object(server.cookie_manager, 'manager', manager):
            result = asyncio.run(server.process_qr_login_cookies('unb=fixture; cookie2=new-scanned-session', 'fixture', self.user))
        self.assertFalse(result['is_new_account'])
        self.assertEqual(recovery.status('fixture-recovery')['recovery_state'], 'verifying')
        manager.update_cookie.assert_called_once()

    def test_disabled_account_stays_disabled_after_manual_verification(self):
        self.require()
        manager = self.make_manager(enabled=False)
        result = {'success': True, 'cookies_str': 'unb=fixture; x5sec=pass', 'message': 'fixture'}
        with patch.object(server.cookie_manager, 'manager', manager), patch('utils.manual_captcha.open_manual_session', new=AsyncMock(return_value=result)):
            response = asyncio.run(server.start_manual_captcha('fixture-recovery', 60, 'disabled', self.user, check_only=True))
        self.assertTrue(response['success'])
        manager.update_cookie.assert_not_called()
        self.assertEqual(recovery.status('fixture-recovery')['failure_category'], 'account_disabled')

    def test_enabling_only_resumes_credentials_already_verified_while_disabled(self):
        manager = self.make_manager(enabled=False)
        recovery.require_manual('fixture-recovery', 'account_disabled')
        with patch.object(server.cookie_manager, 'manager', manager):
            server.update_cookie_status('fixture-recovery', server.CookieStatusIn(enabled=True), self.user)
            self.assertEqual(recovery.status('fixture-recovery')['recovery_state'], 'checking')
            recovery.require_manual('fixture-recovery', 'message_verification_required')
            server.update_cookie_status('fixture-recovery', server.CookieStatusIn(enabled=True), self.user)
            self.assertEqual(recovery.status('fixture-recovery')['recovery_state'], 'required')

    def test_duplicate_session_and_cancel_release_task_without_stale_update(self):
        async def exercise():
            self.require()
            started = asyncio.Event()
            closed = asyncio.Event()
            async def open_session(*args, **kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    closed.set()
            with patch.object(server.cookie_manager, 'manager', self.make_manager()), patch('utils.manual_captcha.open_manual_session', side_effect=open_session):
                first = asyncio.create_task(server.start_manual_captcha('fixture-recovery', 60, 'request-a', self.user, check_only=True))
                await asyncio.wait_for(started.wait(),2)
                with self.assertRaises(HTTPException) as error:
                    await server.start_manual_captcha('fixture-recovery', 60, 'request-b', self.user, check_only=True)
                self.assertEqual(error.exception.status_code, 409)
                await server.cancel_manual_captcha('fixture-recovery', 'wrong-id', self.user)
                self.assertFalse(closed.is_set())
                await server.cancel_manual_captcha('fixture-recovery', 'request-a', self.user)
                self.assertFalse((await first)['success'])
                self.assertTrue(closed.is_set())
                await server.cancel_manual_captcha('fixture-recovery', 'request-a', self.user)
            self.assertEqual(recovery.status('fixture-recovery')['failure_category'], 'message_verification_required')
            self.assertFalse(server._manual_captcha_tasks)
        asyncio.run(exercise())

    def test_cancel_before_start_prevents_late_browser_launch(self):
        async def exercise():
            self.require()
            await server.cancel_manual_captcha('fixture-recovery', 'request-late', self.user)
            with patch('utils.manual_captcha.open_manual_session', new=AsyncMock()) as opened:
                result = await server.start_manual_captcha('fixture-recovery', 60, 'request-late', self.user, check_only=True)
            self.assertFalse(result['success'])
            opened.assert_not_awaited()
        asyncio.run(exercise())

    def test_other_owner_cannot_cancel_or_get_link(self):
        user = {'user_id': self.owner + 100, 'username': 'other-fixture'}
        with self.assertRaises(HTTPException) as error:
            asyncio.run(server.cancel_manual_captcha('fixture-recovery', 'request', user))
        self.assertEqual(error.exception.status_code, 404)
        self.assertFalse(any('fresh-captcha-url' in getattr(route,'path','') for route in server.app.routes))

    def test_status_uses_persistence_not_old_logs_and_normalizes_utc(self):
        self.db.add_risk_control_log('fixture-recovery', event_description='旧的滑块失败', processing_status='failed')
        result = server.get_risk_control_status(self.user)['accounts'][0]
        self.assertEqual(result['verification_type'], 'none')
        self.require()
        result = server.get_risk_control_status(self.user)['accounts'][0]
        self.assertFalse(result['can_start_verification'])
        self.assertTrue(result['can_check_connection'])
        recovery.require_manual('fixture-recovery', 'challenge_page_error')
        result = server.get_risk_control_status(self.user)['accounts'][0]
        self.assertFalse(result['can_start_verification'])
        self.assertIn('官网消息页', result['verification_message'])
        self.assertEqual(server.normalize_risk_event_time('2026-10-20 09:23:58'), '2026-10-20T09:23:58+00:00')
        self.assertEqual(server.normalize_risk_event_time('2026-10-20T17:23:58+08:00'), '2026-10-20T09:23:58+00:00')
        self.assertNotIn('attempt_id', result)

    def test_deleting_account_clears_recovery_state(self):
        self.require()
        self.assertTrue(self.db.delete_cookie('fixture-recovery'))
        self.assertEqual(recovery.get(self.db, 'fixture-recovery')['recovery_state'], 'none')


class BrowserRecoveryResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_async_slot_wait_does_not_steal_future_slot(self):
        semaphore = threading.Semaphore(0)
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock()))
        with patch.object(browser_limit, '_get_semaphore', return_value=semaphore):
            task = asyncio.create_task(browser_limit.launch_browser(playwright))
            await asyncio.sleep(0.03)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            semaphore.release()
            await asyncio.sleep(0.15)
            self.assertTrue(semaphore.acquire(blocking=False))
            playwright.chromium.launch.assert_not_awaited()

    async def test_cancelled_browser_launch_closes_browser_before_releasing(self):
        semaphore = threading.Semaphore(1)
        started, finish = asyncio.Event(), asyncio.Event()
        browser = SimpleNamespace(close=AsyncMock())
        async def launch(**kwargs):
            started.set()
            await finish.wait()
            return browser
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
        with patch.object(browser_limit, '_get_semaphore', return_value=semaphore):
            task = asyncio.create_task(browser_limit.launch_browser(playwright))
            await asyncio.wait_for(started.wait(),2)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(semaphore.acquire(blocking=False))
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            browser.close.assert_awaited_once()
            self.assertTrue(semaphore.acquire(blocking=False))

    async def test_atomic_slider_reservations_and_nonblocking_constructor(self):
        manager = object.__new__(slider.SliderConcurrencyManager)
        manager.max_concurrent, manager.wait_timeout = 1, 0.05
        manager.active_instances, manager.waiting_queue = {}, []
        manager.instance_lock = threading.Lock()
        with patch.object(slider, 'concurrency_manager', manager):
            one = slider.XianyuSliderStealth('fixture-one', enable_learning=False)
            two = slider.XianyuSliderStealth('fixture-two', enable_learning=False)
            self.assertFalse(manager.active_instances)
            try:
                one._acquire_instance()
                task = asyncio.create_task(asyncio.to_thread(manager.acquire_instance, two.user_id, two))
                ticks = 0
                while not task.done():
                    ticks += 1
                    await asyncio.sleep(0.005)
                self.assertFalse(await task)
                self.assertGreater(ticks, 2)
                self.assertEqual(len(manager.active_instances), 1)
                manager.unregister_instance(one.user_id, two)
                self.assertEqual(len(manager.active_instances), 1)
                one.close_browser()
                one.close_browser()
                two._acquire_instance()
                self.assertEqual(len(manager.active_instances), 1)
            finally:
                one.close_browser()
                two.close_browser()
            self.assertFalse(manager.active_instances)

    async def test_password_object_is_created_run_and_closed_in_same_worker(self):
        calls = []
        def factory(**kwargs):
            calls.append(threading.get_ident())
            return SimpleNamespace(login_with_password_playwright=lambda *args: (calls.append(threading.get_ident()), {'fixture': 'value'})[1],
                                   close_browser=lambda: calls.append(threading.get_ident()))
        with patch.object(slider, 'XianyuSliderStealth', side_effect=factory):
            self.assertEqual(await slider.run_password_login('fixture', 'fixture', 'fake'), {'fixture': 'value'})
        self.assertEqual(len(set(calls)), 1)
        self.assertNotEqual(calls[0], threading.get_ident())

    async def test_cancelled_password_worker_keeps_ownership_until_cleanup(self):
        started, release, closed = threading.Event(), threading.Event(), threading.Event()
        def work(*args):
            started.set()
            release.wait(2)
            return None
        instance = SimpleNamespace(login_with_password_playwright=work, close_browser=closed.set)
        with patch.object(slider, 'XianyuSliderStealth', return_value=instance):
            task = asyncio.create_task(slider.run_password_login('fixture', 'fixture', 'fake'))
            await asyncio.to_thread(started.wait, 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(closed.is_set())
            release.set()
            await asyncio.to_thread(closed.wait, 1)
            self.assertTrue(closed.is_set())

    async def test_password_failure_releases_both_reservations(self):
        instance = slider.XianyuSliderStealth('fixture-password', enable_learning=False)
        with patch.object(browser_limit, 'acquire_slot') as acquired, patch.object(browser_limit, 'release_slot') as released, patch.object(slider, 'sync_playwright', side_effect=RuntimeError('fixture browser launch failure')):
            self.assertIsNone(await asyncio.to_thread(instance.login_with_password_playwright, 'fixture', 'fake'))
        acquired.assert_called_once()
        released.assert_called_once()
        self.assertNotIn(instance.user_id, slider.concurrency_manager.active_instances)


if __name__ == '__main__':
    unittest.main()
