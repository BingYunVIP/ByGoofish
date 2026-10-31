"""项目启动入口：

1. 创建 CookieManager，按配置文件 / 环境变量初始化账号任务
2. 在后台线程启动 FastAPI (reply_server) 提供管理与自动回复接口
3. 主协程保持运行
"""

import os
import sys
import shutil
from pathlib import Path
from app.runtime import acquire_runtime_lock
acquire_runtime_lock()
from app.logging_setup import configure_logging
configure_logging()

# 设置标准输出编码为UTF-8（Windows兼容）
def _setup_console_encoding():
    """设置控制台编码为UTF-8，避免Windows GBK编码问题"""
    if sys.platform == 'win32':
        try:
            # 方法1: 设置环境变量
            os.environ['PYTHONIOENCODING'] = 'utf-8'
            
            # 方法2: 尝试设置控制台代码页为UTF-8
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                kernel32.SetConsoleOutputCP(65001)  # UTF-8代码页
            except Exception:
                pass
            
            # 方法3: 重新包装stdout和stderr
            try:
                if hasattr(sys.stdout, 'buffer'):
                    import io
                    # 只在编码不是UTF-8时重新包装
                    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
                        sys.stdout = io.TextIOWrapper(
                            sys.stdout.buffer, 
                            encoding='utf-8', 
                            errors='replace',
                            line_buffering=True
                        )
                    if sys.stderr.encoding and sys.stderr.encoding.lower() not in ('utf-8', 'utf8'):
                        sys.stderr = io.TextIOWrapper(
                            sys.stderr.buffer, 
                            encoding='utf-8', 
                            errors='replace',
                            line_buffering=True
                        )
            except Exception:
                pass
        except Exception:
            pass

# 在程序启动时设置编码
_setup_console_encoding()

# 定义ASCII安全字符（备用方案）
_OK = '[OK]'
_WARN = '[WARN]'
_ERROR = '[ERROR]'
_INFO = '[INFO]'

# ==================== 在导入任何模块之前先迁移数据库 ====================
def _migrate_database_files_early():
    """在启动前检查并迁移数据库文件到data目录（使用print，因为logger还未初始化）"""
    print("检查数据库文件位置...")
    
    # 确保data目录存在
    data_dir = Path("data")
    if not data_dir.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        print(f"{_OK} 创建 data 目录")
    
    # 定义需要迁移的文件
    files_to_migrate = [
        ("xianyu_data.db", "data/xianyu_data.db", "主数据库"),
    ]
    
    migrated_files = []
    
    # 迁移主数据库和统计数据库
    for old_path, new_path, description in files_to_migrate:
        old_file = Path(old_path)
        new_file = Path(new_path)
        
        if old_file.exists():
            if not new_file.exists():
                # 新位置不存在，移动文件
                try:
                    shutil.move(str(old_file), str(new_file))
                    print(f"{_OK} 迁移{description}: {old_path} -> {new_path}")
                    migrated_files.append(description)
                except Exception as e:
                    print(f"{_WARN} 无法迁移{description}: {e}")
                    print(f"  尝试复制文件...")
                    try:
                        shutil.copy2(str(old_file), str(new_file))
                        print(f"{_OK} 已复制{description}到新位置")
                        print(f"  请在确认数据正常后手动删除: {old_path}")
                        migrated_files.append(f"{description}(已复制)")
                    except Exception as e2:
                        print(f"{_ERROR} 复制{description}失败: {e2}")
            else:
                # 新位置已存在，检查旧文件大小
                try:
                    if old_file.stat().st_size > 0:
                        print(f"{_WARN} 发现旧{description}文件: {old_path}")
                        print(f"  新数据库位于: {new_path}")
                        print(f"  建议备份后删除旧文件")
                except:
                    pass
    
    # 迁移备份文件
    backup_files = list(Path(".").glob("xianyu_data_backup_*.db"))
    if backup_files:
        print(f"发现 {len(backup_files)} 个备份文件")
        backup_migrated = 0
        for backup_file in backup_files:
            new_backup_path = data_dir / backup_file.name
            if not new_backup_path.exists():
                try:
                    shutil.move(str(backup_file), str(new_backup_path))
                    print(f"{_OK} 迁移备份文件: {backup_file.name}")
                    backup_migrated += 1
                except Exception as e:
                    print(f"{_WARN} 无法迁移备份文件 {backup_file.name}: {e}")
        
        if backup_migrated > 0:
            migrated_files.append(f"{backup_migrated}个备份文件")
    
    # 输出迁移总结
    if migrated_files:
        print(f"{_OK} 数据库迁移完成，已迁移: {', '.join(migrated_files)}")
    else:
        print(f"{_OK} 数据库文件检查完成")
    
    return True

# 在导入 db_manager 之前先执行数据库迁移
try:
    _migrate_database_files_early()
except Exception as e:
    print(f"{_WARN} 数据库迁移检查失败: {e}")
    # 继续启动，因为可能是首次运行

# ==================== Playwright 离线环境自检 ====================
# 依赖安装交给部署阶段：服务启动时不下载、不换镜像、不修改共享缓存。
# 用本解释器要求的实际浏览器做自检，不能把其他 Node 项目的旧版本误认成可用。
try:
    if getattr(sys, 'frozen', False):
        bundled_browsers = Path(sys.executable).parent / 'playwright'
        if bundled_browsers.is_dir():
            os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(bundled_browsers))
    from utils.playwright_runtime import check_runtime, print_report
    browser_report = check_runtime()
    print_report(browser_report)
    if browser_report['errors']:
        print(f"{_WARN} 浏览器相关功能尚未就绪；二维码生成本身使用 HTTP API。")
except Exception as e:
    print(f"{_WARN} 浏览器自检异常: {type(e).__name__}；请运行 scripts/playwright_doctor.py")

# ==================== 验证已构建前端 ====================
def _verify_frontend():
    """启动只验证发布产物；构建和验收必须在部署前完成。"""
    import re
    static_dir = Path(os.getenv('APP_STATIC_DIR', 'static')).resolve()
    index = static_dir / 'index.html'
    if not index.is_file():
        raise RuntimeError('缺少 static/index.html；请先在 frontend 执行 npm ci、npm run typecheck、npm run build')
    refs = re.findall(r'(?:src|href)=[\"\']([^\"\']+)[\"\']', index.read_text(encoding='utf-8'))
    assets = [ref for ref in refs if '/assets/' in ref and not ref.startswith(('http:', 'https:', '//'))]
    if not assets:
        raise RuntimeError('前端入口未引用构建资源，请重新构建并验收')
    for ref in assets:
        relative = ref.split('?', 1)[0].lstrip('/')
        if relative.startswith('static/'):
            relative = relative[len('static/'):]
        target = (static_dir / relative).resolve()
        if not target.is_relative_to(static_dir) or not target.is_file():
            raise RuntimeError('前端构建资源不完整，请重新构建并验收')
    print(f"{_OK} 已构建前端资源检查通过")


_verify_frontend()

# ==================== 现在可以安全地导入其他模块 ====================
import asyncio
import threading
from urllib.parse import urlparse
from loguru import logger

# 修复Linux环境下的asyncio子进程问题
if sys.platform.startswith('linux'):
    try:
        # 在程序启动时就设置正确的事件循环策略
        asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
        logger.debug("已设置事件循环策略以支持子进程")
    except Exception as e:
        logger.debug(f"设置事件循环策略失败: {e}")

from app.config import AUTO_REPLY, COOKIES_LIST
from app import cookie_manager as cm
from app.file_log_collector import setup_file_logging


def _start_api_server():
    """后台线程启动 FastAPI 服务"""
    loop = None
    api_conf = AUTO_REPLY.get('api', {})

    # 优先使用环境变量配置
    host = os.getenv('API_HOST', '127.0.0.1')  # 默认仅本机访问，由HTTPS反代对外提供服务
    port = int(os.getenv('API_PORT', '8080'))  # 默认端口8080

    # 如果配置文件中有特定配置，则使用配置文件
    if 'host' in api_conf and 'API_HOST' not in os.environ:
        host = api_conf['host']
    if 'port' in api_conf and 'API_PORT' not in os.environ:
        port = api_conf['port']

    # 兼容旧的URL配置方式
    if (
        'url' in api_conf
        and 'host' not in api_conf
        and 'port' not in api_conf
        and 'API_HOST' not in os.environ
        and 'API_PORT' not in os.environ
    ):
        url = api_conf.get('url', 'http://0.0.0.0:8080/xianyu/reply')
        parsed = urlparse(url)
        if parsed.hostname and parsed.hostname != 'localhost':
            host = parsed.hostname
        port = parsed.port or 8080

    logger.info(f"启动Web服务器: http://{host}:{port}")
    # 在后台线程中创建独立事件循环并直接运行 server.serve()
    import uvicorn
    try:
        config = uvicorn.Config("app.reply_server:app", host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())
    except BaseException as e:
        logger.error(f"uvicorn服务器退出: {type(e).__name__}: {e}")
    finally:
        if loop is not None:
            if loop.is_running():
                loop.stop()
            loop.close()
        asyncio.set_event_loop(None)




def load_keywords_file(path: str):
    """从文件读取关键字 -> [(keyword, reply)]"""
    kw_list = []
    p = Path(path)
    if not p.exists():
        return kw_list
    with p.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '\t' in line:
                k, r = line.split('\t', 1)
            elif ' ' in line:
                k, r = line.split(' ', 1)
            elif ':' in line:
                k, r = line.split(':', 1)
            else:
                continue
            kw_list.append((k.strip(), r.strip()))
    return kw_list


async def main():
    print("开始启动主程序...")

    # 初始化文件日志收集器
    print("初始化文件日志收集器...")
    setup_file_logging()
    logger.info("文件日志收集器已启动，开始收集实时日志")

    loop = asyncio.get_running_loop()

    # 创建 CookieManager 并在全局暴露
    print("创建 CookieManager...")
    cm.manager = cm.CookieManager(loop)
    manager = cm.manager
    print("CookieManager 创建完成")

    # 1) 从数据库加载的 Cookie 已经在 CookieManager 初始化时完成
    # 为每个启用的 Cookie 启动任务
    for cid, val in manager.cookies.items():
        # 检查账号是否启用
        if not manager.get_cookie_status(cid):
            logger.info(f"跳过禁用的 Cookie: {cid}")
            continue

        try:
            await manager._ensure_cookie_task_async(cid, val)
        except Exception as e:
            logger.error(f"启动 Cookie 任务失败: {cid}, {e}")
            import traceback
            logger.error(f"详细错误信息: {traceback.format_exc()}")
    
    # 2) 如果配置文件中有新的 Cookie，也加载它们
    for entry in COOKIES_LIST:
        cid = entry.get('id')
        val = entry.get('value')
        if not cid or not val or cid in manager.cookies:
            continue
        
        kw_file = entry.get('keywords_file')
        kw_list = load_keywords_file(kw_file) if kw_file else None
        manager.add_cookie(cid, val, kw_list)
        logger.info(f"从配置文件加载 Cookie: {cid}")

    # 3) 若老环境变量仍提供单账号 Cookie，则作为 default 账号
    env_cookie = os.getenv('COOKIES_STR')
    if env_cookie and 'default' not in manager.list_cookies():
        manager.add_cookie('default', env_cookie)
        logger.info("从环境变量加载 default Cookie")

    # 启动 API 服务线程
    print("启动 API 服务线程...")
    api_thread = threading.Thread(target=_start_api_server, daemon=True)
    api_thread.start()
    print("API 服务线程已启动")

    # 启动数据库自动备份：全局一份，不随账号数量重复
    print("启动数据库自动备份任务...")
    asyncio.create_task(_db_backup_loop())

    # 阻塞保持运行
    print("主程序启动完成，保持运行...")
    while api_thread.is_alive():
        await asyncio.sleep(1)
    raise RuntimeError("API 服务线程已退出，停止账号任务并交由服务管理器处理")


async def _db_backup_loop():
    """数据库定时备份。

    SQLite 是单文件存储，损坏或误删就全部丢失，因此默认开启。
    间隔和保留份数可在系统设置里调整。
    """
    from app.db_manager import db_manager
    from utils.db_backup import create_backup

    while True:
        try:
            enabled_str = db_manager.get_system_setting('db_backup_enabled')
            interval_str = db_manager.get_system_setting('db_backup_interval')
            keep_str = db_manager.get_system_setting('db_backup_keep')

            # 默认开启：备份没有副作用，出问题时能救命
            enabled = str(enabled_str or '').strip().lower() != 'false'
            try:
                interval = int(interval_str) if interval_str else 86400
            except (TypeError, ValueError):
                interval = 86400
            interval = max(3600, interval)
            try:
                keep = int(keep_str) if keep_str else 7
            except (TypeError, ValueError):
                keep = 7

            if not enabled:
                await asyncio.sleep(600)
                continue

            result = create_backup(keep=max(1, keep))
            if not result['success']:
                logger.warning(f"数据库自动备份失败: {result['message']}")

            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("数据库自动备份任务已取消")
            raise
        except Exception as e:
            logger.error(f"数据库自动备份异常: {e}")
            await asyncio.sleep(600)


if __name__ == '__main__':
    asyncio.run(main())
