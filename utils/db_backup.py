"""数据库自动备份。

项目原先只有手动导出接口，SQLite 是单文件存储，一旦损坏或误删就全丢。
这里定时用 SQLite 的在线备份 API 生成快照，并按份数滚动清理旧文件。

备份文件放在 ``data/`` 下，命名与手动备份保持一致（``xianyu_data_backup_*.db``），
这样已有的「备份文件列表」和下载接口能直接识别。
"""

import glob
import os
from pathlib import Path
from datetime import datetime
import time
from typing import Any, Dict, List

from loguru import logger


DB_PATH = os.getenv('DB_PATH', os.path.join('data', 'xianyu_data.db'))
BACKUP_DIR = str(Path(DB_PATH).resolve().parent)
BACKUP_PREFIX = "xianyu_data_backup_"


def create_backup(keep: int = 7) -> Dict[str, Any]:
    """生成一份数据库备份并清理超出保留份数的旧备份。

    用 ``sqlite3.Connection.backup()`` 而不是直接复制文件 —— 直接复制在写入
    过程中可能拿到损坏的快照。

    Args:
        keep: 保留最近多少份备份，超出的按时间从旧到新删除。

    Returns:
        ``{"success", "path", "size", "removed", "message"}``
    """
    if not os.path.exists(DB_PATH):
        return {
            "success": False,
            "path": "",
            "size": 0,
            "removed": [],
            "message": f"数据库文件不存在: {DB_PATH}",
        }

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = os.path.join(BACKUP_DIR, f"{BACKUP_PREFIX}{stamp}.db")

    try:
        from app.services.database_snapshot import snapshot
        snapshot(DB_PATH, target)
    except Exception as e:
        logger.error(f"数据库备份失败: {e}")
        # 失败时清掉可能产生的半成品
        if os.path.exists(target):
            try:
                os.remove(target)
            except OSError:
                pass
        return {
            "success": False,
            "path": "",
            "size": 0,
            "removed": [],
            "message": f"备份失败: {e}",
        }

    size = os.path.getsize(target) if os.path.exists(target) else 0
    removed = _cleanup_old_backups(keep)

    logger.info(
        f"数据库备份完成: {target} ({round(size / 1024 / 1024, 2)} MB)"
        + (f"，清理 {len(removed)} 份旧备份" if removed else "")
    )
    return {
        "success": True,
        "path": target,
        "size": size,
        "removed": removed,
        "message": f"备份完成: {os.path.basename(target)}",
    }


def _cleanup_old_backups(keep: int) -> List[str]:
    """只保留最近 ``keep`` 份备份，返回被删除的文件名。"""
    if keep <= 0:
        return []

    pattern = os.path.join(BACKUP_DIR, f"{BACKUP_PREFIX}*.db")
    files = sorted(glob.glob(pattern), key=lambda p: os.path.getmtime(p), reverse=True)

    removed = []
    for path in files[keep:]:
        try:
            os.remove(path)
            removed.append(os.path.basename(path))
        except OSError as e:
            logger.warning(f"删除旧备份失败 {path}: {e}")
    return removed


def list_backups() -> List[Dict[str, Any]]:
    """列出全部备份，按时间从新到旧。"""
    pattern = os.path.join(BACKUP_DIR, f"{BACKUP_PREFIX}*.db")
    items = []
    for path in glob.glob(pattern):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        items.append({
            "filename": os.path.basename(path),
            "size": stat.st_size,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "modified_time": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)
            ),
        })
    items.sort(key=lambda x: x["modified_time"], reverse=True)
    return items
