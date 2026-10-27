#!/usr/bin/env python3
"""把已验收的前端构建发布到 static/：先复制带哈希的新资源，再原子切换入口，保留上一代。

用法：
    VITE_OUT_DIR=/tmp/frontend-stage npm run build        # 在 frontend 目录构建到临时目录并验收
    python scripts/publish_frontend.py /tmp/frontend-stage [--dry-run]

* 新资源先全部复制完，再用 os.replace 原子替换 index.html，运行中的服务不会读到半成品；
* 只保留「新一代」和「发布前正在使用的一代」引用到的资源，更早的一代才会被清理，出问题可以直接换回旧入口；
* 不碰 static 下后端自有的文件（uploads、xianyu_js_version_2.js 等）。
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
ASSET_NAME = re.compile(r'[A-Za-z0-9_.\-]+\.(?:js|css)')


def reachable(entry: Path, assets: Path) -> set:
    """从入口文件出发，沿着文本里出现的资源文件名一路找到所有会被加载的资源。"""
    found, queue = set(), [entry]
    while queue:
        current = queue.pop()
        try:
            text = current.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        for name in ASSET_NAME.findall(text):
            path = assets / name
            if name not in found and path.is_file():
                found.add(name)
                queue.append(path)
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('stage', type=Path, help='前端构建输出目录（含 index.html 和 assets/）')
    parser.add_argument('--static-dir', type=Path, default=Path(os.getenv('APP_STATIC_DIR', ROOT / 'static')))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    stage, static = args.stage.resolve(), args.static_dir.resolve()
    stage_assets, live_assets = stage / 'assets', static / 'assets'
    if not (stage / 'index.html').is_file() or not stage_assets.is_dir():
        sys.exit('构建目录不完整：缺少 index.html 或 assets/')
    live_assets.mkdir(parents=True, exist_ok=True)

    new_set = reachable(stage / 'index.html', stage_assets)
    missing = [name for name in new_set if not (stage_assets / name).is_file()]
    old_entry = static / 'index.html'
    old_set = reachable(old_entry, live_assets) if old_entry.is_file() else set()
    keep = new_set | old_set
    stale = sorted(path.name for path in live_assets.iterdir() if path.is_file() and path.name not in keep)
    to_copy = sorted(name for name in new_set if not (live_assets / name).is_file())
    print(f'新一代资源 {len(new_set)} 个（需复制 {len(to_copy)} 个），保留上一代 {len(old_set - new_set)} 个，将清理更早一代 {len(stale)} 个')
    if missing:
        sys.exit('构建目录缺少入口引用的资源：' + ', '.join(missing[:5]))
    if args.dry_run:
        return

    for name in to_copy:
        shutil.copy2(stage_assets / name, live_assets / name)
    for name in ('favicon.svg',):
        if (stage / name).is_file():
            shutil.copy2(stage / name, static / name)
    temporary = static / 'index.html.new'
    shutil.copy2(stage / 'index.html', temporary)
    os.replace(temporary, old_entry)  # 原子切换入口
    for name in stale:
        (live_assets / name).unlink()
    print('已发布：入口已切换，上一代资源保留，更早一代已清理')


if __name__ == '__main__':
    main()
