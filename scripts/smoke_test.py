#!/usr/bin/env python3
"""在临时数据库和目录中回归本次修复，再离线启动浏览器；不登录真实账号。"""

import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TESTS = (
    'tests.test_direct_network', 'tests.test_playwright_runtime',
    'tests.test_qr_login_flow', 'tests.test_qr_verification_resume',
    'tests.test_browser_slot_release', 'tests.test_slider_stealth_engine',
    'tests.test_manual_captcha_flow', 'tests.test_seller_api_parsing',
    'tests.test_consign_and_logistics_parsing',
)


def main():
    with tempfile.TemporaryDirectory(prefix='xianyu-smoke-') as directory:
        env = {**os.environ, 'DB_PATH': str(Path(directory) / 'test.db'),
               'PYTHONPATH': str(ROOT), 'PYTHONDONTWRITEBYTECODE': '1',
               'SQL_LOG_ENABLED': 'false', 'ADMIN_PASSWORD': secrets.token_urlsafe(24)}
        result = subprocess.run([sys.executable, '-m', 'unittest', *TESTS, '-q'],
                                cwd=directory, env=env, capture_output=True, text=True)
        output = result.stdout + result.stderr
        # 通过时只展示总结；失败时保留足够的上下文。所有数据库与日志均在临时目录。
        print(output[-16000:] if result.returncode else '\n'.join(output.splitlines()[-5:]))
        if result.returncode:
            return result.returncode
    return subprocess.run([sys.executable, str(ROOT / 'scripts/playwright_doctor.py')],
                          cwd=ROOT).returncode


if __name__ == '__main__':
    raise SystemExit(main())
