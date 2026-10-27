"""本地夹具覆盖错误页、首次故障、隔离权限、日志上限与双重脱敏。"""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from app.db_manager import DBManager
from app.services import captcha_diagnostics as diag, auth_recovery
from app import reply_server as server
from utils.captcha_page_state import inspect_page, ChallengePageError
from utils import browser_limit
from utils.captcha_remote_control import captcha_controller
from utils.direct_network import direct_browser_options

FIXTURES = Path(__file__).parent / 'fixtures'


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = DBManager(str(Path(self.temp.name) / 'db.sqlite'))
        self.owner = self.db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        self.cookie = 'unb=fixture-account; cookie2=fixture-cookie-secret; _m_h5_tk=fixture-token_1'
        self.db.save_cookie('fixture-diag', self.cookie, self.owner)
        self.capture = diag.Capture(self.db, 'fixture-diag', self.owner, self.cookie)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.db.close)
        self.addCleanup(server._manual_captcha_finished.clear)

    def read(self, **kwargs):
        return diag.read(self.db, self.capture.id, self.owner, **kwargs)

    def test_sanitizes_before_storage_and_again_before_response(self):
        text = 'Cookie: first=fixture-cookie-secret; second=rotated-private\n'
        text += 'Authorization: Bearer bearer-private\naccessToken="fixture-token_1" '
        text += 'https://name:password@example.test/punish?x5secdata=fixture-cookie-secret&other=url-private#fragment fixture-account'
        self.capture.emit('error', 'console', text)
        stored = self.db.conn.execute('SELECT message FROM captcha_diagnostic_events WHERE diagnostic_id=?', (self.capture.id,)).fetchall()
        for private in ('fixture-cookie-secret','rotated-private','bearer-private','fixture-token_1','url-private','fragment','fixture-account','name:password'):
            self.assertNotIn(private, json.dumps(stored))
        self.db.conn.execute('UPDATE captcha_diagnostic_events SET message=? WHERE diagnostic_id=? AND seq=1',
                             ('token=legacy-private https://example.test/p?sign=legacy-query', self.capture.id))
        result = json.dumps(self.read())
        self.assertNotIn('legacy-private', result)
        self.assertNotIn('legacy-query', result)
        self.assertIn('example.test/p', result)

    def test_caps_paginate_and_expire_without_deleting_audit(self):
        for _ in range(505):
            self.capture.emit('info', 'fixture', '长' * 600)
        data = self.read()
        self.assertEqual(data['event_count'], 500)
        self.assertEqual(data['dropped_count'], 6)
        seen = []
        while True:
            seen.extend(data['entries'])
            if not data['has_more']: break
            data = self.read(after_seq=data['next_seq'])
        self.assertEqual([e['seq'] for e in seen], list(range(1,501)))
        self.assertTrue(all(len(e['message']) <= 512 for e in seen))
        auth_recovery.record_failure(self.db,'fixture-diag','challenge_page_error',self.capture.id)
        self.db.conn.execute('UPDATE captcha_diagnostics SET created_at=? WHERE id=?',
                             (time.time()-diag.RETENTION_SECONDS-1,self.capture.id))
        self.db.conn.commit()
        self.assertIsNone(self.read())
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM captcha_diagnostic_events').fetchone()[0],0)
        self.assertEqual(self.db.get_risk_control_logs_count(),1)

    def test_first_fault_survives_cancel_and_repeat_cleanup(self):
        self.capture.fail('challenge_page_error','验证页面报错','TioSgk')
        self.capture.fail('browser_error','后续错误')
        self.capture.finish('cancelled')
        count = self.read()['event_count']
        self.capture.finish('cancelled')
        data = self.read()
        self.assertEqual(data['failure_category'],'challenge_page_error')
        self.assertEqual(data['error_code'],'TioSgk')
        self.assertEqual(data['close_reason'],'cancelled')
        self.assertEqual(data['status'],'failed')
        self.assertEqual(data['event_count'],count)

    def test_console_error_identifies_resource_without_query_secrets(self):
        from types import SimpleNamespace
        context = SimpleNamespace(on=lambda event, cb: callbacks.__setitem__(event, cb),
                                  remove_listener=lambda *args: None)
        callbacks = {}
        self.capture.attach(context)
        callbacks['console'](SimpleNamespace(type='error', text='Failed to load resource: 404',
            location={'url':'https://example.test/favicon.ico?sign=private-query#fragment', 'lineNumber':0}))
        message = self.read()['entries'][-1]['message']
        self.assertIn('example.test/favicon.ico:0', message)
        self.assertNotIn('private-query', message)
        self.assertNotIn('fragment', message)
        self.capture.detach()

    def test_cancel_status_is_explicit_and_does_not_reinterpret_history(self):
        self.db.add_risk_control_log('fixture-diag','auth_recovery',processing_status='failed',error_message='cancelled')
        auth_recovery.record_failure(self.db,'fixture-diag','cancelled',self.capture.id)
        another = diag.Capture(self.db,'fixture-diag',self.owner,self.cookie)
        another.fail('challenge_page_error','错误')
        auth_recovery.record_failure(self.db,'fixture-diag','challenge_page_error',another.id)
        self.assertEqual(self.db.get_risk_control_logs_count(processing_status='cancelled'),1)
        self.assertEqual(self.db.get_risk_control_logs_count(processing_status='failed'),2)
        self.assertEqual({r['processing_status'] for r in self.db.get_risk_control_logs(processing_status='cancelled')},{'cancelled'})
        self.assertEqual(self.db.conn.execute('SELECT processing_status FROM risk_control_logs WHERE id=1').fetchone()[0],'failed')

    def test_owner_and_current_account_owner_both_required(self):
        self.assertIsNone(diag.read(self.db,self.capture.id,self.owner+1))
        with patch('app.db_manager.db_manager',self.db):
            self.assertEqual(server.get_captcha_diagnostics(self.capture.id,0,100,{'user_id':self.owner})['data']['diagnostic_id'],self.capture.id)
            with self.assertRaises(HTTPException) as error:
                server.get_captcha_diagnostics(self.capture.id,0,100,{'user_id':self.owner+1})
            self.assertEqual(error.exception.status_code,404)
        self.db.conn.execute('UPDATE captcha_diagnostics SET owner_id=? WHERE id=?',(self.owner+1,self.capture.id))
        self.db.conn.commit()
        self.assertIsNone(diag.read(self.db,self.capture.id,self.owner))

    def test_recovery_result_linked_to_attempt_only(self):
        auth_recovery.set_state(self.db,'fixture-diag','required','message_verification_required')
        active=auth_recovery.begin_verification(self.db,'fixture-diag',check_only=True)
        state=auth_recovery.set_state(self.db,'fixture-diag','checking',expected_attempt=active['attempt_id'])
        self.db.conn.execute('UPDATE captcha_diagnostics SET recovery_attempt=? WHERE id=?',(state['attempt_id'],self.capture.id))
        self.db.conn.commit()
        self.db.add_risk_control_log('fixture-diag','auth_recovery',diagnostic_id=self.capture.id)
        diag.recovery_result(self.db,'fixture-diag','old-attempt','recovered','')
        self.assertEqual(self.read()['status'],'processing')
        auth_recovery.set_state(self.db,'fixture-diag','required','token_probe_failed',expected_attempt=state['attempt_id'])
        self.assertEqual(self.read()['failure_category'],'token_probe_failed')
        self.assertEqual(next(r for r in self.db.get_risk_control_logs() if r.get('diagnostic_id')==self.capture.id)['processing_status'],'failed')

    def test_restart_marks_interrupted_diagnostic_and_preserves_primary_error(self):
        auth_recovery.set_state(self.db,'fixture-diag','verifying')
        auth_recovery.initialize(self.db)
        diag.initialize(self.db)
        data=self.read()
        self.assertEqual(data['stage'],'interrupted')
        self.assertEqual(data['failure_category'],'interrupted')
        self.assertEqual(data['status'],'failed')

    def test_http_auth_limits_and_cross_owner_denial(self):
        from fastapi.testclient import TestClient
        old=dict(server.app.dependency_overrides)
        try:
            with patch('app.db_manager.db_manager',self.db), TestClient(server.app) as client:
                endpoint='/api/captcha/diagnostics/'+self.capture.id
                self.assertIn(client.get(endpoint).status_code,(401,403))
                server.app.dependency_overrides[server.get_current_user]=lambda:{'user_id':self.owner}
                self.assertEqual(client.get(endpoint).status_code,200)
                self.assertEqual(client.get(endpoint+'?after_seq=-1').status_code,422)
                self.assertEqual(client.get(endpoint+'?limit=101').status_code,422)
                server.app.dependency_overrides[server.get_current_user]=lambda:{'user_id':self.owner+1}
                self.assertEqual(client.get(endpoint).status_code,404)
        finally:
            server.app.dependency_overrides.clear()
            server.app.dependency_overrides.update(old)

    def test_cancel_after_page_error_uses_primary_reason_and_releases_task(self):
        from types import SimpleNamespace
        async def exercise():
            auth_recovery.set_state(self.db,'fixture-diag','required','message_verification_required')
            entered=asyncio.Event()
            async def session(*args,diagnostic,**kwargs):
                diagnostic.fail('challenge_page_error','错误','TioSgk')
                entered.set()
                await asyncio.Event().wait()
            manager=SimpleNamespace(instances={})
            user={'user_id':self.owner}
            with patch('app.db_manager.db_manager',self.db), patch('app.cookie_manager.manager',manager), \
                 patch('utils.manual_captcha.open_manual_session',side_effect=session):
                task=asyncio.create_task(server.start_manual_captcha('fixture-diag',60,'fixture-request',user,check_only=True))
                await asyncio.wait_for(entered.wait(),2)
                await server.cancel_manual_captcha('fixture-diag','fixture-request',user)
                await task
                await server.cancel_manual_captcha('fixture-diag','fixture-request',user)
            self.assertNotIn('fixture-diag',server._manual_captcha_tasks)
            self.assertEqual(auth_recovery.get(self.db,'fixture-diag')['failure_category'],'challenge_page_error')
            rows=self.db.get_risk_control_logs()
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['processing_status'],'failed')
            data=diag.read(self.db,rows[0]['diagnostic_id'],self.owner)
            self.assertEqual(data['close_reason'],'cancelled')
        asyncio.run(exercise())

    def test_delete_account_removes_private_diagnostics(self):
        self.assertTrue(self.db.delete_cookie('fixture-diag'))
        self.assertIsNone(self.read())
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM captcha_diagnostic_events').fetchone()[0],0)

    def test_browser_errors_and_network_are_captured_before_navigation(self):
        async def exercise():
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(**direct_browser_options({'headless':True}))
                context=await browser.new_context()
                self.capture.attach(context)
                await context.route('**/*',lambda route: route.abort() if 'missing-script' in route.request.url else route.fulfill(status=503,body='<h1>unavailable</h1>',content_type='text/html'))
                page=await context.new_page()
                await page.goto('http://fixture.test/page?sign=fixture-private')
                await page.set_content((FIXTURES/'captcha_script_failure.html').read_text())
                await page.wait_for_timeout(200)
                self.capture.detach()
                await browser.close()
            entries=self.read()['entries']
            kinds={e['kind'] for e in entries}
            self.assertTrue({'navigation','response','requestfailed','console','pageerror'} <= kinds,kinds)
            self.assertNotIn('fixture-private',json.dumps(entries))
            self.assertNotIn('fixture-secret',json.dumps(entries))
            self.assertEqual((await inspect_page(page))['state'],'loading')
        asyncio.run(exercise())


class CaptchaStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.pw=await async_playwright().start()
        self.browser=await self.pw.chromium.launch(**direct_browser_options({'headless':True, 'channel':'chromium'}))
        self.page=await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def test_shared_user_agent_matches_unmodified_project_browser(self):
        from utils.user_agents import CHROME_UA, CHROMIUM_VERSION
        self.assertEqual(self.browser.version, CHROMIUM_VERSION)
        self.assertEqual(await self.page.evaluate('navigator.userAgent'), CHROME_UA)
        self.assertTrue(await self.page.evaluate('navigator.webdriver'))

    async def test_short_error_container_and_iframe_are_never_ready(self):
        from utils.manual_captcha import _wait_for_captcha_present
        for fixture in ('captcha_error.html','captcha_iframe_error.html'):
            await self.page.goto((FIXTURES/fixture).resolve().as_uri())
            state=await inspect_page(self.page,was_ready=True)
            self.assertEqual(state,{'state':'page_error','error_code':'TioSgk'})
            with self.assertRaises(ChallengePageError):
                await _wait_for_captcha_present(self.page,timeout=1)

    async def test_error_after_drag_cannot_complete_and_short_slider_crops(self):
        await self.page.goto((FIXTURES/'captcha_drag_error.html').resolve().as_uri())
        self.assertEqual((await inspect_page(self.page))['state'],'interactive')
        await captcha_controller.create_session('fixture-drag',self.page,owner_id=1)
        try:
            self.assertIsNotNone(captcha_controller.active_sessions['fixture-drag']['captcha_info'])
            await self.page.locator('#nc_slide').click()
            self.assertFalse(await captcha_controller.check_completion('fixture-drag'))
            self.assertEqual((await inspect_page(self.page,True))['error_code'],'AfterDrag')
        finally:
            await captcha_controller.close_session('fixture-drag')

    async def test_control_disappearance_before_delayed_error_is_not_success(self):
        await self.page.goto((FIXTURES/'captcha_drag_error.html').resolve().as_uri())
        await captcha_controller.create_session('fixture-delayed',self.page,owner_id=1)
        try:
            await self.page.evaluate("""() => {
                document.getElementById('nocaptcha').style.display='none';
                setTimeout(() => document.body.innerText="Oops... something's wrong... (error:Delayed)",450);
            }""")
            self.assertFalse(await captcha_controller.check_completion('fixture-delayed'))
            self.assertEqual((await inspect_page(self.page,True))['error_code'],'Delayed')
        finally:
            await captcha_controller.close_session('fixture-delayed')

    async def test_loading_and_disabled_control_are_not_interactive_or_completed(self):
        await self.page.set_content('<div id="nocaptcha" style="height:48px">loading<button id="nc_slide" disabled>slide</button></div>')
        self.assertEqual((await inspect_page(self.page))['state'],'loading')
        self.assertEqual((await inspect_page(self.page,True))['state'],'loading')

    async def test_controller_never_completes_a_page_that_was_not_ready(self):
        await self.page.set_content('<p>Loading</p>')
        await captcha_controller.create_session('fixture-never-ready',self.page,owner_id=1)
        try:
            self.assertFalse(await captcha_controller.check_completion('fixture-never-ready'))
        finally:
            await captcha_controller.close_session('fixture-never-ready')

    async def test_full_session_error_returns_final_frame_and_releases_browser(self):
        from utils.manual_captcha import open_manual_session
        with patch('utils.manual_captcha._fetch_live_verification_url',new=AsyncMock(return_value=(FIXTURES/'captcha_error.html').resolve().as_uri())):
            result=await open_manual_session('fixture-error','unb=fixture',timeout=2)
        self.assertFalse(result['success'])
        self.assertEqual(result['failure_category'],'challenge_page_error')
        self.assertEqual(result['error_code'],'TioSgk')
        self.assertTrue(result['last_screenshot'])
        self.assertNotIn('fixture-error',captcha_controller.active_sessions)
        semaphore=browser_limit._get_semaphore()
        self.assertTrue(semaphore.acquire(blocking=False))
        semaphore.release()
