#!/usr/bin/env python3
"""真实启动失败验收，使用空临时数据库，不启动任何闲鱼账号。"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    import yaml
    configuration = yaml.safe_load((ROOT / 'global_config.yml').read_text())
    if configuration.get('COOKIES_LIST') or os.getenv('COOKIES_STR'):
        raise SystemExit('启动验收要求配置中没有账号，避免启动真实账号')
    results = []
    with tempfile.TemporaryDirectory(prefix='xianyu-startup-check-') as directory:
        work = Path(directory)
        env = {**os.environ, 'DB_PATH': str(work / 'test.db'), 'PYTHONPATH': str(ROOT),
               'PYTHONDONTWRITEBYTECODE': '1', 'SQL_LOG_ENABLED': 'false',
               'ADMIN_PASSWORD': 'local-startup-fixture-only', 'API_HOST': '127.0.0.1',
               'APP_STATIC_DIR': str(work / 'static')}
        for case in ('missing_frontend', 'occupied_port'):
            with socket.socket() as occupied:
                occupied.bind(('127.0.0.1', 0))
                occupied.listen()
                env['API_PORT'] = str(occupied.getsockname()[1])
                if case == 'occupied_port':
                    (work / 'static/assets').mkdir(parents=True)
                    (work / 'static/assets/local.js').write_text('')
                    (work / 'static/index.html').write_text('<script src="/static/assets/local.js"></script>')
                result = subprocess.run([sys.executable, '-B', str(ROOT / 'Start.py')],
                                        cwd=work, env=env, capture_output=True, text=True, timeout=40)
            expected = '缺少 static/index.html' if case == 'missing_frontend' else 'API 服务线程已退出'
            passed = result.returncode != 0 and expected in result.stdout + result.stderr
            results.append({'check': case, 'passed': passed})
            if not passed:
                raise AssertionError(f'{case}: 未通过，请检查启动退出逻辑')
    if '--record' in sys.argv:
        (ROOT / 'Custom/startup-smoke.json').write_text(json.dumps(results, indent=2) + '\n')
    print('2 real startup failure checks passed; no platform requests')


if __name__ == '__main__':
    main()
