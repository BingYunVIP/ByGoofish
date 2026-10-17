#!/usr/bin/env python3
"""检测 / 安装本项目 Python Playwright、匹配 Chromium 与 Linux 系统依赖。"""

import argparse
from importlib import metadata
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.direct_network import direct_environment
from utils.playwright_runtime import check_runtime, print_report, required_version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true', help='安装项目固定版本与对应浏览器，然后离线自检')
    parser.add_argument('--with-deps', action='store_true', help='同时安装系统库（需 --install；Linux 可能需要 sudo）')
    parser.add_argument('--platform', help='显式兼容构建，例如 ubuntu24.04-x64；不代表当前系统获得官方支持')
    args = parser.parse_args()
    if (args.with_deps or args.platform) and not args.install:
        parser.error('--with-deps / --platform 只能与 --install 一起使用')

    if args.install:
        env = direct_environment()
        # 不清除其他 Node / Python 项目共用缓存中的浏览器。
        env['PLAYWRIGHT_SKIP_BROWSER_GC'] = '1'
        env.setdefault('PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT', '30000')
        if args.platform:
            env['PLAYWRIGHT_HOST_PLATFORM_OVERRIDE'] = args.platform
            print(f'[WARN] 使用兼容构建 {args.platform}，必须以实际离线自检结果为准。', flush=True)
        try:
            installed = metadata.version('playwright')
        except metadata.PackageNotFoundError:
            installed = None
        try:
            if installed != required_version():
                subprocess.run([sys.executable, '-m', 'pip', 'install',
                                f'playwright=={required_version()}'], env=env, check=True)
            command = [sys.executable, '-m', 'playwright', 'install']
            if args.with_deps:
                command.append('--with-deps')
            subprocess.run(command + ['chromium'], env=env, check=True)
        except subprocess.CalledProcessError:
            print('[ERROR] 安装失败；请检查上面的下载 / 系统错误及 Custom/PLAYWRIGHT.md。')
            return 1

    report = check_runtime()
    print_report(report)
    return 1 if report['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
