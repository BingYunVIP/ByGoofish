"""消息中的商品标识只能来自明确的商品字段或商品链接，不能猜测长数字。"""
import json
from urllib.parse import parse_qs, urlsplit


def extract_item_id(message):
    visited = 0
    def visit(value, depth=0):
        nonlocal visited
        visited += 1
        if depth > 12 or visited > 2000:
            return None
        if isinstance(value, dict):
            for key in ('itemId', 'item_id'):
                candidate = value.get(key)
                if isinstance(candidate, (str, int)) and str(candidate).strip():
                    return str(candidate).strip()
            for child in value.values():
                found = visit(child, depth + 1)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = visit(child, depth + 1)
                if found:
                    return found
        elif isinstance(value, str):
            text = value.strip()
            if text.startswith(('{', '[')):
                try:
                    return visit(json.loads(text), depth + 1)
                except (ValueError, TypeError):
                    return None
            if '://' in text:
                try:
                    url = urlsplit(text)
                    query = parse_qs(url.query)
                    for key in ('itemId', 'item_id'):
                        if query.get(key):
                            return query[key][0]
                    # id只有在明确商品详情链接里才有商品含义。
                    if url.path.rstrip('/') in ('/item', '/item-detail') or url.netloc == 'item':
                        return (query.get('id') or [None])[0]
                except ValueError:
                    return None
        return None
    return visit(message)
