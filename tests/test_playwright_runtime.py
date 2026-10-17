import contextlib
from importlib import metadata
import io
import os
import subprocess
import unittest
from unittest.mock import patch

from scripts import playwright_doctor
from utils import playwright_runtime


class PlaywrightRuntimeTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
