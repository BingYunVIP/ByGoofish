"""Product image URLs and strictly scoped main-image extraction. Never fetch arbitrary URLs."""
import json
import re
from urllib.parse import urlsplit


def normalize_image_url(value):
    if not isinstance(value, str):
        return ''
    value = value.strip()
    if not value or re.search(r'[\x00-\x20\\]', value):
        return ''
    if value.startswith('/') and not value.startswith('//'):
        return value
    if value.startswith('//'):
        value = 'https:' + value
    try:
        parts = urlsplit(value)
        if parts.username is not None or parts.password is not None:
            return ''
        host = (parts.hostname or '').lower()
        if parts.scheme == 'https' and host:
            return value
        if parts.scheme == 'http' and (host == 'alicdn.com' or host.endswith('.alicdn.com')) and parts.port is None:
            return 'https' + value[4:]
    except ValueError:
        pass
    return ''


def initialize(db):
    """Idempotently upgrade known CDN URLs only; preserve listing evidence and prose."""
    with db.lock:
        for rowid, value in db.conn.execute("SELECT id,item_image FROM item_info WHERE item_image LIKE 'http://%' OR item_image LIKE '//%'").fetchall():
            normalized = normalize_image_url(value)
            if normalized and normalized != value:
                db.conn.execute('UPDATE item_info SET item_image=? WHERE id=?', (normalized, rowid))
        db.conn.commit()


def main_image(result, item_id):
    data = result.get('data') if isinstance(result, dict) else None
    item = data.get('itemDO') if isinstance(data, dict) else None
    if not isinstance(item, dict):
        return ''
    tracking = data.get('trackParams') if isinstance(data.get('trackParams'), dict) else {}
    identity = item.get('itemId') or tracking.get('itemId')
    if str(identity or '') != str(item_id):
        return ''
    candidates = [item.get(key) for key in ('mainPic', 'mainImage', 'picUrl', 'picInfo')]
    images = item.get('imageInfos') or item.get('images') or []
    if isinstance(images, str):
        try:
            images = json.loads(images)
        except ValueError:
            images = []
    if isinstance(images, list):
        candidates.extend(sorted(images, key=lambda image: not bool(isinstance(image, dict) and image.get('major'))))
    for image in candidates:
        if isinstance(image, dict):
            image = image.get('url') or image.get('picUrl')
        url = normalize_image_url(image)
        if url.startswith('https://'):
            return url
    return ''


def fill_missing(db, cookie_id, item_id, result):
    url = main_image(result, item_id)
    if not url:
        return ''
    with db.lock:
        db.conn.execute("UPDATE item_info SET item_image=? WHERE cookie_id=? AND item_id=? AND TRIM(COALESCE(item_image,''))=''",
                        (url, cookie_id, item_id))
        db.conn.commit()
        row = db.conn.execute('SELECT item_image FROM item_info WHERE cookie_id=? AND item_id=?',(cookie_id,item_id)).fetchone()
    return normalize_image_url(row[0]) if row else ''
