#!/usr/bin/env python3
"""Root-only isolated native service smoke; no production DB, accounts, or service changes.

Use scripts/dev_isolated.sh scripts/native_service_smoke.py --node-bin /path/to/node/bin
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
from urllib.request import build_opener, ProxyHandler

import yaml
from native_deploy import write_service

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node-bin', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('isolated systemd smoke requires root')
    config = yaml.safe_load((ROOT / 'global_config.yml').read_text())
    cookies = config.get('COOKIES')
    if config.get('COOKIES_LIST') or (cookies and (not isinstance(cookies, dict) or cookies.get('value'))):
        parser.error('inline account credentials present; refusing to start any account')
    if not (args.node_bin / 'node').is_file():
        parser.error('node executable is missing')
    work = Path(tempfile.mkdtemp(prefix='bygoofish fresh-service '))
    name = f'bygoofish-native-smoke-{os.getpid()}.service'
    unit = Path('/etc/systemd/system') / name
    dropin = Path(str(unit) + '.d')
    try:
        for directory in ('app', 'utils', 'scripts', 'static'):
            excluded = ['__pycache__', 'node_modules']
            if directory == 'static':
                excluded.append('uploads')
            shutil.copytree(ROOT / directory, work / directory,
                            ignore=shutil.ignore_patterns(*excluded))
        for filename in ('Start.py', 'XianyuAutoAsync.py', 'global_config.yml', 'requirements.txt', 'requirements.lock'):
            shutil.copy2(ROOT / filename, work / filename)
        (work / '.venv').symlink_to(ROOT / '.venv', target_is_directory=True)
        # Exercise private Node lookup including spaces in PATH, not the host's global Node.
        private_node = work / 'private node'
        private_node.symlink_to(args.node_bin.resolve(), target_is_directory=True)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        db = work / 'data/isolated.db'
        write_service(work, dropin, '127.0.0.1', str(port), db, 'root', '/root', 1, node_bin=str(private_node))
        unit.write_text('[Unit]\nDescription=Isolated native deployment smoke\n[Service]\nType=simple\n')
        with (dropin / '90-bygoofish-native.conf').open('a') as output:
            output.write('\nEnvironment=ACCOUNT_OFFLINE_MONITOR_ENABLED=false\nEnvironment=COOKIES_STR=\n'
                         'Environment=ADMIN_PASSWORD=native-service-fixture-only\n')
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'start', name], check=True)
        opener = build_opener(ProxyHandler({}))
        healthy = False
        for _ in range(100):
            try:
                with opener.open(f'http://127.0.0.1:{port}/health', timeout=1) as response:
                    healthy = json.load(response).get('status') == 'healthy'
                if healthy:
                    break
            except OSError:
                pass
            time.sleep(.3)
        if not healthy:
            raise RuntimeError('isolated native service did not become healthy')
        with sqlite3.connect(db) as database:
            accounts = database.execute('SELECT COUNT(*) FROM cookies').fetchone()[0]
            orders = database.execute('SELECT COUNT(*) FROM orders').fetchone()[0]
            if accounts or orders:
                raise RuntimeError('isolated test unexpectedly contains business data')
        pid = (work / 'data/isolated.db.runtime.lock').read_text().strip()
        environment = dict(part.split(b'=', 1) for part in (Path('/proc') / pid / 'environ').read_bytes().split(b'\0') if b'=' in part)
        if environment.get(b'API_PORT') != str(port).encode() or environment.get(b'DB_PATH') != os.fsencode(db):
            raise RuntimeError('systemd environment file was not applied')
        node = shutil.which('node', path=os.fsdecode(environment[b'PATH']))
        if not node or Path(node).parent != private_node:
            raise RuntimeError('private Node is not available to the service')
        version = subprocess.check_output([node, '--version'], text=True).strip()
        result = {'isolated_native_service': 'passed', 'healthy': healthy, 'empty_accounts': accounts,
                  'empty_orders': orders, 'space_in_directory': True, 'private_node': version,
                  'environment_file_applied': True}
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result))
    finally:
        with args.output.with_suffix('.log').open('w') as output:
            subprocess.run(['journalctl', '-u', name, '-n', '160', '--no-pager'], stdout=output, stderr=subprocess.DEVNULL)
        subprocess.run(['systemctl', 'stop', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['systemctl', 'reset-failed', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        unit.unlink(missing_ok=True)
        shutil.rmtree(dropin, ignore_errors=True)
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        shutil.rmtree(work)


if __name__ == '__main__':
    main()
