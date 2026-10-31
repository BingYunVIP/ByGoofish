"""应用上传附件的路径边界检查。"""
from __future__ import annotations

from pathlib import Path
import re


def upload_root(project_root: Path | None = None) -> Path:
    root = (project_root or Path(__file__).resolve().parents[1]) / 'static' / 'uploads' / 'images'
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def safe_upload_path(value: str, project_root: Path | None = None) -> Path:
    root = upload_root(project_root)
    raw = str(value or '').strip().lstrip('/')
    prefix = 'static/uploads/images/'
    if not raw.startswith(prefix) or raw.startswith('//'):
        raise ValueError('附件路径必须位于应用上传目录')
    filename = raw[len(prefix):]
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,180}', filename) or filename in {'.', '..'}:
        raise ValueError('附件路径格式无效')
    unresolved = root / filename
    if unresolved.is_symlink():
        raise ValueError('不允许通过符号链接访问附件')
    candidate = unresolved.resolve(strict=True)
    if candidate.parent != root or not candidate.is_file():
        raise ValueError('附件不存在或路径无效')
    return candidate
