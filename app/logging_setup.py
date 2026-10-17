"""全进程统一日志配置；模块导入不再替换其他组件的输出。"""
import os
import sys
import threading
from pathlib import Path
from loguru import logger
from utils.log_sanitizer import redact_log_record

_configured=False
_lock=threading.Lock()
LOG_PATH=Path(os.getenv('APP_LOG_PATH','logs/application.log'))


def configure_logging():
    global _configured
    with _lock:
        if _configured:return
        LOG_PATH.parent.mkdir(parents=True,exist_ok=True)
        descriptor=os.open(LOG_PATH,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
        os.close(descriptor)
        logger.remove()
        logger.configure(patcher=redact_log_record)
        options={'format':'{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {name}:{function}:{line} - {message}',
                 'level':os.getenv('LOG_LEVEL','INFO'),'enqueue':True,'backtrace':False,'diagnose':False}
        logger.add(str(LOG_PATH),rotation='10 MB',retention='7 days',encoding='utf-8',**options)
        logger.add(sys.stdout,**options)
        _configured=True
