"""Product prose, provenance and bounded platform acquisition, separate from card metadata."""
import json
import time
import secrets
import re

RETRY_SECONDS = 6 * 60 * 60


class DetailFetchError(Exception):
    def __init__(self, category):
        self.category = category
        super().__init__(category)


def card_metadata(value):
    """Recognize only the old list-card shape; arbitrary user JSON is valid prose."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if not isinstance(value, dict):
        return None
    if ('detail_params' in value and isinstance(value['detail_params'], dict)
            and any(k in value for k in ('track_params', 'item_label_data', 'card_type'))
            and 'title' in value):
        return value
    return None


def prose(value):
    if not isinstance(value, str) or card_metadata(value) is not None:
        return ''
    return value.strip()


def initialize(db):
    with db.lock:
        cols = {r[1] for r in db.conn.execute('PRAGMA table_info(item_info)')}
        for name, definition in {
            'platform_metadata': "TEXT NOT NULL DEFAULT ''",
            'detail_source': "TEXT NOT NULL DEFAULT 'unknown'",
            'detail_status': "TEXT NOT NULL DEFAULT 'missing'",
            'detail_updated_at': 'REAL',
            'detail_retry_at': 'REAL',
            'detail_failure': "TEXT NOT NULL DEFAULT ''",
            'detail_browser_blocked': 'INTEGER NOT NULL DEFAULT 0',
        }.items():
            if name not in cols:
                db.conn.execute(f'ALTER TABLE item_info ADD COLUMN {name} {definition}')
        for rowid, detail, description in db.conn.execute(
                "SELECT id,item_detail,item_description FROM item_info WHERE detail_source='unknown'").fetchall():
            metadata = card_metadata(detail)
            if metadata:
                text = prose(metadata.get('description')) or prose(description)
                db.conn.execute('''UPDATE item_info SET platform_metadata=?,item_detail=?,
                    detail_source=?,detail_status=?,detail_updated_at=? WHERE id=?''',
                    (json.dumps(metadata, ensure_ascii=False), text, 'platform' if text else 'unknown',
                     'ready' if text else 'missing', time.time() if text else None, rowid))
            elif prose(detail):
                # Existing prose has no trustworthy origin: preserve as a user override.
                db.conn.execute("UPDATE item_info SET detail_source='manual',detail_status='ready' WHERE id=?", (rowid,))
        db.conn.execute("UPDATE item_info SET detail_status='failed',detail_failure='interrupted' WHERE detail_status='fetching'")
        db.conn.execute("CREATE TABLE IF NOT EXISTS item_detail_accounts (cookie_id TEXT PRIMARY KEY REFERENCES cookies(id) ON DELETE CASCADE,generation INTEGER NOT NULL DEFAULT 0,pending INTEGER NOT NULL DEFAULT 0,claimed INTEGER NOT NULL DEFAULT -1,blocked INTEGER NOT NULL DEFAULT 0,legacy_claimed INTEGER NOT NULL DEFAULT 0)")
        db.conn.execute("CREATE TABLE IF NOT EXISTS item_detail_diagnostics (id TEXT PRIMARY KEY,cookie_id TEXT NOT NULL REFERENCES cookies(id) ON DELETE CASCADE,item_id TEXT NOT NULL,generation INTEGER NOT NULL,created_at REAL NOT NULL,category TEXT NOT NULL,stage TEXT NOT NULL,http_status INTEGER,codes TEXT NOT NULL,elapsed_ms INTEGER NOT NULL)")
        db.conn.commit()


def write(cursor, cookie_id, item_id, text, *, source, metadata=None, explicit=False):
    """Caller owns transaction. Automated writes never replace a user override, even empty."""
    if metadata is not None:
        cursor.execute('UPDATE item_info SET platform_metadata=? WHERE cookie_id=? AND item_id=?',
                       (json.dumps(metadata, ensure_ascii=False), cookie_id, item_id))
    recognized = card_metadata(text)
    if recognized:
        cursor.execute('UPDATE item_info SET platform_metadata=? WHERE cookie_id=? AND item_id=?',
                       (json.dumps(recognized, ensure_ascii=False), cookie_id, item_id))
        text = prose(recognized.get('description'))
        if not text:
            return False
    text = prose(text)
    if not text and not explicit:
        return False
    cursor.execute('''UPDATE item_info SET item_detail=?,detail_source=?,detail_status=?,
        detail_updated_at=?,detail_retry_at=NULL,detail_failure='',updated_at=CURRENT_TIMESTAMP
        WHERE cookie_id=? AND item_id=?''' + (" AND detail_source!='manual'" if source != 'manual' else ''),
        (text, source, 'ready' if text or source == 'manual' else 'missing', time.time(), cookie_id, item_id))
    return cursor.rowcount > 0


ACCOUNT_FAILURES = {'login_expired', 'security_verification_required', 'authentication_required'}
MESSAGES = {
    'authentication_required': '历史详情请求受限，原始原因未细分；请进行一次受控检查并查看诊断。',
    'login_expired': '商品详情接口明确报告登录会话过期；请更新同一账号的网页登录态。',
    'security_verification_required': '商品详情接口明确要求安全验证；请在官网商品页完成验证，再同步网页登录态。',
    'account_auth_paused': '账号认证处理尚未完成；请先在账号管理完成对应操作。',
    'signing_expired': '商品详情签名已过期，本轮未取得可用的新签名；请更新网页登录态。',
    'credentials_missing': '当前凭证缺少签名字段；请同步完整网页登录态。',
    'access_denied': '商品详情访问被拒绝，尚无证据表明需要安全验证；请查看诊断。',
    'rate_limited': '商品详情请求被限流，冷却后再检查。',
    'network_error': '商品详情网络请求失败，冷却后再检查。',
    'timeout': '商品详情请求超时，冷却后再检查。',
    'http_error': '商品详情服务暂时不可用，冷却后再检查。',
    'invalid_response': '商品详情响应无法识别，请查看诊断。',
    'description_not_found': '平台未返回可确认的商品说明，未使用整页文字代替。',
    'item_unavailable': '该商品不存在或当前账号无权读取。',
    'browser_authentication_required': '商品页限制浏览器读取；官方详情接口仍按冷却策略检查。',
    'browser_error': '商品详情浏览器读取失败，请查看本机浏览器诊断。',
    'interrupted': '上一次详情读取中断，冷却后再检查。',
}


def account(db, cookie_id):
    row = db.conn.execute('SELECT generation,pending,claimed,blocked,legacy_claimed FROM item_detail_accounts WHERE cookie_id=?', (cookie_id,)).fetchone()
    return row or (0, 0, -1, 0, 0)


def generation(db, cookie_id):
    with db.lock:
        return account(db, cookie_id)[0]


def credentials_changed(db, cookie_id, previous, current):
    from utils.xianyu_utils import trans_cookies
    if not current or not current.strip():
        return False
    try:
        previous_values = trans_cookies(previous) if previous and previous.strip() else {}
        current_values = trans_cookies(current)
    except (ValueError, TypeError):
        return False
    if previous_values == current_values:
        return False
    with db.lock:
        db.conn.execute('INSERT OR IGNORE INTO item_detail_accounts(cookie_id) VALUES(?)', (cookie_id,))
        db.conn.execute('UPDATE item_detail_accounts SET generation=generation+1,pending=1,blocked=0 WHERE cookie_id=?', (cookie_id,))
        db.conn.execute("UPDATE item_info SET detail_status='missing',detail_failure='',detail_retry_at=NULL,detail_browser_blocked=0 WHERE cookie_id=? AND detail_source!='manual' AND TRIM(COALESCE(item_detail,''))=''", (cookie_id,))
        db.conn.commit()
    return True


def take_pending(db, cookie_id):
    with db.lock:
        db.conn.execute('INSERT OR IGNORE INTO item_detail_accounts(cookie_id) VALUES(?)', (cookie_id,))
        c = db.conn.execute('UPDATE item_detail_accounts SET claimed=generation,pending=0 WHERE cookie_id=? AND pending=1 AND claimed!=generation', (cookie_id,))
        db.conn.commit()
        return bool(c.rowcount)


def claim(db, cookie_id, item_id, *, explicit=False):
    with db.lock:
        now = time.time()
        state = account(db, cookie_id)
        legacy = db.conn.execute("SELECT 1 FROM item_info WHERE cookie_id=? AND detail_status='blocked' AND detail_failure='authentication_required' LIMIT 1", (cookie_id,)).fetchone() if not state[4] else None
        target = db.conn.execute('SELECT detail_status,detail_failure FROM item_info WHERE cookie_id=? AND item_id=?', (cookie_id, item_id)).fetchone()
        legacy_probe = explicit and legacy and (target == ('blocked', 'authentication_required')) and (not state[4]) and (not state[3])
        if state[3] or (legacy and (not legacy_probe)):
            return False
        c = db.conn.execute("UPDATE item_info SET detail_status='fetching',detail_retry_at=?,detail_failure='' WHERE cookie_id=? AND item_id=? AND detail_source!='manual' AND TRIM(COALESCE(item_detail,''))='' AND detail_status!='fetching' AND (? OR detail_retry_at IS NULL OR detail_retry_at<=?)", (now + RETRY_SECONDS, cookie_id, item_id, bool(legacy_probe), now))
        if c.rowcount and legacy_probe:
            record_diagnostic(db, cookie_id, item_id, state[0], 'authentication_required', stage='legacy_state')
            db.conn.execute('INSERT OR IGNORE INTO item_detail_accounts(cookie_id) VALUES(?)', (cookie_id,))
            db.conn.execute('UPDATE item_detail_accounts SET legacy_claimed=1 WHERE cookie_id=?', (cookie_id,))
        db.conn.commit()
        return bool(c.rowcount)


def failure(db, cookie_id, item_id, category, *, expected_generation=None):
    with db.lock:
        if expected_generation is not None and generation(db, cookie_id) != expected_generation:
            return
        db.conn.execute("UPDATE item_info SET detail_status=?,detail_failure=?,detail_retry_at=? WHERE cookie_id=? AND item_id=? AND detail_source!='manual' AND TRIM(COALESCE(item_detail,''))=''", ('blocked' if category in ACCOUNT_FAILURES else 'failed', category, time.time() + RETRY_SECONDS, cookie_id, item_id))
        if category in ACCOUNT_FAILURES:
            db.conn.execute('INSERT OR IGNORE INTO item_detail_accounts(cookie_id) VALUES(?)', (cookie_id,))
            db.conn.execute('UPDATE item_detail_accounts SET blocked=1 WHERE cookie_id=?', (cookie_id,))
        db.conn.commit()


def record_diagnostic(db, cookie_id, item_id, gen, category, stage='api', http_status=None, codes=(), elapsed_ms=0):
    safe = [c for c in codes if c in SAFE_CODES][:8]
    category = category if category in MESSAGES or category in ('', 'platform_error', 'item_identity_mismatch') else 'invalid_response'
    stage = stage if stage in ('api', 'extraction', 'browser', 'legacy_state') else 'api'
    identity = secrets.token_hex(16)
    with db.lock:
        db.conn.execute('INSERT INTO item_detail_diagnostics VALUES(?,?,?,?,?,?,?,?,?,?)', (identity, cookie_id, item_id, gen, time.time(), category, stage, http_status, json.dumps(safe), max(0, int(elapsed_ms))))
        db.conn.execute('DELETE FROM item_detail_diagnostics WHERE created_at<?', (time.time() - 7 * 86400,))
        db.conn.execute('DELETE FROM item_detail_diagnostics WHERE cookie_id=? AND id NOT IN (SELECT id FROM item_detail_diagnostics WHERE cookie_id=? ORDER BY created_at DESC LIMIT 500)', (cookie_id, cookie_id))
        db.conn.commit()
    return identity


SAFE_CODES={'SUCCESS','FAIL_SYS_USER_VALIDATE','FAIL_SYS_SESSION_EXPIRED','FAIL_SYS_TOKEN_EXOIRED','FAIL_SYS_TOKEN_EXPIRED','FAIL_SYS_TOKEN_EMPTY','FAIL_SYS_ILLEGAL_ACCESS','FAIL_SYS_TRAFFIC_LIMIT','FAIL_BIZ_ITEM_NOT_FOUND','FAIL_BIZ_ITEM_NO_PERMISSION','NEED_LOGIN','LOGIN_REQUIRED','CAPTCHA'}


def classify(http_status, result, location=''):
    raw_codes = result.get('ret', []) if isinstance(result, dict) else []
    codes = [v.split('::', 1)[0] for v in raw_codes[:8] if isinstance(v, str)] if isinstance(raw_codes, list) else []
    if any((c in ('FAIL_SYS_USER_VALIDATE', 'CAPTCHA') for c in codes)):
        return ('security_verification_required', codes)
    if any((c in ('FAIL_SYS_SESSION_EXPIRED', 'NEED_LOGIN', 'LOGIN_REQUIRED') for c in codes)):
        return ('login_expired', codes)
    if any((c in ('FAIL_SYS_TOKEN_EXOIRED', 'FAIL_SYS_TOKEN_EXPIRED', 'FAIL_SYS_TOKEN_EMPTY') for c in codes)):
        return ('signing_expired', codes)
    if http_status == 429 or 'FAIL_SYS_TRAFFIC_LIMIT' in codes:
        return ('rate_limited', codes)
    if any((c in ('FAIL_BIZ_ITEM_NOT_FOUND', 'FAIL_BIZ_ITEM_NO_PERMISSION') for c in codes)):
        return ('item_unavailable', codes)
    if 300 <= http_status < 400:
        from urllib.parse import urlsplit
        try:
            parsed = urlsplit(location)
            path = parsed.path.lower()
            host = (parsed.hostname or '').lower()
        except ValueError:
            return ('invalid_response', codes)
        trusted = host.endswith('.taobao.com') or host.endswith('.goofish.com') or host in ('taobao.com', 'goofish.com')
        if trusted and any((k in path for k in ('punish', 'captcha'))):
            return ('security_verification_required', codes)
        if trusted and ('login' in path or host.startswith('login.')):
            return ('login_expired', codes)
        return ('access_denied', codes)
    if http_status in (401, 403) or 'FAIL_SYS_ILLEGAL_ACCESS' in codes:
        return ('access_denied', codes)
    if http_status >= 400:
        return ('http_error', codes)
    if not isinstance(result, dict) or not codes:
        return ('invalid_response', codes)
    if 'SUCCESS' in codes:
        return ('', codes)
    return ('platform_error', codes)


def diagnostic(db, identity, cookie_id):
    with db.lock:
        row = db.conn.execute('SELECT id,item_id,created_at,category,stage,http_status,codes,elapsed_ms FROM item_detail_diagnostics WHERE id=? AND cookie_id=?', (identity, cookie_id)).fetchone()
    if not row:
        return None
    out = dict(zip(('diagnostic_id', 'item_id', 'created_at', 'failure_category', 'stage', 'http_status', 'platform_codes', 'elapsed_ms'), row))
    try:
        raw_codes = json.loads(out['platform_codes'])
    except (ValueError, TypeError):
        raw_codes = []
    out['platform_codes'] = [code for code in raw_codes if isinstance(code, str) and code in SAFE_CODES][:8] if isinstance(raw_codes, list) else []
    if out['failure_category'] not in MESSAGES and out['failure_category'] not in ('', 'platform_error', 'item_identity_mismatch'):
        out['failure_category'] = 'invalid_response'
    if out['stage'] not in ('api', 'extraction', 'browser', 'legacy_state'):
        out['stage'] = 'api'
    out['message'] = MESSAGES.get(out['failure_category'], '详情请求未完成，请查看诊断。') if out['failure_category'] else '商品详情接口读取成功。'
    if out['stage'] == 'legacy_state':
        out['message'] = '历史阻塞状态存档；当时未采集具体HTTP或平台错误码，不代表本次请求结果。'
    return out


def presentation(db, cookie_id, item):
    from app.services import auth_recovery
    out = dict(item)
    with db.lock:
        state = account(db, cookie_id)
        row = db.conn.execute('SELECT id,created_at FROM item_detail_diagnostics WHERE cookie_id=? AND item_id=? AND generation=? ORDER BY created_at DESC LIMIT 1', (cookie_id, item['item_id'], state[0])).fetchone()
        legacy = db.conn.execute("SELECT 1 FROM item_info WHERE cookie_id=? AND detail_status='blocked' AND detail_failure='authentication_required' LIMIT 1", (cookie_id,)).fetchone() if not state[4] else None
    paused = auth_recovery.get(db, cookie_id)['auth_paused']
    category = 'account_auth_paused' if paused else item.get('detail_failure', '')
    if not category and state[3] and not prose(item.get('item_detail')):
        with db.lock:
            blocked_reason = db.conn.execute("SELECT detail_failure FROM item_info WHERE cookie_id=? AND detail_failure IN ('login_expired','security_verification_required','authentication_required') ORDER BY updated_at DESC LIMIT 1", (cookie_id,)).fetchone()
        category = blocked_reason[0] if blocked_reason else 'authentication_required'
    can = not paused and (not state[3]) and (item.get('detail_source') != 'manual') and (not prose(item.get('item_detail'))) and (item.get('detail_status') != 'fetching') and (not legacy or not state[4]) and (not item.get('detail_retry_at') or item['detail_retry_at'] <= time.time() or bool(legacy and (not state[4])))
    out.update(detail_diagnostic_id=row[0] if row else None, detail_last_attempt_at=row[1] if row else None, detail_failure_message=MESSAGES.get(category, '商品说明暂未获取，冷却后可继续检查。') if category else '', detail_can_refresh=bool(can), detail_actions=(['sync_web_session'] if paused or state[3] or category in ('credentials_missing', 'signing_expired') else []) + (['check_detail'] if can else []) + (['view_diagnostic'] if row else []))
    return out


async def extract_page_description(page):
    """Only visible product-description nodes; never the full page or recommendation cards."""
    address = page.url.lower()
    if any(word in address for word in ('login.', '/login', 'punish', 'captcha', 'sec.taobao')):
        raise DetailFetchError('authentication_required')
    for node in await page.locator('#nocaptcha, #nc_1_wrapper, iframe[src*="captcha"], iframe[src*="punish"]').all():
        if await node.is_visible():
            raise DetailFetchError('authentication_required')
    # CSS module hashes vary between official deployments. Do not use generic [class*=desc].
    selectors = ('[class^="desc--"]', '[class*=" desc--"]', '[data-testid="item-description"]')
    for selector in selectors:
        for node in await page.locator(selector).all():
            if not await node.is_visible():
                continue
            text = (await node.inner_text()).strip()
            if not text:
                continue
            if any(marker in text for marker in ("Oops", '请先登录', '请完成验证', '滑动验证', '访问受限', '非法访问')):
                raise DetailFetchError('authentication_required')
            return text
    # Header/login templates may exist in a healthy product page. Only inspect visible
    # prompts when no real product description was found.
    for marker in ('请先登录', '请完成验证', '非法访问', "Oops... something's wrong"):
        for node in await page.get_by_text(marker, exact=True).all():
            if await node.is_visible():
                raise DetailFetchError('authentication_required')
    raise DetailFetchError('description_not_found')


def resume_after_auth(db, cookie_id):
    # Message connectivity does not prove product-detail access. Explicit credential
    # submissions alone create a new acquisition generation via credentials_changed.
    return None


def api_description(result, item_id):
    """Official website renders data.itemDO.desc, not labels or tracking fields."""
    data = result.get('data')
    if not isinstance(data, dict):
        return ''
    item = data.get('itemDO')
    if not isinstance(item, dict):
        return ''
    identity = item.get('itemId') or (data.get('trackParams') or {}).get('itemId')
    if identity is not None and str(identity) != str(item_id):
        raise DetailFetchError('item_identity_mismatch')
    return prose(item.get('desc'))


def block_browser(db, cookie_id, item_id):
    with db.lock:
        db.conn.execute('UPDATE item_info SET detail_browser_blocked=1 WHERE cookie_id=? AND item_id=?', (cookie_id,item_id))
        db.conn.commit()
