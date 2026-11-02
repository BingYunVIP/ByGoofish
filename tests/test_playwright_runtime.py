import contextlib
from importlib import metadata
import io
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

from scripts import playwright_doctor
from utils import playwright_runtime


class PlaywrightRuntimeTests(unittest.TestCase):
    def test_service_user_cache_must_not_fall_back_to_root_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root_binary=Path(folder)/'root-cache/chrome'
            root_binary.parent.mkdir();root_binary.write_text('fixture');root_binary.chmod(0o700)
            service_binary=Path(folder)/'service-cache/chrome'
            driver=SimpleNamespace(chromium=SimpleNamespace(executable_path=str(service_binary)))
            with self.assertRaises(playwright_runtime.BrowserRuntimeError) as error:
                playwright_runtime.require_browser_binary(driver)
            self.assertEqual(error.exception.category,'browser_missing')
            service_binary.parent.mkdir();service_binary.write_text('fixture');service_binary.chmod(0o700)
            playwright_runtime.require_browser_binary(driver)
            service_binary.chmod(0o600)
            with self.assertRaises(playwright_runtime.BrowserRuntimeError) as error:
                playwright_runtime.require_browser_binary(driver)
            self.assertEqual(error.exception.category,'browser_permission_denied')

    def test_browser_failure_is_specific_and_does_not_dump_exception_payload(self):
        for error,category in [(RuntimeError("Executable doesn't exist: sensitive-token"),'browser_missing'),
                               (RuntimeError('error while loading shared libraries'),'browser_dependency_missing'),
                               (PermissionError('sensitive-token'),'browser_permission_denied'),
                               (RuntimeError('unknown sensitive-token'),'browser_launch_failed')]:
            kind,message=playwright_runtime.browser_failure(error)
            self.assertEqual(kind,category);self.assertNotIn('sensitive-token',message)

    def test_offline_report_does_not_claim_account_recovery(self):
        old=playwright_runtime.runtime_status()
        try:
            playwright_runtime.remember_runtime_report({'errors':[], 'failures':[]})
            self.assertTrue(playwright_runtime.runtime_status()['ready'])
            self.assertIn('不代表',playwright_runtime.runtime_status()['message'])
        finally:
            playwright_runtime._runtime_status.update(old)

    def test_missing_python_package_is_reported_before_starting_browser(self):
        with patch.object(metadata, 'version', side_effect=metadata.PackageNotFoundError):
            report = playwright_runtime.check_runtime()
        self.assertIsNone(report['version'])
        self.assertIn('未安装', report['errors'][0])

    def test_wrong_python_package_is_not_treated_as_ready(self):
        with patch.object(metadata, 'version', return_value='0.0.1'):
            report = playwright_runtime.check_runtime()
        self.assertIn('版本不匹配', report['errors'][0])
        self.assertFalse(report['checks'])

    def test_missing_binary_and_missing_system_library_have_different_remedies(self):
        binary = playwright_runtime.browser_error_hint(RuntimeError("Executable doesn't exist"))
        library = playwright_runtime.browser_error_hint(RuntimeError('error while loading shared libraries'))
        self.assertIn('--install', binary)
        self.assertNotIn('--with-deps', binary)
        self.assertIn('--with-deps', library)

    def test_install_uses_current_python_and_protects_shared_cache(self):
        report = {'python': 'test', 'version': 'test', 'executable': None, 'checks': [], 'errors': []}
        with (
            patch('sys.argv', ['doctor', '--install', '--with-deps', '--platform', 'ubuntu24.04-x64']),
            patch.dict(os.environ, {'HTTPS_PROXY': 'socks5://127.0.0.1:9'}),
            patch.object(metadata, 'version', return_value=playwright_runtime.required_version()),
            patch.object(subprocess, 'run') as run,
            patch.object(playwright_doctor, 'check_runtime', return_value=report),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(playwright_doctor.main(), 0)
        command = run.call_args.args[0]
        self.assertEqual(command, [playwright_doctor.sys.executable, '-m', 'playwright',
                                   'install', '--with-deps', 'chromium'])
        env = run.call_args.kwargs['env']
        self.assertNotIn('HTTPS_PROXY', env)
        self.assertEqual(env['PLAYWRIGHT_SKIP_BROWSER_GC'], '1')
        self.assertEqual(env['PLAYWRIGHT_HOST_PLATFORM_OVERRIDE'], 'ubuntu24.04-x64')

    def test_failed_install_has_nonzero_status(self):
        with (
            patch('sys.argv', ['doctor', '--install']),
            patch.object(metadata, 'version', return_value=playwright_runtime.required_version()),
            patch.object(subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'install')),
            patch.object(playwright_doctor, 'check_runtime') as check,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(playwright_doctor.main(), 1)
            check.assert_not_called()


class BrowserPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_qr_browser_failure_is_reported_and_not_automatically_reopened(self):
        from utils.qr_login import QRLoginManager,QRLoginSession
        manager=QRLoginManager();session=QRLoginSession('fixture-browser-qr',owner_id=1)
        session.status='verification_required';session.verification_url='https://example.invalid/verify'
        manager.sessions[session.session_id]=session
        with tempfile.TemporaryDirectory() as folder:
            driver=SimpleNamespace(chromium=SimpleNamespace(executable_path=str(Path(folder)/'missing/chrome')),
                                   stop=AsyncMock())
            factory=MagicMock();factory.start=AsyncMock(return_value=driver)
            with patch('playwright.async_api.async_playwright',return_value=factory):
                await manager._run_verification_page(session.session_id)
        self.assertEqual(manager.get_session_status(session.session_id)['failure_category'],'browser_missing')
        self.assertEqual(manager.get_session_status(session.session_id)['status'],'error')
        manager._ensure_verification_task(session)
        self.assertIsNone(session.verification_task)
        driver.stop.assert_awaited_once()

    async def test_missing_browser_stops_before_platform_request_and_releases_driver(self):
        from utils.manual_captcha import open_manual_session
        with tempfile.TemporaryDirectory() as folder:
            driver=SimpleNamespace(chromium=SimpleNamespace(executable_path=str(Path(folder)/'missing/chrome')),
                                   stop=AsyncMock())
            factory=MagicMock();factory.start=AsyncMock(return_value=driver)
            with patch('playwright.async_api.async_playwright',return_value=factory), \
                 patch('utils.manual_captcha._fetch_live_verification_url',new_callable=AsyncMock) as request, \
                 patch('utils.manual_captcha.browser_limit.launch_browser',new_callable=AsyncMock) as launch:
                result=await open_manual_session('fixture-missing-browser','unb=fixture',timeout=1)
            self.assertEqual(result['failure_category'],'browser_missing')
            request.assert_not_awaited();launch.assert_not_awaited();driver.stop.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
