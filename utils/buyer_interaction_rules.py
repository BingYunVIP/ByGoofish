"""订单完成时间门槛；避免升级后把历史完成订单当成新订单自动触达。"""
from datetime import datetime, timezone

from utils.order_time import platform_order_time


def completed_after(finish_time, activated_at) -> bool:
    normalized = platform_order_time(finish_time)
    if not normalized:
        return False
    try:
        finished = datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        ).timestamp()
        return finished >= float(activated_at)
    except (TypeError, ValueError, OverflowError, OSError):
        return False
