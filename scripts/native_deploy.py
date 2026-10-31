#!/usr/bin/env python3
"""Native menu support. Stdlib only; never import the application in this process."""
import argparse
import fcntl
import ipaddress
import json
import math
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import build_opener, ProxyHandler, Request

SERVICE = "bygoofish.service"
SERVICE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
PROTECTED_TABLES = ("users", "cookies", "orders", "cards")


def command(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def systemd_values(text):
    """Read only assignments, never execute an EnvironmentFile as shell code."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        parts = shlex.split(value, comments=False)
        values[key] = " ".join(parts)
    return values


def environment_files(value, *, required_only=False):
    paths = []
    for item, ignore in re.findall(r"(.+?)\s+\(ignore_errors=(yes|no)\)(?:\s+|$)", value):
        if required_only and ignore != 'no':
            continue
        item = item.strip().strip('"').lstrip("-")
        # systemctl escapes whitespace in some output modes.
        item = re.sub(r"\\x([0-9a-fA-F]{2})", lambda match: chr(int(match[1], 16)), item)
        paths.append(Path(item))
    return paths


def settings(root):
    owner = pwd.getpwuid(root.stat().st_uid)
    result = dict(host="127.0.0.1", port="8080", db=str(root / "data/xianyu_data.db"),
                  user=owner.pw_name, home=owner.pw_dir, exists="no", browser_path=os.getenv("PLAYWRIGHT_BROWSERS_PATH", ""),
                  service_path=SERVICE_PATH)
    saved = root / ".local/bygoofish/settings.json"
    if saved.is_file():
        result.update({k: str(v) for k, v in json.loads(saved.read_text()).items() if k in result and k != "exists"})
    if shutil.which("systemctl"):
        proc = subprocess.run(["systemctl", "show", SERVICE, "--no-pager",
                               "-p", "LoadState", "-p", "Environment", "-p", "EnvironmentFiles",
                               "-p", "User", "-p", "WorkingDirectory"], capture_output=True, text=True)
        data = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
        if proc.returncode == 0 and data.get("LoadState") not in (None, "not-found"):
            if Path(data.get("WorkingDirectory") or "/").resolve() != root:
                raise ValueError("已有同名服务属于其他目录；请先处理服务冲突")
            result["exists"] = "yes"
            env = dict(part.split("=", 1) for part in shlex.split(data.get("Environment", "")) if "=" in part)
            # systemctl prints path (ignore_errors=yes/no). Quote-aware tokenization also handles spaces.
            required = set(environment_files(data.get("EnvironmentFiles", ""), required_only=True))
            for path in environment_files(data.get("EnvironmentFiles", "")):
                if path.is_file():
                    env.update(systemd_values(path.read_text()))
                elif path in required:
                    raise ValueError('现有服务缺少必需环境文件，请先恢复该配置；不会用默认配置覆盖')
            for key, variable in (("host", "API_HOST"), ("port", "API_PORT"), ("db", "DB_PATH")):
                if env.get(variable):
                    result[key] = env[variable]
            result["user"] = data.get("User") or "root"
            result["home"] = env.get("HOME") or pwd.getpwnam(result["user"]).pw_dir
            result["browser_path"] = env.get("PLAYWRIGHT_BROWSERS_PATH", "")
            result["service_path"] = env.get("PATH") or SERVICE_PATH
    validate_settings(result, root)
    return result


def validate_settings(data, root):
    if data["host"] != "localhost":
        ipaddress.ip_address(data["host"])
    if not str(data["port"]).isdigit() or not 1 <= int(data["port"]) <= 65535:
        raise ValueError("服务端口必须在1–65535之间")
    for key in ("db", "home", "user"):
        if any(c in data[key] for c in "\n\r\x00"):
            raise ValueError("配置中存在不支持的控制字符")
    db = Path(data["db"])
    data["db"] = str((root / db).resolve() if not db.is_absolute() else db.resolve())


def profile(cpus, memory):
    if cpus <= 2 or memory <= 4.5 * 1024:
        return 1
    if cpus <= 4 or memory <= 8.5 * 1024:
        return 2
    return 3


def resources(root):
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        memory[key] = int(value.strip().split()[0]) // 1024
    cpus = len(os.sched_getaffinity(0))
    total = memory["MemTotal"]
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            cpus = min(cpus, max(1, math.ceil(int(quota) / int(period))))
        limit = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if limit != "max":
            total = min(total, int(limit) // 1024 ** 2)
    except (OSError, ValueError):
        pass
    return dict(cpus=cpus, memory=total, available=memory["MemAvailable"],
                swap=memory["SwapTotal"], disk=shutil.disk_usage(root).free // 1024 ** 2,
                browsers=profile(cpus, total))


def snapshot(source, destination):
    source, destination = Path(source).resolve(), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("SQLite一致性校验失败")
    destination.chmod(0o600)


def assert_stopped(db):
    lock = Path(str(db) + ".runtime.lock")
    if lock.exists():
        with lock.open("r+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("数据库仍由业务进程使用，已停止部署") from error


def backup(root, destination, db):
    destination = destination.resolve()
    if destination == root or root in destination.parents or destination.exists():
        raise ValueError("备份必须写到仓库外尚不存在的目录")
    destination.mkdir(parents=True, mode=0o700)
    (destination / ".bygoofish-backup").write_text(str(root))
    excluded = {".git", ".venv", ".local", "node_modules", "__pycache__", ".cache", "data", "static",
                "logs", "browser_data", "Custom", "Audit", "trajectory_history", "backups"}

    def code_filter(member):
        return None if any(part in excluded for part in Path(member.name).parts) else member

    with tarfile.open(destination / "code.tar", "w") as archive:
        for path in root.iterdir():
            if path.name not in excluded:
                archive.add(path, arcname=path.name, filter=code_filter)
    if (root / ".git").exists():
        command(["git", "-c", f"safe.directory={root}", "-C", str(root), "bundle", "create", str(destination / "repository.bundle"), "--all"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with (destination / "working.patch").open("wb") as output:
            command(["git", "-c", f"safe.directory={root}", "-C", str(root), "diff", "--binary", "HEAD"], stdout=output)
    names = {"global_config.yml", ".env", "config.local.yml", "global_config.local.yml", "browser_data", "static"}
    names.update(path.name for path in root.glob(".env.*") if path.is_file())
    for name in names:
        path = root / name
        if path.is_dir():
            shutil.copytree(path, destination / name, symlinks=True)
        elif path.is_file():
            shutil.copy2(path, destination / name)
    if db.is_file():
        snapshot(db, destination / "database.db")
    # Keep auxiliary data, but never copy a live WAL database as ordinary bytes.
    data_dir = root / "data"
    if data_dir.exists():
        for path in data_dir.rglob("*"):
            if not path.is_file() or path.resolve() == db.resolve() or path.is_symlink():
                continue
            if path.name.endswith(("-wal", "-shm", "-journal", ".runtime.lock")) or "_backup_" in path.name:
                continue
            output = destination / "data" / path.relative_to(data_dir)
            if path.suffix in (".db", ".sqlite", ".sqlite3"):
                snapshot(path, output)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, output)
    private = root / ".local/bygoofish"
    for name in ("settings.json", "service.env"):
        if (private / name).is_file():
            shutil.copy2(private / name, destination / name)
    for path in (Path("/etc/systemd/system/bygoofish.service"),
                 Path("/etc/systemd/system/bygoofish.service.d")):
        if path.is_dir():
            shutil.copytree(path, destination / path.name)
        elif path.is_file():
            shutil.copy2(path, destination / path.name)
    if shutil.which("systemctl"):
        proc = subprocess.run(["systemctl", "show", SERVICE, "-p", "EnvironmentFiles", "--value"],
                              capture_output=True, text=True)
        files = {}
        for index, path in enumerate(environment_files(proc.stdout.strip())):
            if path.is_file():
                name = f"environment-{index}.conf"
                shutil.copy2(path, destination / name)
                files[name] = str(path)
        (destination / "environment-files.json").write_text(json.dumps(files, indent=2) + "\n")
    manifest = dict(root=str(root), db=str(db), head=git_head(root) if (root / '.git').exists() else None,
                    venv=str((root / ".venv").resolve()), created=time.time())
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for path in destination.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o700 if path.is_dir() else 0o600)
    (destination / "complete").touch(mode=0o600)


def git_head(root):
    return subprocess.check_output(["git", "-c", f"safe.directory={root}", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()


def prune(root, keep):
    """Only prune backups produced by this manager, never arbitrary sibling files."""
    keep = keep.resolve()
    if not (keep / "complete").is_file() or (keep / ".bygoofish-backup").read_text() != str(root):
        raise ValueError("新备份不完整，保留全部旧备份")
    for path in keep.parent.glob("update-*"):
        marker = path / ".bygoofish-backup"
        if path != keep and path.is_dir() and not path.is_symlink() and marker.is_file() and marker.read_text() == str(root):
            shutil.rmtree(path)


def record_runtime(backup_dir, previous):
    path = backup_dir / 'manifest.json'
    data = json.loads(path.read_text())
    data['venv'] = str(previous.resolve()) if previous else None
    path.write_text(json.dumps(data, indent=2) + '\n')


def prune_envs(root, backup_dir):
    directory = root / '.local/bygoofish/envs'
    previous = json.loads((backup_dir / 'manifest.json').read_text()).get('venv')
    keep = {(root / '.venv').resolve()}
    if previous:
        keep.add(Path(previous).resolve())
    for path in directory.glob('py*'):
        # Only installer-created complete environments; never arbitrary folders or symlinks.
        if path.is_symlink() or path.resolve() in keep or not (path / '.complete').is_file():
            continue
        busy = False
        for cmdline in Path('/proc').glob('[0-9]*/cmdline'):
            try:
                if os.fsencode(str(path)) + b'/' in cmdline.read_bytes():
                    busy = True
                    break
            except (OSError, PermissionError):
                continue
        if not busy:
            shutil.rmtree(path)


def quote_unit(value, specifiers=True):
    if any(c in str(value) for c in "\n\r\x00"):
        raise ValueError("服务路径包含控制字符")
    value = str(value).replace("\\", "\\\\").replace('"', '\\"')
    if specifiers:
        value = value.replace("%", "%%")
    return '"' + value + '"'


def write_service(root, directory, host, port, db, user, home, browsers, browser_path="", node_bin="", service_path=""):
    runtime_path = service_path or SERVICE_PATH
    if node_bin:
        runtime_path = node_bin + ':' + ':'.join(part for part in runtime_path.split(':') if part != node_bin)
    data = dict(host=host, port=port, db=str(db), user=user, home=home, exists="yes", browser_path=browser_path,
                service_path=runtime_path)
    validate_settings(data, root)
    private = root / ".local/bygoofish"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    (private / "settings.json").write_text(json.dumps(data, indent=2) + "\n")
    env = dict(API_HOST=host, API_PORT=port, DB_PATH=str(db), WEB_CONCURRENCY="1",
               MAX_CONCURRENT_BROWSERS=str(browsers), PYTHONDONTWRITEBYTECODE="1",
               SQL_LOG_ENABLED="false", HOME=home, TZ="Asia/Shanghai", PATH=runtime_path,
               BYGOOFISH_MASTER_KEY_FILE=str(private / 'master.key'))
    if browser_path:
        env["PLAYWRIGHT_BROWSERS_PATH"] = browser_path
    (private / "service.env").write_text("".join(f"{k}={quote_unit(v, specifiers=False)}\n" for k, v in env.items()))
    master = private / 'master.key'
    if not master.exists():
        import base64, secrets
        master.write_text(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii') + '\n')
    master.chmod(0o600)
    for path in (private / "settings.json", private / "service.env", master):
        path.chmod(0o600)
    directory.mkdir(parents=True, exist_ok=True)
    text = ("[Unit]\nStartLimitIntervalSec=300\nStartLimitBurst=3\n\n[Service]\n"
            f"WorkingDirectory={str(root).replace('%', '%%')}\nExecStart=\n"
            f"ExecStart=/usr/bin/xvfb-run -a -s \"-screen 0 1920x1080x24\" {quote_unit(root / '.venv/bin/python')} -u {quote_unit(root / 'Start.py')}\n"
            f"User={user}\nEnvironmentFile={str(private / 'service.env').replace('%', '%%')}\n"
            "Restart=on-failure\nRestartSec=15\nKillMode=control-group\nKillSignal=SIGINT\n"
            "TimeoutStopSec=45\nUMask=0077\nNoNewPrivileges=true\n"
            "PrivateTmp=true\nProtectSystem=strict\nProtectHome=read-only\n"
            "ProtectKernelTunables=true\nProtectKernelModules=true\nProtectControlGroups=true\n"
            "RestrictSUIDSGID=true\nLockPersonality=true\nRestrictRealtime=true\n"
            f"ReadWritePaths={str(root).replace('%','%%').replace(' ','\\x20')}/data "
            f"{str(root).replace('%','%%').replace(' ','\\x20')}/logs "
            f"{str(root).replace('%','%%').replace(' ','\\x20')}/browser_data "
            f"{str(root).replace('%','%%').replace(' ','\\x20')}/static/uploads "
            f"{str(root).replace('%','%%').replace(' ','\\x20')}/.local\n")
    (directory / "90-bygoofish-native.conf").write_text(text)


def restore_service(root, backup_dir, base_created, unit_dir=Path("/etc/systemd/system")):
    if (backup_dir / '.bygoofish-backup').read_text() != str(root) or not (backup_dir / 'complete').is_file():
        raise ValueError('服务配置回退要求完整的本项目备份')
    unit = unit_dir / SERVICE
    if base_created and unit.is_file():
        if 'Description=ByGoofish native service' not in unit.read_text():
            raise ValueError('服务文件已被其他操作修改，不自动删除')
        unit.unlink()
    managed = Path(SERVICE + '.d') / '90-bygoofish-native.conf'
    for source, target in [(backup_dir / managed, unit_dir / managed),
                           *((backup_dir / name, root / '.local/bygoofish' / name)
                             for name in ('settings.json', 'service.env'))]:
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        else:
            target.unlink(missing_ok=True)


def database_counts(path):
    if not path.is_file():
        return {}
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                for name in PROTECTED_TABLES if name in names}


def probe(root):
    """Child only: isolated HTTP app, no Start.py, CookieManager, or external sockets."""
    work = Path.cwd()
    if not work.name.startswith("bygoofish-preflight-") or Path(os.environ["DB_PATH"]).parent != work:
        raise ValueError("拒绝在非隔离数据库上运行预检")
    sys.path.insert(0, str(root))
    before = database_counts(Path(os.environ["DB_PATH"]))
    real_connect = socket.socket.connect

    def local_connect(sock, address):
        if sock.family != socket.AF_UNIX:
            if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "::1", "localhost"):
                raise OSError("部署预检禁止访问外部网络")
        return real_connect(sock, address)

    socket.socket.connect = local_connect
    real_resolve = socket.getaddrinfo

    def local_resolve(host, *args, **kwargs):
        if host not in (None, "127.0.0.1", "::1", "localhost"):
            raise OSError("部署预检禁止外部DNS查询")
        return real_resolve(host, *args, **kwargs)

    socket.getaddrinfo = local_resolve
    from fastapi.testclient import TestClient
    from app.reply_server import app
    from app import cookie_manager
    if cookie_manager.manager is not None:
        raise ValueError("预检不得启动真实账号管理器")
    with TestClient(app) as client:
        response = client.get("/health")
        # A missing CookieManager intentionally makes health 503 in this API-only process.
        if (response.status_code not in (200, 503) or response.json().get("services", {}).get("database") != "ok"
                or client.get("/").status_code != 200):
            raise ValueError("隔离HTTP健康或前端入口检查失败")
    after = database_counts(Path(os.environ["DB_PATH"]))
    if any(after.get(name, -1) < count for name, count in before.items()):
        raise ValueError("数据库升级减少了受保护的业务记录")


def preflight(root, python, static, db, log):
    log.touch(mode=0o600, exist_ok=True)
    log.chmod(0o600)
    with tempfile.TemporaryDirectory(prefix="bygoofish-preflight-") as temporary:
        work = Path(temporary)
        cases = (False, True) if db.is_file() else (False,)
        with log.open("ab") as output:
            for existing in cases:
                target = work / ("existing.db" if existing else "fresh.db")
                if existing:
                    snapshot(db, target)
                env = {**os.environ, "DB_PATH": str(target), "PYTHONPATH": str(root),
                       "APP_STATIC_DIR": str(static), "APP_LOG_PATH": str(work / "app.log"),
                       "SQL_LOG_ENABLED": "false", "ACCOUNT_OFFLINE_MONITOR_ENABLED": "false",
                       "PYTHONDONTWRITEBYTECODE": "1", "ADMIN_PASSWORD": "deployment-fixture-only"}
                env.pop("COOKIES_STR", None)
                command([str(python), "-B", str(Path(__file__).resolve()), "probe", str(root)],
                        cwd=work, env=env, stdout=output, stderr=output, timeout=120)


def node_release(index):
    releases = json.loads(index.read_text())
    return next(item["version"] for item in releases if item["version"].startswith("v22.") and item.get("lts"))


def public_address(family=4):
    result = subprocess.run(['ip', '-j', 'addr', 'show', 'scope', 'global'], capture_output=True, text=True)
    if result.returncode != 0:
        return ''
    addresses = [ipaddress.ip_address(info['local']) for interface in json.loads(result.stdout)
                 for info in interface.get('addr_info', []) if info.get('family') in ('inet', 'inet6')]
    return next((str(address) for address in addresses if address.is_global and address.version == family), '')


def lock_check(lock):
    from importlib.metadata import version, PackageNotFoundError
    for line in lock.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+", line):
            raise ValueError("requirements.lock 必须使用固定版本")
        name, expected = line.split("==")
        try:
            if version(name) != expected:
                raise ValueError("依赖版本与锁定文件不一致")
        except PackageNotFoundError as error:
            raise ValueError("缺少锁定依赖") from error


def headed_check(root):
    sys.path.insert(0, str(root))
    from playwright.sync_api import sync_playwright
    from utils.direct_network import direct_browser_options
    with sync_playwright() as player:
        with player.chromium.launch(**direct_browser_options({"headless": False, "timeout": 20000})) as browser:
            page = browser.new_page()
            page.set_content("<title>native-headed-check</title>")
            if page.title() != "native-headed-check":
                raise ValueError("Xvfb浏览器渲染检查失败")


def default_login(health_url):
    url = urlsplit(health_url)
    if url.scheme != "http" or not url.hostname or url.path != "/health":
        raise ValueError("初始化凭据检查地址无效")
    request = Request(urlunsplit((url.scheme, url.netloc, "/login", "", "")),
                      data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
                      headers={"Content-Type": "application/json"})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=10) as response:
            return bool(json.load(response).get("success"))
    except OSError:
        return False


def redact(line):
    # Legacy log lines may predate the application's current redaction policy.
    def safe_url(match):
        try:
            url = urlsplit(match[0])
            host = url.hostname or ''
            if ':' in host:
                host = '[' + host + ']'
            if url.port:
                host += ':' + str(url.port)
            return urlunsplit((url.scheme, host, url.path, '', ''))
        except ValueError:
            return '[链接已脱敏]'

    line = re.sub(r"https?://[^\s<>\"']+", safe_url, line)
    line = re.sub(r"(?i)(authorization|cookie(?:2|s|_str|_value)?|sgcookie|_m_h5_tk(?:_enc)?|unb|_tb_token_|"
                  r"(?:access_|refresh_)?token|password|passwd|api[_-]?key|secret)"
                  r"([\"']?\s*[:=]\s*).*$", r"\1\2[已脱敏]", line)
    line = re.sub(r"(密码[为是：:= ]+).*$", r"\1[已脱敏]", line)
    line = re.sub(r"(?i)((?:cookie|token)(?:字符串|内容|值)|凭证内容|令牌值)(\s*[:：=]\s*).*$",
                  r"\1\2[已脱敏]", line)
    return line.replace("admin123", "[默认密码已隐藏]")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("settings", "resources", "backup", "prune", "assert-stopped", "snapshot",
                                         "service", "preflight", "probe", "node-release", "port", "fresh",
                                         "lock-check", "headed-check", "default-login", "redact-stream", "restore-service",
                                         "record-runtime", "prune-envs", "public-address"))
    parser.add_argument("args", nargs="*")
    args = parser.parse_args()
    values = args.args
    if args.action in ("settings", "resources"):
        root = Path(values[0]).resolve()
        result = settings(root) if args.action == "settings" else resources(root)
        keys = ("host", "port", "db", "user", "home", "exists", "browser_path", "service_path") if args.action == "settings" else (
            "cpus", "memory", "available", "swap", "disk", "browsers")
        print("\n".join(str(result[key]) for key in keys))
    elif args.action == "backup":
        backup(*(Path(v).resolve() for v in values))
    elif args.action == "prune":
        prune(*(Path(v).resolve() for v in values))
    elif args.action == "record-runtime":
        record_runtime(Path(values[0]), Path(values[1]) if values[1] else None)
    elif args.action == "prune-envs":
        prune_envs(*(Path(v).resolve() for v in values))
    elif args.action == "snapshot":
        snapshot(*values)
    elif args.action == "assert-stopped":
        assert_stopped(Path(values[0]))
    elif args.action == "service":
        root, directory, host, port, db, user, home, browsers, *optional = values
        write_service(Path(root), Path(directory), host, port, Path(db), user, home, int(browsers), *optional)
    elif args.action == "restore-service":
        root, backup_dir, base_created = values
        restore_service(Path(root), Path(backup_dir), base_created == "yes")
    elif args.action == "preflight":
        root, python, static, db, log = values
        # Resolving bin/python's symlink would escape the venv and use system Python.
        preflight(Path(root).resolve(), Path(python).absolute(), Path(static).resolve(),
                  Path(db).resolve(), Path(log).absolute())
    elif args.action == "probe":
        probe(Path(values[0]).resolve())
    elif args.action == "node-release":
        print(node_release(Path(values[0])))
    elif args.action == "public-address":
        print(public_address(int(values[0]) if values else 4))
    elif args.action == "lock-check":
        lock_check(Path(values[0]))
    elif args.action == "headed-check":
        headed_check(Path(values[0]))
    elif args.action == "default-login":
        raise SystemExit(0 if default_login(values[0]) else 1)
    elif args.action == "redact-stream":
        try:
            for line in sys.stdin:
                print(redact(line.rstrip("\n")), flush=True)
        except (KeyboardInterrupt, BrokenPipeError):
            raise SystemExit(130)
    elif args.action == "fresh":
        counts = database_counts(Path(values[0]).resolve())
        print("yes" if "users" not in counts or counts["users"] == 0 else "no")
    elif args.action == "port":
        host, port = values
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as listener:
            listener.bind((host, int(port)))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError, StopIteration) as error:
        # Do not print subprocess environments, DB values, or untrusted download output.
        print(f"部署检查失败：{type(error).__name__}；请查看受限权限的部署日志或检查参数。", file=sys.stderr)
        if isinstance(error, ValueError):
            print(str(error), file=sys.stderr)
        raise SystemExit(1)
