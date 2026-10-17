"""单进程运行约束与数据库维护窗口。"""
import atexit
import os
from pathlib import Path

_locks = {}


def acquire_runtime_lock(db_path=None):
    if int(os.getenv('WEB_CONCURRENCY','1')) != 1:
        raise RuntimeError('账号监听服务只支持单进程；请设置 WEB_CONCURRENCY=1')
    path=Path(db_path or os.getenv('DB_PATH','data/xianyu_data.db')).resolve()
    if str(path) in _locks:return
    path.parent.mkdir(parents=True,exist_ok=True)
    handle=open(str(path)+'.runtime.lock','a+')
    try:
        if os.name=='nt':
            import msvcrt
            handle.seek(0);handle.write('1');handle.flush();handle.seek(0)
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError('此数据库已有服务运行；维护或恢复前必须先停止该服务') from None
    handle.seek(0);handle.truncate();handle.write(str(os.getpid()));handle.flush()
    _locks[str(path)]=handle
    atexit.register(handle.close)
