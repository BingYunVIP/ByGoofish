"""Product prose, provenance and bounded platform acquisition, separate from card metadata."""
import json
import time

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


def claim(db, cookie_id, item_id):
    with db.lock:
        now = time.time()
        c = db.conn.execute('''UPDATE item_info SET detail_status='fetching',detail_retry_at=?,detail_failure=''
            WHERE cookie_id=? AND item_id=? AND detail_source!='manual'
            AND TRIM(COALESCE(item_detail,''))='' AND (detail_retry_at IS NULL OR detail_retry_at<=?)
            AND NOT EXISTS (SELECT 1 FROM item_info blocked WHERE blocked.cookie_id=item_info.cookie_id AND blocked.detail_status='blocked')''',
            (now + RETRY_SECONDS, cookie_id, item_id, now))
        db.conn.commit()
        return c.rowcount > 0


def failure(db, cookie_id, item_id, category):
    with db.lock:
        db.conn.execute('''UPDATE item_info SET detail_status=?,detail_failure=?,detail_retry_at=?
            WHERE cookie_id=? AND item_id=? AND detail_source!='manual' AND TRIM(COALESCE(item_detail,''))='' ''',
            ('blocked' if category == 'authentication_required' else 'failed', category,
             time.time() + RETRY_SECONDS, cookie_id, item_id))
        db.conn.commit()


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
    # A successful manual credential/connection check may resume blocked product acquisition.
    cols = {r[1] for r in db.conn.execute('PRAGMA table_info(item_info)')}
    if 'detail_status' in cols:
        db.conn.execute("UPDATE item_info SET detail_status='missing',detail_failure='',detail_retry_at=NULL "
                        "WHERE cookie_id=? AND (detail_status='blocked' OR detail_failure='browser_authentication_required') AND detail_source!='manual'", (cookie_id,))
        db.conn.execute('UPDATE item_info SET detail_browser_blocked=0 WHERE cookie_id=?', (cookie_id,))


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
