"""人工完成滑块验证。

收到平台要求本人验证后，只在用户明确开启会话时获取一次新页面。
通过现有远程控制通道推送截图并转发操作；报错时停止本轮，不自动刷新。
页面完成仅代表可以继续检查令牌和消息连接，不能直接宣告账号恢复。

与 ``utils.xianyu_slider_stealth`` 的区别是那边用同步 Playwright，无法和
异步的远程通道共享 page 对象，所以这里独立起一个异步浏览器。
"""

import asyncio
import base64
import time
import os
from typing import Any, Dict, Optional

from loguru import logger
from utils import browser_limit
from utils.user_agents import CHROME_UA
from utils.captcha_page_state import inspect_page, ChallengePageError

LOGIN_URL = "https://www.goofish.com/"

# 人工操作需要时间，但也不能无限占用浏览器
DEFAULT_TIMEOUT = 300

# 等待滑块真正出现的最长时间（秒）。惩罚页加载本身就要几秒，
# 等不到只说明控件尚未就绪，具体原因由脱敏浏览器诊断记录。
CAPTCHA_PRESENT_TIMEOUT = 20


def get_verification_url(cookie_id: str) -> Optional[str]:
    """从风控日志里取该账号最近一次滑块惩罚的 URL。

    惩罚 URL（``...punish?x5secdata=...&x5step=2&action=captcha``）是 Token
    刷新响应里 ``data.url`` 返回的，写进了 ``risk_control_logs.event_description``。
    只有导航到这个 URL 才会真正弹出滑块 —— 导航首页（原实现的 LOGIN_URL）不会。

    x5secdata 有有效期（约 1 小时），过期后导航会跳到空白页。所以这里只是
    兜底：会话内若触发实时 Token 刷新拿到新 URL 更好，拿不到才用它。
    """
    import re
    from app.db_manager import db_manager

    try:
        with db_manager.lock:
            cur = db_manager.conn.cursor()
            cur.execute(
                "SELECT event_description FROM risk_control_logs "
                "WHERE cookie_id=? AND event_type IN ('message_verification_required','login_verification_required') "
                "ORDER BY id DESC LIMIT 1",
                (cookie_id,),
            )
            row = cur.fetchone()
    except Exception as exc:
        logger.debug(f"【{cookie_id}】读取惩罚 URL 失败: {exc}")
        return None

    if not row or not row[0]:
        return None
    m = re.search(r"URL: (\S+)", row[0])
    url = m.group(1) if m else None
    if url and ("punish" in url or "action=captcha" in url):
        logger.info(f"【{cookie_id}】从风控日志取到惩罚 URL")
        return url
    return None


def _challenge_response(payload):
    """只提取有限业务码，不把响应正文、Cookie或令牌写入诊断。"""
    import re
    if not isinstance(payload, dict) or not isinstance(payload.get('ret'), list):
        return ['INVALID_RESPONSE'], {}
    codes = []
    for value in payload['ret'][:8]:
        code = str(value).split('::', 1)[0]
        codes.append(code if re.fullmatch(r'[A-Z][A-Z0-9_]{1,80}', code) else 'UNRECOGNIZED_CODE')
    data = payload.get('data')
    return codes or ['INVALID_RESPONSE'], data if isinstance(data, dict) else {}


def _challenge_failure(outcome, category, message):
    if outcome is not None:
        outcome.update(failure_category=category, message=message)


async def _fetch_live_verification_url(cookie_id: str, cookies_str: str, context=None, outcome=None, diagnostic=None, token_sink=None) -> Optional[str]:
    """显式人工恢复的一次挑战获取；签名确实轮换时仅允许一次重新签名。"""
    import json
    import aiohttp
    from app.config import API_ENDPOINTS
    from utils.xianyu_utils import trans_cookies, generate_sign, token_request_data, token_request_headers

    cd = trans_cookies(cookies_str)
    from utils.qr_cookie_bootstrap import initialize_signing_cookies
    if not cd.get('_m_h5_tk', '').split('_')[0] and context is not None:
        initialized = await initialize_signing_cookies(context, cookies_str, outcome=outcome, diagnostic=diagnostic)
        if not initialized:
            return None
        cd = trans_cookies(initialized)
    if not cd.get('_m_h5_tk', '').split('_')[0]:
        _challenge_failure(outcome, 'signing_token_missing', '扫码凭证的签名初始化未完成，请使用人工验证继续检查；不必重复扫码')
        if diagnostic:
            diagnostic.emit('error', 'challenge_result', '缺少签名字段，未请求平台')
        return None
    # 补签和后续正式Token/WS注册共用同一个已保存的账号设备标识。
    from app.db_manager import db_manager
    from app.services.account_auth_client import device_id as account_device_id
    client_device_id = account_device_id(db_manager, cookie_id, cd.get('unb', ''))
    if diagnostic:
        diagnostic.emit('info', 'client_identity', '使用同一账号客户端标识进行挑战与补签（值不记录）')
    # 浏览器调用共享其 Cookie jar；无浏览器的兼容调用也只复用一个直连 Session。
    session = None
    try:
        if context is None:
            session = aiohttp.ClientSession(trust_env=False)
        for attempt in range(2):
            if diagnostic:
                diagnostic.stage('challenge_request' if attempt == 0 else 'challenge_resign')
            started = time.monotonic()
            old_signing = cd['_m_h5_tk']
            ts = str(int(time.time() * 1000))
            params = {
                'jsv': '2.7.2', 'appKey': '34839810', 't': ts, 'v': '1.0',
                'type': 'originaljson', 'accountSite': 'xianyu', 'dataType': 'json',
                'timeout': '20000', 'api': 'mtop.taobao.idlemessage.pc.login.token',
                'sessionOption': 'AutoLoginOnly',
                'dangerouslySetWindvaneParams': '%5Bobject%20Object%5D',
                'smToken': 'token', 'queryToken': 'sm', 'sm': 'sm',
                'spm_cnt': 'a21ybx.im.0.0', 'spm_pre': 'a21ybx.home.sidebar.1.4c053da6vYwnmf',
                'log_id': '4c053da6vYwnmf',
            }
            data_val = token_request_data(client_device_id)
            params['sign'] = generate_sign(ts, old_signing.split('_')[0], data_val)
            headers = token_request_headers()
            if context is not None:
                response = await context.request.post(API_ENDPOINTS.get('token'), params=params,
                                                      form={'data': data_val}, headers=headers, timeout=30000)
                try:
                    status = response.status
                    payload = await response.json()
                finally:
                    await response.dispose()
                fresh = {entry['name']: entry['value'] for entry in await context.cookies()}
                cd.update(fresh)
            else:
                headers['cookie'] = '; '.join(f'{key}={value}' for key, value in cd.items())
                async with session.post(API_ENDPOINTS.get('token'), params=params, data={'data': data_val},
                                        headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                    status = response.status
                    payload = await response.json(content_type=None)
                    cd.update({key: value.value for key, value in response.cookies.items()})
            # 新下发的凭证也加入内存脱敏词表，不写入额外诊断字段。
            if diagnostic:
                from app.services.captcha_diagnostics import secret_values
                diagnostic.secrets = tuple(set(diagnostic.secrets) | set(secret_values('; '.join(f'{k}={v}' for k,v in cd.items()))))
                diagnostic.emit('info', 'challenge_response', f'HTTP {status} {(time.monotonic()-started)*1000:.0f}ms；第{attempt+1}次签名请求')
            codes, data = _challenge_response(payload)
            if diagnostic:
                diagnostic.emit('info', 'challenge_result', '业务码：' + ','.join(codes) +
                                f"；有验证地址：{bool(data.get('url'))}；有访问令牌：{bool(data.get('accessToken'))}")
            if status != 200:
                _challenge_failure(outcome, 'challenge_request_failed', '平台验证请求未成功，本轮已停止，请查看浏览器日志中的HTTP状态')
                return None
            if 'SUCCESS' in codes and isinstance(data.get('accessToken'), str) and data['accessToken']:
                if token_sink is not None:
                    from app.services.account_auth_client import VerifiedToken
                    token_sink(VerifiedToken(cookie_id, client_device_id, data['accessToken'], time.time()))
                if outcome is not None:
                    outcome['already_authenticated'] = True
                return None
            # 登录会话失效不等同于可轮换的签名令牌过期，绝不能反复请求。
            if 'FAIL_SYS_SESSION_EXPIRED' in codes:
                _challenge_failure(outcome, 'login_session_expired', '闲鱼明确返回登录会话已失效，请使用扫码添加账号重新登录同一账号，无需手工复制Cookie')
                return None
            url = data.get('url')
            if isinstance(url, str) and ('punish' in url or 'action=captcha' in url):
                return url
            if any('USER_VALIDATE' in code or 'CAPTCHA' in code for code in codes):
                _challenge_failure(outcome, 'challenge_unavailable', '平台要求本人验证但未返回可用地址，本轮已停止，请查看浏览器日志')
                return None
            signing_expired = bool({'FAIL_SYS_TOKEN_EXOIRED', 'FAIL_SYS_TOKEN_EXPIRED'} & set(codes))
            rotated = bool(cd.get('_m_h5_tk', '').split('_')[0] and cd['_m_h5_tk'] != old_signing)
            if signing_expired and rotated and attempt == 0:
                if diagnostic:
                    diagnostic.emit('info', 'signing_cookie_rotated', '平台已轮换签名凭证，仅用新签名补发一次；不尝试验证码')
                continue
            if signing_expired:
                _challenge_failure(outcome, 'signing_token_expired', '签名令牌刷新未完成，本轮已停止，请查看浏览器日志；这不代表登录会话已过期')
            elif codes == ['INVALID_RESPONSE']:
                _challenge_failure(outcome, 'challenge_invalid_response', '平台返回格式无法识别，本轮已停止，请查看浏览器日志')
            else:
                _challenge_failure(outcome, 'challenge_unavailable', '平台未返回可用验证地址，本轮已停止；请查看浏览器日志中的业务码，不必先手工更新Cookie')
            return None
    except Exception as exc:
        if diagnostic:
            diagnostic.emit('error', 'challenge_request', type(exc).__name__)
        _challenge_failure(outcome, 'challenge_request_failed', '获取验证地址时发生网络或响应解析异常，本轮已停止，请查看浏览器日志')
        logger.debug(f"【{cookie_id}】获取验证地址失败（{type(exc).__name__}）")
        return None
    finally:
        if session is not None:
            await session.close()


async def open_manual_session(
    cookie_id: str,
    cookies_str: str,
    timeout: int = DEFAULT_TIMEOUT,
    headless: Optional[bool] = None,
    diagnostic=None,
    check_only=False,
) -> Dict[str, Any]:
    """打开一个人工验证会话，等待人在浏览器里完成滑块。

    Args:
        timeout: 等待人工操作的秒数，超时后放弃并关闭浏览器。
        headless: 默认无头 —— 页面通过远程通道推给用户，不需要本地窗口。

    Returns:
        ``{"success": bool, "cookies_str": str, "message": str, "session_id": str}``。
        成功时 ``cookies_str`` 是完成验证后的新 Cookie，调用方应保存。
    """
    from playwright.async_api import async_playwright

    from utils.captcha_remote_control import captcha_controller

    session_id = str(cookie_id)
    result: Dict[str, Any] = {
        "success": False,
        "cookies_str": cookies_str,
        "message": "",
        "session_id": session_id,
    }

    playwright = None
    browser = None
    context = None
    if headless is None:
        # 本机 systemd 通过 xvfb-run 提供真实显示器。挑战页对无头浏览器
        # 更容易返回 Oops 错误；人工验证优先使用有显示器的完整 Chromium。
        headless = not bool(os.getenv('DISPLAY'))
    refresh_task = None
    page = None
    started = time.monotonic()
    try:
        if diagnostic:
            diagnostic.stage('browser_start')
        playwright = await async_playwright().start()
        browser = await browser_limit.launch_browser(
            playwright,
            {'headless': headless,
             # 只用完整版 Chromium。headless=True 默认会去找单独下载的
             # chromium_headless_shell，缺失时报 Executable doesn't exist，
             # 人工验证页面就会卡在「正在服务器上打开验证页面」。
             'channel': 'chromium',
             'args': [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ]},
            "人工验证码",
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=CHROME_UA,
        )

        if diagnostic:
            diagnostic.attach(context)
            diagnostic.emit('info', 'browser_ready', f'浏览器就绪 {(time.monotonic()-started)*1000:.0f}ms；模式={"headless" if headless else "display"}')
        if cookies_str:
            await context.add_cookies(_to_playwright_cookies(cookies_str))

        page = await context.new_page()

        # 惩罚页 URL 优先：只有导航到它才会弹出滑块。
        # 原实现导航到闲鱼首页，首页没有滑块 → check_completion 立刻误判
        # “已完成” → 浏览器秒关，用户连上控制页时会话已不存在。
        #
        # 用户明确开启人工验证后只请求一次，不用历史挑战地址兜底。
        outcome = {}
        verification_url = await _fetch_live_verification_url(cookie_id, cookies_str, context=context, outcome=outcome, diagnostic=diagnostic,
            token_sink=lambda verified: result.__setitem__('verified_auth', verified))
        if outcome.get('already_authenticated'):
            result.update(success=True, cookies_str=_from_playwright_cookies(await context.cookies()),
                          message='当前登录凭证有效，继续检查消息连接')
            return result
        if not verification_url:
            result['message'] = outcome.get('message', '平台未返回可用验证地址，本轮已停止，请查看浏览器日志')
            result['failure_category'] = outcome.get('failure_category', 'challenge_unavailable')
            return result

        if check_only:
            result.update(failure_category='official_verification_required',
                          message='服务器消息会话仍要求安全验证；请同步官网已通过消息验证的网页登录态。本次只检查连接，没有刷新验证码页面。')
            return result

        if diagnostic:
            diagnostic.stage('navigation')
        try:
            await page.goto(verification_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            if diagnostic:
                diagnostic.emit('warning','navigation',type(exc).__name__)
            logger.warning(f"【{cookie_id}】导航验证页失败（{type(exc).__name__}）")
        # 给验证组件一点渲染时间，否则截图可能是空白
        await asyncio.sleep(2)

        # 只有真实可见、可用的滑块出现才允许操作；容器或错误文字不算就绪。
        captcha_appeared = await _wait_for_captcha_present(page, timeout=CAPTCHA_PRESENT_TIMEOUT)
        if not captcha_appeared:
            result["message"] = "验证页面未出现可操作控件，请查看浏览器日志后决定是否重新尝试"
            result['failure_category'] = 'challenge_not_ready'
            if diagnostic:
                diagnostic.fail('challenge_not_ready', result['message'])
            logger.warning(f"【{cookie_id}】{result['message']}")
            return result

        if diagnostic:
            diagnostic.stage('interactive')
        await captcha_controller.create_session(session_id, page)
        # 持续推送截图，让用户能看到自己的拖动效果
        refresh_task = asyncio.create_task(
            captcha_controller.auto_refresh_screenshot(session_id, interval=1.0)
        )

        logger.warning(
            f"【{cookie_id}】已开启人工验证会话，请在 {timeout} 秒内于"
            f" /static/captcha_control.html?session={session_id} 完成验证"
        )

        # 关键：会话期间浏览器必须保持存活，等用户拖完才返回。
        # 原实现在这里死等 timeout，且 check_completion 在无滑块时误判完成
        # 直接秒关 —— 改为“滑块出现后，持续等待它消失（完成）或超时”。
        deadline = time.monotonic() + timeout
        completed = False
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            try:
                state = await inspect_page(page, was_ready=True)
                if state['state'] == 'page_error':
                    raise ChallengePageError(state['error_code'])
                if await captcha_controller.check_completion(session_id):
                    completed = True
                    break
            except ChallengePageError:
                raise
            except Exception as exc:
                if diagnostic:
                    diagnostic.emit('warning','state_check',type(exc).__name__)

        if not completed:
            result["message"] = f"人工验证超时（{timeout} 秒内未完成）"
            result['failure_category'] = 'verification_timeout'
            logger.warning(f"【{cookie_id}】{result['message']}")
            return result

        new_cookies = _from_playwright_cookies(await context.cookies())
        if not new_cookies:
            result["message"] = "验证已完成但未取到 Cookie"
            return result

        result["success"] = True
        result["cookies_str"] = new_cookies
        result["message"] = "人工验证完成"
        logger.info(f"【{cookie_id}】人工验证完成，已取得新 Cookie")
        return result
    except asyncio.CancelledError:
        # 关闭是结束原因；关闭前已显示的页面错误仍是本轮失败主因。
        if page is not None and diagnostic:
            try:
                state = await asyncio.wait_for(inspect_page(page), timeout=2)
                if state['state'] == 'page_error':
                    diagnostic.fail('challenge_page_error', '验证页面报错', state['error_code'])
            except Exception:
                pass
        raise
    except ChallengePageError as exc:
        result.update(message=str(exc) + '，本轮已停止；请查看浏览器日志',
                      failure_category='challenge_page_error', error_code=exc.code)
        if diagnostic:
            diagnostic.fail('challenge_page_error', result['message'], exc.code)
        if page is not None:
            try:
                # 小型错误容器也应可读；找不到容器才保留完整视口。
                info = await captcha_controller._get_captcha_info(page)
                shot, _ = await asyncio.wait_for(captcha_controller._screenshot_captcha_area(page, info), timeout=3)
                result['last_screenshot'] = base64.b64encode(shot).decode()
            except Exception:
                pass
        return result
    except Exception as exc:
        if diagnostic:
            diagnostic.emit('error','browser_error',type(exc).__name__)
        result["message"] = "人工验证会话异常，请查看风控日志中的错误分类"
        result["failure_category"] = 'queue_timeout' if isinstance(exc, TimeoutError) and '等待浏览器' in str(exc) else 'browser_error'
        logger.error(f"【{cookie_id}】{result['message']}")
        return result
    finally:
        if refresh_task and not refresh_task.done():
            refresh_task.cancel()
            await asyncio.gather(refresh_task, return_exceptions=True)
        try:
            from utils.captcha_remote_control import captcha_controller

            await captcha_controller.close_session(session_id)
        except Exception:
            pass
        if diagnostic:
            try:
                diagnostic.detach()
            except Exception:
                logger.warning('浏览器诊断解绑未完成，继续关闭验证浏览器')
        for closer in (context, browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:
                    pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass
        if diagnostic:
            diagnostic.emit('info','browser_closed', f'浏览器已清理；会话耗时 {(time.monotonic()-started):.1f}s')


async def _wait_for_captcha_present(page, timeout: int = CAPTCHA_PRESENT_TIMEOUT) -> bool:
    """等页面里出现可见的滑块/验证码元素。

    原实现不区分“页面没有滑块”和“滑块验证完成” —— 首页没滑块，于是
    check_completion 误判完成。这里显式等滑块出现，避免把“没弹出滑块”
    当成“已完成”。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = await inspect_page(page)
        if state['state'] == 'page_error':
            raise ChallengePageError(state['error_code'])
        if state['state'] == 'interactive':
            return True
        await asyncio.sleep(0.25)
    return False


def _to_playwright_cookies(cookies_str: str) -> list:
    """把 Cookie 字符串转成 Playwright 需要的结构。"""
    cookies = []
    for part in cookies_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": ".goofish.com",
            "path": "/",
        })
    return cookies


def _from_playwright_cookies(cookies: list) -> str:
    """把 Playwright 的 Cookie 列表拼回字符串。"""
    pairs = []
    for cookie in cookies or []:
        name = cookie.get("name")
        if not name:
            continue
        pairs.append(f"{name}={cookie.get('value', '')}")
    return "; ".join(pairs)
