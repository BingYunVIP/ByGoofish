import asyncio
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from app.db_manager import DBManager
from app.services import auth_recovery as recovery
from app import reply_server as server
from fastapi import HTTPException


class RecoveryActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=DBManager(str(Path(self.tmp.name)/'actions.db'))
        self.owner=self.db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        self.db.save_cookie('action-fixture','unb=fixture; cookie2=fixture',self.owner)
        self.user={'user_id':self.owner,'username':'fixture'}
        self.patches=[patch('app.db_manager.db_manager',self.db),patch.object(server,'db_manager',self.db)]
        for p in self.patches:p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches):p.stop()
        self.db.close();self.tmp.cleanup()

    async def test_message_error_offers_verified_session_sync_and_connection_check(self):
        recovery.set_state(self.db,'action-fixture','required','challenge_page_error')
        s=recovery.get(self.db,'action-fixture')
        self.assertEqual(s['event_type'],'message_verification_required')
        self.assertEqual(s['recovery_action'],'sync_web_session')
        self.assertFalse(s['can_start_verification'])
        self.assertTrue(s['can_check_connection'])
        self.assertNotIn('重新扫码',recovery.FAILURES['challenge_page_error'])
        self.assertIsNone(recovery.begin_verification(self.db,'action-fixture'))
        active=recovery.begin_verification(self.db,'action-fixture',check_only=True)
        self.assertEqual(active['recovery_state'],'verifying')
        recovery.set_state(self.db,'action-fixture','required','official_verification_required',expected_attempt=active['attempt_id'])
        self.assertFalse(recovery.get(self.db,'action-fixture')['can_check_connection'])
        with patch('app.services.auth_recovery.time.time',return_value=time.time()+61):
            self.assertTrue(recovery.get(self.db,'action-fixture')['can_check_connection'])

    async def test_confirmed_session_expiry_still_requires_login(self):
        recovery.set_state(self.db,'action-fixture','required','login_session_expired')
        s=recovery.get(self.db,'action-fixture')
        self.assertEqual(s['recovery_action'],'qr_login')
        self.assertFalse(s['can_start_verification'])
        self.assertFalse(s['can_check_connection'])

    async def test_explicit_check_calls_browser_in_check_only_mode_once(self):
        recovery.set_state(self.db,'action-fixture','required','challenge_page_error')
        opened=AsyncMock(return_value={'success':False,'message':'needs official verification','failure_category':'official_verification_required'})
        with patch('utils.manual_captcha.open_manual_session',opened),patch.object(server.cookie_manager,'manager',None):
            result=await server.start_manual_captcha('action-fixture',60,'fixture-check',self.user,check_only=True)
            self.assertFalse(result['success'])
            self.assertTrue(opened.await_args.kwargs['check_only'])
            with self.assertRaises(HTTPException):
                await server.start_manual_captcha('action-fixture',60,'fixture-check-2',self.user,check_only=True)
        self.assertEqual(opened.await_count,1)
        self.assertEqual(recovery.get(self.db,'action-fixture')['recovery_action'],'sync_web_session')

    async def test_check_only_endpoint_keeps_account_authorization(self):
        recovery.set_state(self.db,'action-fixture','required','challenge_page_error')
        with self.assertRaises(HTTPException) as e:
            await server.start_manual_captcha('action-fixture',60,'fixture-check',{'user_id':self.owner+1},check_only=True)
        self.assertEqual(e.exception.status_code,404)

    async def test_qr_confirmation_requires_message_verification_action(self):
        from utils.qr_login import qr_login_manager
        sid='action-fixture-qr'
        server.qr_check_processed[sid]={'owner_id':self.owner,'timestamp':time.time(),'processed':True,
            'result':{'status':'processing','account_info':{'account_id':'action-fixture'}}}
        recovery.set_state(self.db,'action-fixture','required','challenge_page_error')
        try:
            result=await server.check_qr_code_status(sid,self.user)
            self.assertEqual(result['status'],'message_verification_required')
            self.assertEqual(result['recovery_action'],'sync_web_session')
            self.assertTrue(result['can_check_connection'])
            self.assertFalse(result['account_ready'])
        finally:server.qr_check_processed.pop(sid,None)


class CheckWithoutNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_still_challenged_does_not_open_or_refresh_error_page(self):
        from utils.manual_captcha import open_manual_session
        # The real project browser launches, but no platform URL is requested.
        from app.services import captcha_diagnostics
        requests=[]
        diagnostic=SimpleNamespace(stage=lambda *_:None,emit=lambda *_:None,detach=lambda:None,
            attach=lambda context:context.on('request',lambda request:requests.append(request.url)))
        with patch('utils.manual_captcha._fetch_live_verification_url',new=AsyncMock(return_value='https://example.test/challenge')), \
             patch('utils.manual_captcha._wait_for_captcha_present',new=AsyncMock()) as slider:
            result=await open_manual_session('action-fixture','unb=fixture; cookie2=fixture',timeout=60,check_only=True,diagnostic=diagnostic)
        self.assertFalse(result['success'])
        self.assertEqual(result['failure_category'],'official_verification_required')
        self.assertEqual(requests,[])
        slider.assert_not_awaited()
