"""Native installer regressions: temporary Git repositories, SQLite, and fake services only."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("native_deploy", ROOT / "scripts/native_deploy.py")
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL, text=True).strip()


class NativeHelpers(unittest.TestCase):
    def test_resource_profiles(self):
        self.assertEqual([native.profile(cpu, ram) for cpu, ram in
                          [(1, 16000), (8, 1024), (4, 8000), (12, 8192), (12, 16384)]], [1, 1, 2, 2, 3])

    def test_environment_files_are_not_shell(self):
        value = native.systemd_values('API_PORT=9000\nDB_PATH="/tmp/space dir/data.db"\nKEY="$(touch /no-such-file)"\n')
        self.assertEqual(value["API_PORT"], "9000")
        self.assertEqual(value["DB_PATH"], "/tmp/space dir/data.db")
        self.assertEqual(value["KEY"], "$(touch /no-such-file)")
        self.assertEqual(native.environment_files('/etc/one.env (ignore_errors=no) /tmp/two\\x20words (ignore_errors=yes)'),
                         [Path('/etc/one.env'), Path('/tmp/two words')])

    def test_settings_preserve_existing_service_and_hide_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            envfile = root / "service env"
            envfile.write_text('API_PORT=9090\nPRIVATE_PASSWORD=never-return-this\n')
            output = (f'LoadState=loaded\nWorkingDirectory={root}\nUser=root\n'
                      f'Environment=API_HOST=127.0.0.1 API_PORT=8080 DB_PATH={root}/data.db TOKEN=private\n'
                      f'EnvironmentFiles={envfile} (ignore_errors=no)\n')
            with patch.object(native.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output, "")):
                data = native.settings(root)
            self.assertEqual(data["host"], "127.0.0.1")
            self.assertEqual(data["port"], "9090")
            self.assertEqual(data["db"], str(root / "data.db"))
            self.assertNotIn("private", json.dumps(data))

    def test_foreign_service_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(native.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, 'LoadState=loaded\nWorkingDirectory=/not-this-install\n', "")):
                with self.assertRaisesRegex(ValueError, "其他目录"):
                    native.settings(Path(temporary))

    def test_missing_required_environment_file_cannot_reset_existing_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = (f'LoadState=loaded\nWorkingDirectory={root}\nUser=root\n'
                      f'EnvironmentFiles={root}/missing.env (ignore_errors=no)\n')
            with patch.object(native.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output, '')):
                with self.assertRaisesRegex(ValueError, '不会用默认配置覆盖'):
                    native.settings(root)

    def test_sqlite_snapshot_includes_wal_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, output = Path(temporary) / 'live.db', Path(temporary) / 'copy.db'
            with sqlite3.connect(source) as db:
                db.execute('pragma journal_mode=WAL')
                db.execute('create table stock(value)')
                db.execute("insert into stock values('preserve')")
                db.commit()
                native.snapshot(source, output)
                self.assertEqual(db.execute('select value from stock').fetchone()[0], 'preserve')
                with sqlite3.connect(output) as copy:
                    self.assertEqual(copy.execute('select value from stock').fetchone()[0], 'preserve')
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_active_runtime_lock_blocks_deployment(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / 'live.db'
            with Path(str(db) + '.runtime.lock').open('w') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(ValueError, "业务进程"):
                    native.assert_stopped(db)
            native.assert_stopped(db)

    def test_service_keeps_paths_quoted_and_does_not_include_password(self):
        with tempfile.TemporaryDirectory(prefix='bygoofish space ') as temporary:
            root = Path(temporary)
            native.write_service(root, root / 'unit', '0.0.0.0', '8080', root / 'data/main.db', 'root', '/root', 1,
                                 node_bin=str(root / 'tools/node/bin'))
            unit = (root / 'unit/90-bygoofish-native.conf').read_text()
            env = (root / '.local/bygoofish/service.env').read_text()
            self.assertIn('Restart=on-failure', unit)
            self.assertIn('StartLimitBurst=3', unit)
            self.assertIn('ExecStart=\nExecStart=', unit)
            self.assertIn('WorkingDirectory=' + str(root), unit)
            self.assertIn('EnvironmentFile=' + str(root / '.local/bygoofish/service.env'), unit)
            self.assertIn('WEB_CONCURRENCY="1"', env)
            self.assertTrue(native.systemd_values(env)['PATH'].startswith(str(root / 'tools/node/bin') + ':'))
            self.assertNotIn('admin123', env + unit)
            self.assertEqual((root / '.local/bygoofish/service.env').stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(shutil.which('systemd-analyze'), 'systemd-analyze is unavailable')
    def test_generated_service_passes_real_systemd_parser_including_space_paths(self):
        with tempfile.TemporaryDirectory(prefix='bygoofish unit ') as temporary:
            root = Path(temporary)
            native.write_service(root, root / 'dropin', '0.0.0.0', '8080', root / 'data/live.db', 'root', '/root', 1)
            unit = root / 'bygoofish-fixture.service'
            text = '[Unit]\nDescription=Native fixture\n[Service]\nType=simple\n'
            # Only parse the unit. The harmless executable also works on CI without Xvfb.
            text += (root / 'dropin/90-bygoofish-native.conf').read_text().replace('/usr/bin/xvfb-run', '/usr/bin/true')
            unit.write_text(text)
            result = subprocess.run(['systemd-analyze', 'verify', str(unit)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse([line for line in result.stderr.splitlines() if str(unit) in line], result.stderr)

    def test_prune_only_complete_managed_update_backups(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'project'
            root.mkdir()
            base = Path(temporary) / 'backups'
            base.mkdir()
            old, keep, foreign = (base / name for name in ('update-old', 'update-new', 'update-private'))
            for path in (old, keep, foreign):
                path.mkdir()
            for path in (old, keep):
                (path / '.bygoofish-backup').write_text(str(root))
            with self.assertRaises(ValueError):
                native.prune(root, keep)
            self.assertTrue(old.exists())
            (keep / 'complete').touch()
            native.prune(root, keep)
            self.assertFalse(old.exists())
            self.assertTrue(keep.exists())
            self.assertTrue(foreign.exists())

    def test_log_redaction(self):
        values = ["Authorization: Bearer real-value", "cookie=real-value", 'password="real-value"',
                  "https://example.invalid/challenge?token=real-value#real-value", "密码为 real-value", "admin123",
                  'cookie2=real-value', '_m_h5_tk=real-value', 'sgcookie=real-value',
                  'Cookie字符串：real-value', 'https://user:real-value@example.invalid/path']
        for value in values:
            self.assertNotIn('real-value', native.redact(value))
            self.assertNotIn('admin123', native.redact(value))
        self.assertIn('HTTP 429', native.redact('平台 HTTP 429，等待重试'))

    def test_node_selects_only_tested_lts_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            index = Path(temporary) / 'index.json'
            index.write_text(json.dumps([{'version': 'v24.1.0', 'lts': 'x'}, {'version': 'v22.9.0', 'lts': 'x'}]))
            self.assertEqual(native.node_release(index), 'v22.9.0')

    def test_public_url_uses_the_listener_address_family_and_skips_private_ips(self):
        interfaces = [{'addr_info': [{'family': 'inet', 'local': '10.0.0.1'},
                                     {'family': 'inet6', 'local': '2606:4700::1111'},
                                     {'family': 'inet', 'local': '1.1.1.1'}]}]
        with patch.object(native.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(interfaces), '')):
            self.assertEqual(native.public_address(4), '1.1.1.1')
            self.assertEqual(native.public_address(6), '2606:4700::1111')

    def test_backup_without_git_is_complete_private_and_keeps_business_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, backup = Path(temporary) / 'project', Path(temporary) / 'backup'
            root.mkdir()
            (root / 'Start.py').write_text('code')
            (root / '.env').write_text('PRIVATE_VALUE=fixture')
            (root / 'data').mkdir()
            db = root / 'data/live.db'
            with sqlite3.connect(db) as conn:
                conn.execute('create table orders(id)')
                conn.execute('insert into orders values(123)')
            with patch.object(native.shutil, 'which', return_value=None):
                native.backup(root, backup, db)
            self.assertTrue((backup / 'complete').is_file())
            self.assertTrue((backup / '.env').is_file())
            self.assertEqual((backup / 'database.db').stat().st_mode & 0o777, 0o600)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
            with sqlite3.connect(backup / 'database.db') as conn:
                self.assertEqual(conn.execute('select id from orders').fetchone()[0], 123)
            with self.assertRaises(ValueError):
                native.backup(root, root / 'bad-backup', db)

    def test_configuration_rollback_preserves_database_and_other_dropins(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, backup, units = base / 'project', base / 'backup', base / 'units'
            root.mkdir(); backup.mkdir()
            (backup / '.bygoofish-backup').write_text(str(root))
            (backup / 'complete').touch()
            dropins = units / 'bygoofish.service.d'
            native.write_service(root, dropins, '127.0.0.1', '9090', root / 'live.db', 'root', '/root', 1)
            managed = Path('bygoofish.service.d/90-bygoofish-native.conf')
            (backup / managed.parent).mkdir()
            shutil.copy2(units / managed, backup / managed)
            for name in ('settings.json', 'service.env'):
                shutil.copy2(root / '.local/bygoofish' / name, backup / name)
            (dropins / '20-user.conf').write_text('[Service]\nNice=5\n')
            (root / 'live.db').write_text('new business data must stay')
            native.write_service(root, dropins, '0.0.0.0', '8080', root / 'live.db', 'root', '/root', 2)
            native.restore_service(root, backup, False, units)
            self.assertEqual((units / managed).read_bytes(), (backup / managed).read_bytes())
            self.assertEqual(json.loads((root / '.local/bygoofish/settings.json').read_text())['port'], '9090')
            self.assertEqual((root / 'live.db').read_text(), 'new business data must stay')
            self.assertTrue((dropins / '20-user.conf').is_file())

    def test_runtime_cleanup_keeps_current_and_backup_and_unmanaged_environments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            envs = root / '.local/bygoofish/envs'
            for name in ('py-current', 'py-previous', 'py-stale', 'py-personal'):
                (envs / name).mkdir(parents=True)
                if name != 'py-personal':
                    (envs / name / '.complete').touch()
            (root / '.venv').symlink_to(envs / 'py-current')
            backup = root / 'backup'
            backup.mkdir()
            (backup / 'manifest.json').write_text(json.dumps({'venv': str(envs / 'py-previous')}))
            native.prune_envs(root, backup)
            self.assertEqual({p.name for p in envs.iterdir()}, {'py-current', 'py-previous', 'py-personal'})

    def test_real_first_database_login_and_no_password_in_initialization_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = '''
from fastapi.testclient import TestClient
from app.reply_server import app
with TestClient(app) as client:
    assert client.post('/login', json={'username':'admin','password':'admin123'}).json()['success']
print('fresh database login passed')
'''
            result = subprocess.run([sys.executable, '-B', '-c', script], cwd=temporary,
                env={**os.environ, 'PYTHONPATH': str(ROOT), 'DB_PATH': str(Path(temporary) / 'fresh.db'),
                     'APP_STATIC_DIR': str(ROOT / 'static'), 'APP_LOG_PATH': str(Path(temporary) / 'app.log'),
                     'ACCOUNT_OFFLINE_MONITOR_ENABLED': 'false', 'ADMIN_PASSWORD': '', 'SQL_LOG_ENABLED': 'false'},
                capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            self.assertIn('fresh database login passed', result.stdout)
            self.assertNotIn('admin123', result.stdout + result.stderr)


class NativeMenu(unittest.TestCase):
    def test_menu_from_another_directory_invalid_input_eof_and_no_ansi(self):
        result = subprocess.run(['bash', str(ROOT / 'ByGoofish.sh')], input='invalid\n0\n',
                                capture_output=True, text=True, cwd='/tmp', timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn('请输入0–5', result.stdout)
        self.assertIn('后台服务保持运行', result.stdout)
        self.assertNotIn('\x1b', result.stdout)
        self.assertEqual(subprocess.run(['bash', str(ROOT / 'ByGoofish.sh')], input='',
                                       text=True, stdout=subprocess.DEVNULL, timeout=10).returncode, 0)

    def shell(self, body, **kwargs):
        return subprocess.run(['bash', '-c', f'source {str(ROOT / "ByGoofish.sh")!r}\n' + body],
                              text=True, capture_output=True, timeout=15, **kwargs)

    def test_supported_and_rejected_platform_fixtures(self):
        for distro, version, arch, expected in [('debian', '12', 'x86_64', 0), ('debian', '13', 'aarch64', 0),
                ('ubuntu', '22.04', 'x86_64', 0), ('ubuntu', '24.04', 'aarch64', 0),
                ('ubuntu', '20.04', 'x86_64', 1), ('debian', '11', 'x86_64', 1),
                ('alpine', '3.20', 'x86_64', 1), ('ubuntu', '24.04', 'riscv64', 1)]:
            result = self.shell(f'validate_platform {distro} {version} {arch}')
            self.assertEqual(result.returncode, expected, (distro, version, arch, result.stderr))

    def test_low_memory_and_disk_stop_before_installation(self):
        for memory, disk in [(512, 99999), (4096, 100)]:
            result = self.shell(f'load_resources() {{ CPUS=2; MEMORY={memory}; AVAILABLE=100; SWAP=0; DISK={disk}; BROWSERS=1; }}\ncheck_capacity')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('不足' if memory < 960 else '6GB', result.stderr)

    def test_failure_cleanup_never_restores_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            events = Path(temporary) / 'events'
            result = self.shell(f'''LOG=/dev/null
STOPPED=yes; STARTED=yes; WORK=''
systemctl() {{ echo "$*" >> {str(events)!r}; }}
trap deployment_cleanup EXIT
false
''')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('不自动覆盖数据库', result.stdout)
            self.assertFalse(events.exists())

    def test_port_collision(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
            result = self.shell(f'HOST=127.0.0.1; PORT={port}; LOG=/dev/null; check_port')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('占用', result.stderr)

    def test_default_credentials_are_terminal_only_and_only_for_fresh_database(self):
        # No real login: stub only the explicit initializer credential check.
        body = '''LOG=/dev/null; BACKUP=''; HOST=127.0.0.1; PORT=8080
systemctl() { :; }
wait_health() { return 0; }
show_access() { :; }
python3() { [[ $2 == default-login ]]; }
finish_deployment yes FRESH
'''
        fresh = self.shell(body.replace('FRESH', 'yes'))
        existing = self.shell(body.replace('FRESH', 'no'))
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertIn('默认账号：admin', fresh.stdout)
        self.assertIn('默认密码：admin123', fresh.stdout)
        self.assertNotIn('admin123', existing.stdout)
        self.assertIn('原来的登录账号和密码', existing.stdout)

    def test_failed_node_checksum_never_extracts_or_switches_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            body = f'''STATE={temporary!r}; WORK="$STATE/work"; mkdir -p "$WORK"; ARCH=x86_64
node() {{ echo v24.0.0; }}
download() {{
 case "$1" in
  */index.json) echo '[{{"version":"v22.9.0","lts":"test"}}]' > "$2" ;;
  */SHASUMS256.txt) echo '0000000000000000000000000000000000000000000000000000000000000000  node-v22.9.0-linux-x64.tar.xz' > "$2" ;;
  *) echo 'damaged archive' > "$2" ;;
 esac
}}
prepare_node
'''
            result = self.shell(body)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('校验失败', result.stderr)
            self.assertFalse((Path(temporary) / 'tools/node22').exists())

    def test_logs_ctrl_c_returns_to_menu_and_redacts_legacy_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / 'systemctl').write_text(f'''#!/bin/sh
printf '%s\\n' '{ROOT}'
''')
            (directory / 'journalctl').write_text('''#!/bin/sh
echo 'Cookie: fixture-secret'
exec python3 -c 'import time; time.sleep(60)'
''')
            for file in directory.iterdir():
                file.chmod(0o755)
            pid, master = pty.fork()
            if pid == 0:
                os.execvpe('bash', ['bash', str(ROOT / 'ByGoofish.sh')],
                           {**os.environ, 'PATH': str(directory) + ':' + os.environ['PATH'], 'TERM': 'xterm'})
            output = bytearray()

            def read_until(needle, timeout=6):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if select.select([master], [], [], .1)[0]:
                        try:
                            output.extend(os.read(master, 8192))
                        except OSError:
                            break
                        if needle in output:
                            return True
                return False

            try:
                self.assertTrue(read_until('请选择'.encode()))
                os.write(master, b'5\n')
                self.assertTrue(read_until('已脱敏'.encode()))
                self.assertNotIn(b'fixture-secret', output)
                output.clear()
                os.write(master, b'\x03')
                self.assertTrue(read_until('请选择'.encode()), output.decode(errors='replace'))
                self.assertNotIn(b'fixture-secret', output)
                os.write(master, b'0\n')
                self.assertTrue(read_until('已退出'.encode()))
            finally:
                try:
                    os.killpg(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                os.close(master)
                os.waitpid(pid, 0)


class UpdateSafety(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='bygoofish-update-tests-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.remote, self.live = self.base / 'remote', self.base / 'live'
        self.remote.mkdir()
        git(self.remote, 'init', '-b', 'main')
        git(self.remote, 'config', 'user.email', 'fixture@example.invalid')
        git(self.remote, 'config', 'user.name', 'Fixture')
        for directory in ('scripts', 'frontend', 'static'):
            (self.remote / directory).mkdir()
        shutil.copy2(ROOT / 'ByGoofish.sh', self.remote / 'ByGoofish.sh')
        shutil.copy2(ROOT / 'scripts/native_deploy.py', self.remote / 'scripts/native_deploy.py')
        for name in ('Start.py', 'requirements.lock', 'frontend/package-lock.json', 'static/index.html'):
            (self.remote / name).write_text('fixture')
        git(self.remote, 'add', '.')
        git(self.remote, 'commit', '-m', 'fixture base')
        self.old = git(self.remote, 'rev-parse', 'HEAD')
        subprocess.run(['git', 'clone', '-q', str(self.remote), str(self.live)], check=True)
        git(self.live, 'config', 'user.email', 'fixture@example.invalid')
        git(self.live, 'config', 'user.name', 'Fixture')
        self.events = self.base / 'events'

    def advance(self, repo):
        (repo / 'new-code.py').write_text('new version')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'fixture next')

    def update(self, fail=''):
        # The real Git cleanliness/ancestry gates, fast-forward, and EXIT cleanup run.
        # No package installer, real service command, or production DB is reachable.
        body = f'''source {str(self.live / 'ByGoofish.sh')!r}
REMOTE={str(self.remote)!r}
require_root() {{ :; }}
begin_deployment() {{
  STATE={str(self.base / 'state')!r}; mkdir -p "$STATE"; WORK=$(mktemp -d "$STATE/staging-XXXXXX")
  LOG="$STATE/deploy.log"; touch "$LOG"
  HOST=127.0.0.1; PORT=12345; DB={str(self.base / 'db.sqlite')!r}; EXISTS=yes
  RUN_USER=$(id -un); RUN_HOME=$HOME
  STOPPED=no; STARTED=no; WAS_ACTIVE=no; CODE_SWITCHED=no; VENV_SWITCHED=no
  trap deployment_cleanup EXIT
}}
record() {{ echo "$*" >> {str(self.events)!r}; }}
load_resources() {{ DISK={'100' if fail == 'disk' else '99999'}; }}
check_capacity() {{ record capacity; }}
configure_access() {{ :; }}
install_system_packages() {{ record install; }}
prepare_python() {{ NEXT_VENV="$STATE/environment"; record python; }}
prepare_node() {{ :; }}
build_frontend() {{ record build; {'return 1' if fail == 'build' else ':'}; }}
prepare_browser() {{ :; }}
preflight() {{ record preflight; }}
service_active() {{ return 0; }}
systemctl() {{ record "service $*"; }}
check_port() {{ :; }}
save_backup() {{ BACKUP="$STATE/backup"; mkdir -p "$BACKUP"; record backup; }}
switch_venv() {{ record switch; {'return 1' if fail == 'switch' else ':'}; }}
install_service() {{ record unit; }}
finish_deployment() {{ STARTED=yes; record healthy; }}
logged() {{
 if [[ "$*" == *'merge --ff-only'* && {fail!r} == concurrent ]]; then printf 'operator edit' > "$ROOT/Start.py"; fi
 if [[ "$*" == *publish_frontend.py* ]]; then record publish; else "$@" >> "$LOG" 2>&1; fi
}}
update_latest
'''
        return subprocess.run(['bash', '-c', body], capture_output=True, text=True, timeout=30)

    def test_up_to_date_does_not_install_or_restart(self):
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('已是最新版', result.stdout)
        self.assertFalse(self.events.exists())
        git(self.remote, 'branch', '-m', 'unpublished')
        result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('main 分支尚未发布', result.stderr)
        self.assertFalse(self.events.exists())

    def test_local_ahead_and_dirty_are_not_overwritten(self):
        self.advance(self.live)
        ahead = git(self.live, 'rev-parse', 'HEAD')
        result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('未发布', result.stderr)
        self.assertEqual(git(self.live, 'rev-parse', 'HEAD'), ahead)
        self.assertFalse(self.events.exists())
        (self.live / 'Start.py').write_text('personal changes')
        result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('未提交', result.stderr)
        self.assertEqual((self.live / 'Start.py').read_text(), 'personal changes')

    def test_build_failure_keeps_running_service_and_code(self):
        self.advance(self.remote)
        result = self.update('build')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(git(self.live, 'rev-parse', 'HEAD'), self.old)
        self.assertNotIn('service stop', self.events.read_text())

    def test_download_space_failure_keeps_code_and_service(self):
        self.advance(self.remote)
        result = self.update('disk')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('6GB', result.stderr)
        self.assertEqual(git(self.live, 'rev-parse', 'HEAD'), self.old)
        self.assertFalse(self.events.exists())

    def test_switch_failure_restores_code_before_old_service_start(self):
        self.advance(self.remote)
        result = self.update('switch')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(git(self.live, 'rev-parse', 'HEAD'), self.old)
        self.assertEqual(git(self.live, 'status', '--porcelain'), '')
        self.assertFalse((self.live / 'new-code.py').exists())
        events = self.events.read_text()
        self.assertIn('service stop bygoofish.service', events)
        self.assertIn('service start bygoofish.service', events)

    def test_successful_update_fast_forwards_once_and_checks_before_stopping(self):
        self.advance(self.remote)
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(git(self.live, 'rev-parse', 'HEAD'), git(self.remote, 'rev-parse', 'HEAD'))
        events = self.events.read_text().splitlines()
        self.assertLess(events.index('preflight'), events.index('service stop bygoofish.service'))
        self.assertLess(events.index('backup'), events.index('switch'))
        self.assertEqual(events.count('healthy'), 1)

    def test_concurrent_operator_edit_is_preserved_when_merge_is_rejected(self):
        (self.remote / 'Start.py').write_text('new upstream code')
        git(self.remote, 'add', '.')
        git(self.remote, 'commit', '-m', 'upstream modifies entrypoint')
        result = self.update('concurrent')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(git(self.live, 'rev-parse', 'HEAD'), self.old)
        self.assertEqual((self.live / 'Start.py').read_text(), 'operator edit')
        self.assertIn('已保留改动', result.stdout)
        self.assertNotIn('service start', self.events.read_text())

    def test_no_service_choice_completes_install_without_starting_background_process(self):
        body = f'''source {str(self.live / 'ByGoofish.sh')!r}
STATE={str(self.base / 'state')!r}; WORK="$STATE/staging-fixture"; mkdir -p "$WORK"
LOG="$STATE/log"; DB="$STATE/fresh.db"; HOST=0.0.0.0; PORT=8080; EXISTS=no
RUN_USER=$(id -un); RUN_HOME=$HOME; BROWSERS=1
begin_deployment() {{ :; }}
check_capacity() {{ :; }}
configure_access() {{ :; }}
check_database_access() {{ :; }}
confirm() {{ return 1; }}
install_system_packages() {{ :; }}
prepare_python() {{ NEXT_VENV="$STATE/environment"; }}
prepare_node() {{ :; }}
build_frontend() {{ :; }}
prepare_browser() {{ :; }}
preflight() {{ :; }}
check_port() {{ :; }}
save_backup() {{ :; }}
switch_venv() {{ :; }}
logged() {{ :; }}
systemctl() {{ echo 'unexpected service call' >&2; return 99; }}
finish_deployment() {{ printf 'create=%s fresh=%s\\n' "$1" "$2"; }}
first_install
'''
        result = subprocess.run(['bash', '-c', body], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('create=no fresh=yes', result.stdout)
        self.assertNotIn('unexpected service call', result.stderr)
        saved = json.loads((self.live / '.local/bygoofish/settings.json').read_text())
        self.assertEqual(saved['host'], '0.0.0.0')


if __name__ == '__main__':
    unittest.main()
