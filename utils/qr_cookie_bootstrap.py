"""Initialize MTOP signing cookies after explicit QR login or manual recovery.

This is one public, read-only profile request, never an unsigned login-token
request. A challenge or expired session stops the attempt without retrying.
"""
import json
import re
import time

from utils.user_agents import CHROME_UA
from utils.xianyu_utils import generate_sign, trans_cookies


def has_signing_cookies(cookies):
    return bool(cookies.get('_m_h5_tk', '').split('_')[0] and cookies.get('_m_h5_tk_enc'))


async def initialize_signing_cookies(context, cookies_str, outcome=None, diagnostic=None):
    """Return merged cookies in memory, leaving persistence and CAS to the caller."""
    outcome = outcome if outcome is not None else {}
    original = trans_cookies(cookies_str)

    def fail(category, message):
        outcome.update(failure_category=category, message=message)
        if diagnostic:
            diagnostic.emit('error', 'signing_bootstrap', message)
        return None

    if has_signing_cookies(original):
        return cookies_str
    if not original.get('unb') or not original.get('cookie2'):
        return fail('signing_token_missing', '扫码凭证不完整，签名初始化未执行；请查看账号诊断')
    if diagnostic:
        diagnostic.stage('signing_bootstrap')
        diagnostic.emit('info', 'signing_bootstrap', '使用已保存的扫码凭证初始化签名，仅请求一次公开资料，不请求登录令牌')
    api = 'mtop.idle.web.user.page.head'
    data = json.dumps({'userId': original['unb']}, separators=(',', ':'))
    timestamp = str(int(time.time() * 1000))
    token = original.get('_m_h5_tk', '').split('_')[0]
    params = {'jsv': '2.7.2', 'appKey': '34839810', 't': timestamp,
              'sign': generate_sign(timestamp, token, data), 'v': '1.0',
              'type': 'originaljson', 'accountSite': 'xianyu', 'dataType': 'json',
              'timeout': '20000', 'api': api, 'sessionOption': 'AutoLoginOnly', 'valueType': 'string'}
    started = time.monotonic()
    try:
        response = await context.request.post(
            f'https://h5api.m.goofish.com/h5/{api}/1.0/', params=params,
            form={'data': data}, headers={'accept': 'application/json',
                'content-type': 'application/x-www-form-urlencoded', 'user-agent': CHROME_UA,
                'origin': 'https://seller.goofish.com', 'referer': 'https://seller.goofish.com/'},
            timeout=20000)
        try:
            status = response.status
            payload = await response.json()
        finally:
            await response.dispose()
        fresh = {c['name']: c['value'] for c in await context.cookies('https://h5api.m.goofish.com/')}
        if diagnostic:
            from app.services.captcha_diagnostics import secret_values
            diagnostic.secrets = tuple(set(diagnostic.secrets) | set(secret_values(
                '; '.join(f'{k}={v}' for k, v in fresh.items()))))
        if fresh.get('unb') and fresh['unb'] != original['unb']:
            return fail('interrupted', '签名初始化的账号标识不一致，本轮已停止')
        merged = {**original, **fresh}
        ret = payload.get('ret') if isinstance(payload, dict) else None
        codes = [str(v).split('::', 1)[0] for v in ret[:8]] if isinstance(ret, list) else []
        codes = [v if re.fullmatch(r'[A-Z][A-Z0-9_]{1,80}', v) else 'INVALID_RESPONSE' for v in codes]
        if diagnostic:
            diagnostic.emit('info', 'signing_bootstrap_response',
                f'HTTP {status} {(time.monotonic()-started)*1000:.0f}ms；业务码：{",".join(codes)}；签名完整：{has_signing_cookies(merged)}')
        if status != 200:
            return fail('challenge_request_failed', '签名初始化请求未成功，本轮已停止；请查看HTTP状态')
        if 'FAIL_SYS_SESSION_EXPIRED' in codes:
            return fail('login_session_expired', '平台明确返回登录会话已失效，请重新登录同一账号')
        response_data = payload.get('data') if isinstance(payload, dict) else None
        challenge_url = response_data.get('url') if isinstance(response_data, dict) else None
        if any('USER_VALIDATE' in code or 'CAPTCHA' in code or code.startswith('RGV') for code in codes) or challenge_url:
            return fail('login_verification_required', '平台在登录凭证初始化时要求本人验证，本轮已停止；请查看账号诊断')
        allowed = {'SUCCESS', 'FAIL_SYS_TOKEN_EMPTY', 'FAIL_SYS_TOKEN_EXPIRED', 'FAIL_SYS_TOKEN_EXOIRED'}
        if not codes or not set(codes) <= allowed:
            return fail('challenge_invalid_response', '签名初始化响应无法确认，本轮已停止；请查看业务码')
        if not has_signing_cookies(merged):
            return fail('signing_token_missing', '已取得扫码凭证，但平台尚未补全签名；请查看浏览器日志，不必重复扫码')
        return '; '.join(f'{k}={v}' for k, v in merged.items())
    except Exception as exc:
        if diagnostic:
            diagnostic.emit('error', 'signing_bootstrap_request', type(exc).__name__)
        return fail('challenge_request_failed', '签名初始化发生网络或响应解析异常，本轮已停止；请查看浏览器日志')
