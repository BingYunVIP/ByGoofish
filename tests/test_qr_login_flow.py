import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
import time
from unittest.mock import AsyncMock, Mock, patch

from app import reply_server
from app.db_manager import DBManager
from app.services import auth_recovery


class QrLoginFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reply_server.qr_check_processed.clear()
        self.temp=tempfile.TemporaryDirectory()
        self.db=DBManager(str(Path(self.temp.name)/'qr.db'))
        self.owner=self.db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        self.db_patch=patch.object(reply_server,'db_manager',self.db)
        self.db_patch.start()

    def tearDown(self):
        reply_server.qr_check_processed.clear()
        self.db_patch.stop()
        self.db.close()
        self.temp.cleanup()

    async def test_fast_persistence_returns_account_before_cookie_enhancement(self):
        manager = Mock()
        manager.add_cookie.return_value = None
        current_user = {"user_id": self.owner, "username": "tester"}

        with (
            patch.object(reply_server.db_manager, "get_all_cookies", return_value={}),
            patch.object(reply_server.db_manager, "save_cookie", wraps=self.db.save_cookie) as save_cookie,
            patch.object(reply_server.cookie_manager, "manager", manager),
        ):
            result = await reply_server.process_qr_login_cookies(
                "unb=account-1; cookie2=test-value",
                "account-1",
                current_user,
            )

        self.assertEqual(result["account_id"], "account-1")
        self.assertTrue(result["is_new_account"])
        self.assertEqual(result["cookie_field_count"], 2)
        save_cookie.assert_called_once()
        manager.add_cookie.assert_called_once()
        self.assertEqual(auth_recovery.get(self.db,'account-1')['recovery_state'],'verifying')

    async def test_official_qr_replaces_stale_challenge_cookies(self):
        user={'user_id': self.owner, 'username': 'fixture'}
        stale='unb=qr-account; cookie2=fixture; x5secdata=stale; x5sectag=stale-tag'
        manager=SimpleNamespace(instances={}, add_cookie=Mock(), update_cookie=Mock())
        with patch.object(reply_server.db_manager, 'get_all_cookies', return_value={}), \
             patch.object(reply_server.cookie_manager, 'manager', manager):
            info=await reply_server.process_qr_login_cookies(stale, 'qr-account', user)
        saved=self.db.get_cookie('qr-account')
        self.assertNotIn('x5secdata', saved)
        self.assertNotIn('x5sectag', saved)
        self.assertEqual(auth_recovery.get(self.db, 'qr-account')['recovery_state'], 'verifying')
        self.assertEqual(info['account_id'], 'qr-account')

    async def test_token_probe_starts_only_after_qr_signing_cookie_is_complete(self):
        user={'user_id':self.owner,'username':'fixture'}
        manager=SimpleNamespace(instances={},add_cookie=Mock(),update_cookie=Mock())
        fake=SimpleNamespace(_unregister_instance=Mock())
        async def enhance(**_):
            self.assertEqual(auth_recovery.get(self.db,'qr-account')['recovery_state'],'verifying')
            self.db.update_cookie_account_info('qr-account',cookie_value='unb=qr-account; _m_h5_tk=fixture-sign_1; _m_h5_tk_enc=fixture-enc')
            return True
        fake.refresh_cookies_from_qr_login=AsyncMock(side_effect=enhance)
        with patch.object(reply_server.cookie_manager,'manager',manager), \
             patch('XianyuAutoAsync.XianyuLive',return_value=fake), \
             patch.object(reply_server,'_fetch_and_store_account_profile',new=AsyncMock()) as profile:
            info=await reply_server.process_qr_login_cookies('unb=qr-account; cookie2=fixture','qr-account',user)
            await reply_server._enhance_qr_login_cookies('fixture-session',info,'unb=qr-account; cookie2=fixture',user,
                                                       auth_bootstrap_attempt=info['_auth_bootstrap_attempt'])
        self.assertEqual(auth_recovery.get(self.db,'qr-account')['recovery_state'],'checking')
        self.assertIn('_m_h5_tk',manager.update_cookie.call_args.args[1])
        profile.assert_not_awaited()

    async def test_incomplete_qr_enhancement_keeps_authentication_paused(self):
        user={'user_id':self.owner,'username':'fixture'}
        manager=SimpleNamespace(instances={},add_cookie=Mock(),update_cookie=Mock())
        fake=SimpleNamespace(_unregister_instance=Mock(),refresh_cookies_from_qr_login=AsyncMock(return_value=True),send_token_refresh_notification=AsyncMock())
        with patch.object(reply_server.cookie_manager,'manager',manager), \
             patch('XianyuAutoAsync.XianyuLive',return_value=fake), \
             patch.object(reply_server,'_fetch_and_store_account_profile',new=AsyncMock()) as profile:
            info=await reply_server.process_qr_login_cookies('unb=qr-account; cookie2=fixture','qr-account',user)
            await reply_server._enhance_qr_login_cookies('fixture-session',info,'unb=qr-account; cookie2=fixture',user,
                                                       auth_bootstrap_attempt=info['_auth_bootstrap_attempt'])
        state=auth_recovery.get(self.db,'qr-account')
        self.assertEqual(state['recovery_state'],'required')
        self.assertEqual(state['failure_category'],'signing_token_missing')
        manager.update_cookie.assert_not_called()
        profile.assert_not_awaited()

    async def test_qr_failure_preserves_platform_category_and_does_not_start_worker(self):
        user = {'user_id': self.owner, 'username': 'fixture'}
        manager = SimpleNamespace(instances={}, add_cookie=Mock(), update_cookie=Mock())
        fake = SimpleNamespace(_unregister_instance=Mock(),
            _last_qr_bootstrap_error={'failure_category': 'login_verification_required'},
            refresh_cookies_from_qr_login=AsyncMock(return_value=False), send_token_refresh_notification=AsyncMock())
        with patch.object(reply_server.cookie_manager, 'manager', manager), \
             patch('XianyuAutoAsync.XianyuLive', return_value=fake):
            info = await reply_server.process_qr_login_cookies('unb=qr-account; cookie2=fixture', 'qr-account', user)
            await reply_server._enhance_qr_login_cookies('fixture-session', info,
                'unb=qr-account; cookie2=fixture', user, auth_bootstrap_attempt=info['_auth_bootstrap_attempt'])
        self.assertEqual(auth_recovery.get(self.db, 'qr-account')['failure_category'], 'login_verification_required')
        manager.update_cookie.assert_not_called()

    async def test_late_qr_enhancement_cannot_resume_a_replaced_recovery_attempt(self):
        user={'user_id':self.owner,'username':'fixture'}
        manager=SimpleNamespace(instances={},add_cookie=Mock(),update_cookie=Mock())
        fake=SimpleNamespace(_unregister_instance=Mock())
        async def newer_login(**_):
            auth_recovery.save_explicit_credentials(self.db,'qr-account',lambda:
                self.db.update_cookie_account_info('qr-account',cookie_value='unb=qr-account; _m_h5_tk=newer-login_1'))
            return True
        fake.refresh_cookies_from_qr_login=AsyncMock(side_effect=newer_login)
        with patch.object(reply_server.cookie_manager,'manager',manager), \
             patch('XianyuAutoAsync.XianyuLive',return_value=fake), \
             patch.object(reply_server,'_fetch_and_store_account_profile',new=AsyncMock()):
            info=await reply_server.process_qr_login_cookies('unb=qr-account; cookie2=fixture','qr-account',user)
            await reply_server._enhance_qr_login_cookies('fixture-session',info,'unb=qr-account; cookie2=fixture',user,
                                                       auth_bootstrap_attempt=info['_auth_bootstrap_attempt'])
        self.assertEqual(auth_recovery.get(self.db,'qr-account')['recovery_state'],'checking')
        self.assertIn('newer-login_1',self.db.get_cookie('qr-account'))
        manager.update_cookie.assert_not_called()

    async def test_mismatched_account_identifier_is_rejected_before_save(self):
        current_user = {"user_id": 7, "username": "tester"}

        with patch.object(reply_server.db_manager, "save_cookie") as save_cookie:
            with self.assertRaisesRegex(ValueError, "账号标识不一致"):
                await reply_server.process_qr_login_cookies(
                    "unb=cookie-account; cookie2=test-value",
                    "response-account",
                    current_user,
                )

        save_cookie.assert_not_called()

    async def test_qr_status_only_reports_success_after_recovery_state(self):
        self.db.save_cookie('qr-account', 'unb=qr-account; cookie2=fixture', self.owner)
        session_id = 'session-recovery-status'
        reply_server.qr_check_processed[session_id] = {
            'owner_id': self.owner,
            'processed': True,
            'timestamp': time.time(),
            'result': {
                'status': 'processing',
                'account_ready': False,
                'cookie_refresh_status': 'processing',
                'account_info': {'account_id': 'qr-account'},
                'message': '扫码凭证已保存，正在检查令牌与消息连接',
            },
        }
        with patch.object(auth_recovery, 'default_db', lambda: self.db):
            auth_recovery.require_manual('qr-account', 'message_verification_required')
            failed = await reply_server.check_qr_code_status(session_id, {'user_id': self.owner, 'username': 'admin'})
            self.assertEqual(failed['status'], 'message_verification_required')
            self.assertEqual(failed['recovery_action'], 'sync_web_session')
            self.assertFalse(failed['account_ready'])
            self.assertEqual(failed['failure_category'], 'message_verification_required')
            auth_recovery.set_state(self.db, 'qr-account', 'recovered')
            recovered = await reply_server.check_qr_code_status(session_id, {'user_id': self.owner, 'username': 'admin'})
        self.assertEqual(recovered['status'], 'success')
        self.assertTrue(recovered['account_ready'])
        self.assertEqual(recovered['message'], '令牌与消息连接已确认恢复')

    async def test_session_becomes_success_while_enhancement_is_still_running(self):
        session_id = "session-fast-success"
        enhancement_release = asyncio.Event()

        async def wait_for_release(**_):
            await enhancement_release.wait()

        account_info = {
            "account_id": "account-1",
            "is_new_account": True,
            "real_cookie_refreshed": False,
            "cookie_field_count": 2,
            "_manager_operation": None,
        }

        with (
            patch.object(
                reply_server,
                "process_qr_login_cookies",
                new=AsyncMock(return_value=account_info),
            ),
            patch.object(
                reply_server,
                "_enhance_qr_login_cookies",
                side_effect=wait_for_release,
            ),
        ):
            task = asyncio.create_task(
                reply_server._process_qr_login_session(
                    session_id,
                    {"cookies": "unb=account-1; cookie2=test-value", "unb": "account-1"},
                    {"user_id": 7, "username": "tester"},
                )
            )

            for _ in range(20):
                if session_id in reply_server.qr_check_processed:
                    break
                await asyncio.sleep(0)

            result = reply_server.qr_check_processed[session_id]["result"]
            self.assertEqual(result["status"], "processing")
            self.assertFalse(result["account_ready"])
            self.assertEqual(result["cookie_refresh_status"], "processing")
            self.assertFalse(task.done())

            enhancement_release.set()
            await task


if __name__ == "__main__":
    unittest.main()
