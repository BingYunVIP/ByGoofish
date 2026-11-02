"""用当前 Python 环境检测浏览器；自检只渲染本地 HTML，不访问闲鱼。"""

from importlib import metadata
from pathlib import Path
import os
import re
import shlex
import sys

from utils.direct_network import direct_browser_options

RUNTIME_FAILURES = frozenset(('browser_error', 'browser_missing', 'browser_dependency_missing',
                             'browser_permission_denied', 'browser_launch_failed'))
_runtime_status = {'ready': None, 'failure_category': '', 'message': ''}


class BrowserRuntimeError(RuntimeError):
    def __init__(self, category, message):
        self.category = category
        super().__init__(message)


def browser_failure(error):
    """Return bounded, credential-free diagnostics instead of dumping Playwright errors."""
    if isinstance(error, BrowserRuntimeError):
        return error.category, str(error)
    detail = str(error).lower()
    if isinstance(error, PermissionError) or any(x in detail for x in ('permission denied', 'eacces', 'read-only file system')):
        return 'browser_permission_denied', '服务用户无法访问浏览器或写入运行目录；请检查浏览器路径、目录权限和服务可写目录'
    if "executable doesn't exist" in detail:
        return 'browser_missing', '服务用户缺少项目 Playwright 对应的浏览器；请通过原生部署脚本安装匹配浏览器'
    if any(x in detail for x in ('shared libraries', 'host system is missing', 'libnss3.so')):
        return 'browser_dependency_missing', '本机缺少浏览器所需的系统库；请通过原生部署脚本补齐依赖'
    return 'browser_launch_failed', '本机浏览器启动或运行失败；请检查服务权限、显示环境、内存及本地诊断'


def runtime_status():
    return dict(_runtime_status)


def remember_runtime_report(report):
    failures = report.get('failures', [])
    _runtime_status.update(ready=not bool(report.get('errors')),
                          failure_category=failures[0]['category'] if failures else ('browser_error' if report.get('errors') else ''),
                          message=failures[0]['message'] if failures else ('浏览器自检未通过，请查看本机诊断' if report.get('errors') else '本机浏览器离线自检通过；不代表闲鱼账号已恢复'))


def require_browser_binary(playwright):
    """Check the full Chromium binary before making any platform request."""
    executable = playwright.chromium.executable_path
    # Protocol mocks do not represent a filesystem; production always returns str.
    if not isinstance(executable, (str, Path)):
        return
    path = Path(executable)
    try:
        exists = path.is_file()
    except PermissionError:
        raise BrowserRuntimeError(*browser_failure(PermissionError())) from None
    if not exists:
        # exists() may hide EACCES on newer Python versions. Check parent traversal.
        for parent in path.parents:
            if parent.exists() and not os.access(parent, os.X_OK):
                raise BrowserRuntimeError(*browser_failure(PermissionError()))
        raise BrowserRuntimeError(*browser_failure(RuntimeError("Executable doesn't exist")))
    if not os.access(path, os.X_OK):
        raise BrowserRuntimeError(*browser_failure(PermissionError()))


def required_version():
    requirements = Path(__file__).resolve().parents[1] / 'requirements.txt'
    match = re.search(r'^playwright==([^\s#]+)', requirements.read_text(encoding='utf-8'), re.M)
    if not match:
        raise RuntimeError('requirements.txt 必须固定 playwright 版本')
    return match.group(1)


def browser_error_hint(error):
    detail = str(error)
    if "Executable doesn't exist" in detail:
        return '缺少当前 Python Playwright 对应的浏览器，请运行诊断脚本 --install'
    if any(text in detail for text in ('shared libraries', 'Host system is missing', 'libnss3.so')):
        return '缺少 Linux 系统库，请运行诊断脚本 --install --with-deps'
    if 'does not support' in detail or 'not supported' in detail:
        return '当前系统不受此版本支持，请参阅 Custom/PLAYWRIGHT.md 的系统兼容说明'
    return browser_failure(error)[1]


def check_runtime():
    report = {'python': sys.executable, 'version': None, 'executable': None,
              'checks': [], 'errors': [], 'failures': []}
    try:
        report['version'] = metadata.version('playwright')
        from playwright.sync_api import sync_playwright
    except (metadata.PackageNotFoundError, ImportError):
        report['errors'].append('当前 Python 未安装 playwright，请运行诊断脚本 --install')
        return report

    expected = required_version()
    if report['version'] != expected:
        report['errors'].append(f"Playwright 版本不匹配：当前 {report['version']}，项目要求 {expected}")
        return report

    try:
        with sync_playwright() as playwright:
            report['executable'] = playwright.chromium.executable_path
            # 项目同时使用完整 Chromium 和默认 headless shell，必须分别验证。
            modes = [('Chromium', 'chromium', True), ('Headless Shell', None, True)]
            if os.getenv('DISPLAY'):
                modes.append(('Chromium Display', 'chromium', False))
            for label, channel, headless in modes:
                try:
                    with playwright.chromium.launch(**direct_browser_options({
                        'headless': headless, 'channel': channel, 'timeout': 15000,
                        'args': ['--disable-dev-shm-usage'],
                    })) as browser:
                        page = browser.new_page()
                        page.set_content('<title>offline-smoke</title><h1>OK</h1>', timeout=5000)
                        if page.title() != 'offline-smoke':
                            raise RuntimeError('本地 HTML 渲染失败')
                        report['checks'].append(f'{label} {browser.version}：离线启动与渲染通过')
                except Exception as error:
                    category, message = browser_failure(error)
                    report['failures'].append({'category':category, 'message':message})
                    report['errors'].append(f'{label}：{browser_error_hint(error)}')
    except Exception as error:
        category, message = browser_failure(error)
        report['failures'].append({'category':category, 'message':message})
        report['errors'].append(browser_error_hint(error))
    return report


def print_report(report):
    print(f"Python: {report['python']}")
    print(f"Playwright: {report['version'] or '未安装'}")
    if report['executable']:
        print(f"匹配浏览器: {report['executable']}")
    for result in report['checks']:
        print(f'[OK] {result}')
    for error in report['errors']:
        print(f'[ERROR] {error}')
    if report['errors']:
        print(f'修复命令: {shlex.quote(sys.executable)} scripts/playwright_doctor.py --install')
        print('Ubuntu / Debian 缺少系统库时增加 --with-deps；详见 Custom/PLAYWRIGHT.md')

