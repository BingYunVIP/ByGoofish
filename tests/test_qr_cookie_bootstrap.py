"""QR credentials can lack MTOP cookies despite a confirmed official login."""
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from utils.qr_cookie_bootstrap import initialize_signing_cookies, has_signing_cookies
from utils.xianyu_utils import trans_cookies, clear_captcha_challenge_markers


BASIC = 'unb=fixture-qr; cookie2=fixture-session'
COMPLETE = BASIC + '; _m_h5_tk=fixture-sign_1; _m_h5_tk_enc=fixture-enc'


class SigningBootstrapTests(unittest.IsolatedAsyncioTestCase):
    def test_qr_login_clears_stale_challenge_markers_without_touching_signing(self):
        raw = COMPLETE + '; x5secdata=stale; x5sectag=stale-tag; x5step=2'
        cleaned = clear_captcha_challenge_markers(raw)
        self.assertEqual(trans_cookies(cleaned).get('_m_h5_tk'), 'fixture-sign_1')
        self.assertNotIn('x5secdata', trans_cookies(cleaned))
        self.assertNotIn('x5sectag', trans_cookies(cleaned))
        self.assertNotIn('x5step', trans_cookies(cleaned))

    def context(self, payload=None, cookies=None, status=200):
        response = SimpleNamespace(status=status, json=AsyncMock(return_value=payload or {
            'ret': ['FAIL_SYS_TOKEN_EMPTY::token absent'], 'data': {}}), dispose=AsyncMock())
        context = SimpleNamespace(request=SimpleNamespace(post=AsyncMock(return_value=response)),
            cookies=AsyncMock(return_value=cookies if cookies is not None else [
                {'name': 'unb', 'value': 'fixture-qr'},
                {'name': '_m_h5_tk', 'value': 'fixture-sign_1'},
                {'name': '_m_h5_tk_enc', 'value': 'fixture-enc'}]))
        return context, response

    async def test_empty_token_response_initializes_both_fields_with_one_public_request(self):
        context, response = self.context()
        result = await initialize_signing_cookies(context, BASIC)
        self.assertTrue(has_signing_cookies(trans_cookies(result)))
        self.assertEqual(trans_cookies(result)['cookie2'], 'fixture-session')
        call = context.request.post.call_args
        self.assertIn('/mtop.idle.web.user.page.head/', call.args[0])
        self.assertNotIn('login.token', call.args[0])
        self.assertEqual(call.kwargs['params']['api'], 'mtop.idle.web.user.page.head')
        context.request.post.assert_awaited_once()
        context.cookies.assert_awaited_once_with('https://h5api.m.goofish.com/')
        response.dispose.assert_awaited_once()

    async def test_complete_credentials_do_not_make_bootstrap_requests(self):
        context, _ = self.context()
        self.assertEqual(await initialize_signing_cookies(context, COMPLETE), COMPLETE)
        context.request.post.assert_not_awaited()

    async def test_missing_session_never_sends_request(self):
        context, _ = self.context()
        outcome = {}
        self.assertIsNone(await initialize_signing_cookies(context, 'unb=fixture-qr', outcome))
        self.assertEqual(outcome['failure_category'], 'signing_token_missing')
        context.request.post.assert_not_awaited()

    async def test_single_signing_field_is_not_complete(self):
        context, _ = self.context(cookies=[{'name': '_m_h5_tk', 'value': 'fixture-sign_1'}])
        outcome = {}
        self.assertIsNone(await initialize_signing_cookies(context, BASIC, outcome))
        self.assertEqual(outcome['failure_category'], 'signing_token_missing')
        self.assertIn('不必重复扫码', outcome['message'])

    async def test_challenge_session_expiry_and_unknown_response_stop_even_with_new_signing(self):
        for codes, category in [(['FAIL_SYS_USER_VALIDATE'], 'login_verification_required'),
                                (['RGV587_ERROR'], 'login_verification_required'),
                                (['FAIL_SYS_SESSION_EXPIRED'], 'login_session_expired'),
                                (['FAIL_SYS_UNKNOWN'], 'challenge_invalid_response')]:
            with self.subTest(category=category):
                context, _ = self.context({'ret': codes, 'data': {}})
                outcome = {}
                self.assertIsNone(await initialize_signing_cookies(context, BASIC, outcome))
                self.assertEqual(outcome['failure_category'], category)
                context.request.post.assert_awaited_once()

    async def test_challenge_url_cannot_be_mistaken_for_success(self):
        context, _ = self.context({'ret': ['SUCCESS'], 'data': {'url': 'https://example.test/punish?secret=fixture'}})
        outcome = {}
        self.assertIsNone(await initialize_signing_cookies(context, BASIC, outcome))
        self.assertEqual(outcome['failure_category'], 'login_verification_required')
        self.assertNotIn('secret', str(outcome))

    async def test_cross_account_cookie_is_rejected(self):
        context, _ = self.context(cookies=[{'name': 'unb', 'value': 'other-fixture'}])
        outcome = {}
        self.assertIsNone(await initialize_signing_cookies(context, BASIC, outcome))
        self.assertEqual(outcome['failure_category'], 'interrupted')

    async def test_error_response_never_authenticates(self):
        context, _ = self.context(status=403)
        outcome = {}
        self.assertIsNone(await initialize_signing_cookies(context, BASIC, outcome))
        self.assertEqual(outcome['failure_category'], 'challenge_request_failed')

    async def test_parse_failure_and_cancel_dispose_response_without_private_error_text(self):
        for exception in (ValueError('fixture-private-response'), asyncio.CancelledError()):
            context, response = self.context()
            response.json.side_effect = exception
            outcome = {}
            if isinstance(exception, asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await initialize_signing_cookies(context, BASIC, outcome)
            else:
                self.assertIsNone(await initialize_signing_cookies(context, BASIC, outcome))
                self.assertNotIn('fixture-private-response', str(outcome))
            response.dispose.assert_awaited_once()

    async def test_diagnostics_never_expand_payload_or_cookie_values(self):
        context, _ = self.context({'ret': ['FAIL_SYS_TOKEN_EMPTY::fixture-private-body'],
                                  'data': {'sensitive': 'fixture-private-body'}})
        diagnostic = SimpleNamespace(secrets=(), stage=Mock(), emit=Mock())
        await initialize_signing_cookies(context, BASIC, diagnostic=diagnostic)
        emitted = str(diagnostic.emit.call_args_list)
        self.assertIn('FAIL_SYS_TOKEN_EMPTY', emitted)
        for secret in ('fixture-private-body', 'fixture-sign_1', 'fixture-enc', 'fixture-session'):
            self.assertNotIn(secret, emitted)


class QrLiveLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_bootstrap_saves_only_complete_cookies_and_never_navigates(self):
        from app.db_manager import DBManager
        from XianyuAutoAsync import XianyuLive
        with tempfile.TemporaryDirectory() as temp:
            db = DBManager(str(Path(temp) / 'qr.db'))
            try:
                owner = db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
                db.save_cookie('fixture-qr', BASIC, owner)
                live = XianyuLive.__new__(XianyuLive)
                live.cookie_id = 'fixture-qr'
                live.user_id = owner
                live.cookies_str = BASIC
                browser = SimpleNamespace(new_context=AsyncMock(), close=AsyncMock())
                context = SimpleNamespace(add_cookies=AsyncMock(), new_page=AsyncMock())
                browser.new_context.return_value = context
                playwright = SimpleNamespace(stop=AsyncMock())
                factory = SimpleNamespace(start=AsyncMock(return_value=playwright))
                with patch('playwright.async_api.async_playwright', return_value=factory), \
                     patch('XianyuAutoAsync.browser_limit.launch_browser', new=AsyncMock(return_value=browser)), \
                     patch('app.db_manager.db_manager', db), \
                     patch('utils.qr_cookie_bootstrap.initialize_signing_cookies', new=AsyncMock(return_value=COMPLETE)) as bootstrap:
                    self.assertTrue(await live.refresh_cookies_from_qr_login(BASIC))
                self.assertTrue(has_signing_cookies(trans_cookies(db.get_cookie('fixture-qr'))))
                self.assertEqual(live.cookies_str, db.get_cookie('fixture-qr'))
                context.new_page.assert_not_awaited()
                bootstrap.assert_awaited_once()
                browser.close.assert_awaited_once()
                playwright.stop.assert_awaited_once()
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
