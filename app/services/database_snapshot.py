"""一致性快照和经校验的停服恢复。"""
import os
from pathlib import Path
import sqlite3
import tempfile


def snapshot(source_path, output_path):
    source_path=Path(source_path).resolve();output_path=Path(output_path).resolve()
    if source_path==output_path:raise ValueError('快照不能覆盖源数据库')
    output_path.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(source_path.as_uri()+'?mode=ro',uri=True,timeout=30) as src:
        with sqlite3.connect(output_path) as dst:
            src.backup(dst,pages=256,sleep=.05)
            if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('数据库完整性检查失败')
    os.chmod(output_path,0o600)
    return output_path


def validate_backup(path):
    path=Path(path).resolve()
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as conn:
        if conn.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('数据库完整性检查失败')
        tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'users','cookies','system_settings','orders','cards'}<=tables:raise ValueError('备份缺少必要业务表')
        for table,required in {'users':{'id','username','password_hash'},'cookies':{'id','value','user_id'}}.items():
            columns={r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
            if not required<=columns:raise ValueError('备份表结构不兼容')
    return True
