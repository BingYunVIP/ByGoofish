"""用当前 Python 环境检测浏览器；自检只渲染本地 HTML，不访问闲鱼。"""

from importlib import metadata
from pathlib import Path
import re
import shlex
import sys

from utils.direct_network import direct_browser_options


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
    return f'浏览器启动失败（{type(error).__name__}），请检查权限、内存和系统依赖'


def check_runtime():
    report = {'python': sys.executable, 'version': None, 'executable': None,
              'checks': [], 'errors': []}
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
            for label, channel in (('Chromium', 'chromium'), ('Headless Shell', None)):
                try:
                    with playwright.chromium.launch(**direct_browser_options({
                        'headless': True, 'channel': channel, 'timeout': 15000,
                        'args': ['--disable-dev-shm-usage'],
                    })) as browser:
                        page = browser.new_page()
                        page.set_content('<title>offline-smoke</title><h1>OK</h1>', timeout=5000)
                        if page.title() != 'offline-smoke':
                            raise RuntimeError('本地 HTML 渲染失败')
                        report['checks'].append(f'{label} {browser.version}：离线启动与渲染通过')
                except Exception as error:
                    report['errors'].append(f'{label}：{browser_error_hint(error)}')
    except Exception as error:
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

