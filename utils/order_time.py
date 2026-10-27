"""平台的无时区订单时间为北京时间；数据库统一保存UTC，避免日期筛选错位。"""
from datetime import datetime, timezone, timedelta

SHANGHAI = timezone(timedelta(hours=8))


def platform_order_time(value):
    if value in (None, ''):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).replace('.', '', 1).isdigit():
            stamp = float(value)
            if stamp >= 1e11:
                stamp /= 1000
            date = datetime.fromtimestamp(stamp, timezone.utc)
        else:
            date = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
            if date.tzinfo is None:
                date = date.replace(tzinfo=SHANGHAI)
        return date.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def fetched_order_time(result):
    value = result.get('order_time') or None
    if result.get('from_seller_api') or result.get('from_cache'):
        return value
    return platform_order_time(value)
