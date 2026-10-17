#!/usr/bin/env python3
"""停止服务后运行的一致性数据库恢复工具。"""
import argparse
from datetime import datetime
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.runtime import acquire_runtime_lock
from app.services.database_snapshot import snapshot,validate_backup


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file',type=Path,required=True)
    parser.add_argument('--database',type=Path,default=ROOT/'data/xianyu_data.db')
    args=parser.parse_args();source=args.file.resolve();target=args.database.resolve()
    if source==target:parser.error('备份和运行数据库不能相同')
    acquire_runtime_lock(target)
    validate_backup(source)
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
    before=target.parent/f'before_restore_{stamp}.db'
    if target.exists():snapshot(target,before)
    staged=target.parent/f'.restore_{stamp}.db'
    try:
        snapshot(source,staged)
        with sqlite3.connect(staged) as conn:
            # 恢复数据不复活旧管理会话或旧的集成密钥。
            tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('auth_sessions','account_api_keys'):
                if table in tables:conn.execute(f'DELETE FROM {table}')
            # 快照之后发生的交易不能从备份推断；恢复后先暂停账号，供本人核对。
            conn.execute('''INSERT INTO cookie_status(cookie_id,enabled)
                SELECT id,0 FROM cookies WHERE 1
                ON CONFLICT(cookie_id) DO UPDATE SET enabled=0''')
        validate_backup(staged)
        for suffix in ('-wal','-shm'):
            # 只有已获得维护锁且旧连接关闭时才清理旧数据库的旁文件。
            side=Path(str(target)+suffix)
            if side.exists():side.unlink()
        os.replace(staged,target)
        print(f'恢复完成；修改前快照：{before}。账号已暂停，请启动服务、重新登录并核对交易后再启用。')
    finally:
        if staged.exists():staged.unlink()


if __name__=='__main__':main()
