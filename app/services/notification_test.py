"""账号通知规则的测试发送服务。

事件测试遵守订阅及启用状态；只有明确确认的渠道测试可以绕过订阅。
"""

from __future__ import annotations

import secrets
import time
import math
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional
from app.notification_events import EVENT_DEFINITIONS, EVENT_IDS

from app.services.notification_channels import NotificationChannelConfigError
from app.services.notification_sender import (
    NotificationNetworkError,
    NotificationProviderRejected,
    NotificationSendTimeout,
    NotificationDeliveryUncertain,
    NotificationSendError,
)


class NotificationTestError(Exception):
    """测试发送失败，code 对前端稳定，status_code 对 HTTP 状态。"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 502,
        retry_after: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retry_after = retry_after

    def detail(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message}


class NotificationTestRateLimiter:
    """按 (channel_id, user_id) 限流，多个规则共用渠道时不能绕过限制。"""

    def __init__(self, limit: int = 10, window_seconds: int = 60) -> None:
        self.limit = max(1, int(limit))
        self.window_seconds = max(1, int(window_seconds))
        self._hits: Dict[tuple, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def acquire(self, key: tuple) -> Optional[int]:
        """尝试占用一个名额；被限流时返回建议的重试等待秒数。"""
        with self._lock:
            now = time.monotonic()
            hits = self._hits[key]
            while hits and now - hits[0] >= self.window_seconds:
                hits.popleft()
            if len(hits) >= self.limit:
                return max(1, math.ceil(self.window_seconds - (now - hits[0])))
            hits.append(now)
            return None


def build_test_message(cookie_id: str, event_type: str | None = None) -> str:
    """构造测试消息模板，{request_id} 由服务层填充以便追踪。"""
    event_label = next((item['label'] for item in EVENT_DEFINITIONS if item['id'] == event_type), '')
    purpose = (
        f"测试订阅项：{event_label}（仅用于检查是否允许投递）\n"
        "订阅检查已通过，本次只验证通知投递。"
        if event_type else
        "测试方式：手动渠道测试，仅检查通知投递；本次已确认忽略事件订阅。"
    )
    return (
        "【手动测试 · 无需处理】闲鱼超级管家\n"
        "这是一封手动触发的测试通知，不是账号异常告警。\n"
        "本次没有检测Cookie是否失效，也不会改变账号的监听状态。\n"
        f"账号：{cookie_id}\n"
        "追踪 ID：{request_id}\n"
        f"{purpose}\n"
        "账号当前状态请查看管家“账号管理”；收到本邮件不需要重新登录闲鱼。\n"
        "收到本消息说明通知投递成功，无需反复点击测试。"
    )


# 每个用户的同一渠道每分钟最多尝试发送一次；被过滤的事件不占发信配额。
notification_test_rate_limiter = NotificationTestRateLimiter(limit=1, window_seconds=60)


class NotificationTestService:
    """发送通知测试消息并输出脱敏审计日志。"""

    def __init__(
        self,
        db: Any,
        sender: Any = None,
        limiter: Optional[NotificationTestRateLimiter] = None,
        log_fn: Optional[Callable[..., None]] = None,
    ) -> None:
        self.db = db
        self.sender = sender
        self.limiter = limiter

        if log_fn is not None:
            self._log = log_fn
        else:
            from loguru import logger

            self._log = lambda level, message, user: getattr(logger, level)(message)

    def _log_safe(self, level: str, message: str, operator: Optional[dict]) -> None:
        """审计日志只输出结构性字段，绝不携带渠道配置（内含 webhook token 等敏感值）。"""
        try:
            self._log(level, message, operator)
        except TypeError:
            # loguru 风格的 log_fn 只接受 (level, message)
            self._log(level, message)

    async def send_rule_test(
        self,
        rule_id: int,
        user_id: int,
        operator: Optional[Dict[str, Any]] = None,
        *,
        mode: str = 'event',
        event_type: str | None = None,
        confirm_channel_send: bool = False,
    ) -> Dict[str, Any]:
        target = self.db.get_notification_test_target(rule_id, user_id)
        if not target:
            raise NotificationTestError(
                "notification_rule_not_found",
                "通知规则不存在或无权限",
                status_code=404,
            )

        if mode not in ('event', 'channel') or (mode == 'event' and event_type not in EVENT_IDS):
            raise NotificationTestError('notification_test_mode_invalid', '请选择要测试的通知事件', 400)
        if mode == 'channel' and not confirm_channel_send:
            raise NotificationTestError('notification_test_confirmation_required', '渠道测试会真实发信且忽略事件勾选，请先确认', 400)

        request_id = secrets.token_hex(6)
        if mode == 'event':
            # 与正式发送共用数据库选择器，读取当前已保存的订阅，不依赖页面缓存。
            matching = self.db.get_account_notifications(target['cookie_id'], user_id, event_type=event_type)
            accepted = target.get('channel_enabled') and any(
                row['id'] == rule_id and row['enabled'] for row in matching
            )
            if not accepted:
                self._log_safe('info', f'source=notification_test rule_id={rule_id} request_id={request_id} '
                               f'mode=event event_type={event_type} result=skipped user_id={user_id}', operator)
                return {
                    'success': True, 'sent': False, 'skipped': True, 'mode': mode, 'event_type': event_type,
                    'message': '该事件未订阅或规则/渠道已停用，未发送任何通知',
                    'request_id': request_id,
                    'channel': {'id': target['channel_id'], 'name': target['channel_name'], 'type': target['channel_type']},
                }

        if self.limiter is not None:
            retry_after = self.limiter.acquire((int(target['channel_id']), int(user_id)))
            if retry_after is not None:
                raise NotificationTestError(
                    "notification_rate_limited",
                    "测试发送过于频繁，请稍后再试",
                    status_code=429,
                    retry_after=retry_after,
                )

        started_at = time.monotonic()
        message = build_test_message(target["cookie_id"], event_type if mode == 'event' else None).format(request_id=request_id)

        self._log_safe(
            "info",
            f"source=notification_test rule_id={rule_id} request_id={request_id} mode={mode} "
            f"event_type={event_type if mode == 'event' else 'none'} user_id={user_id} result=started",
            operator,
        )

        try:
            if self.sender is None:
                raise NotificationTestError(
                    "notification_send_failed",
                    "通知发送器未初始化",
                    status_code=503,
                )
            receipt = await self.sender.send(
                target["channel_type"],
                target["channel_config"],
                message,
            )
            if not receipt:
                raise NotificationTestError(
                    "notification_send_failed",
                    "通知测试发送失败，请稍后重试",
                    status_code=502,
                )
        except NotificationChannelConfigError:
            raise NotificationTestError(
                "notification_config_invalid",
                "通知渠道配置不完整，请先补全再测试",
                status_code=400,
            )
        except NotificationDeliveryUncertain:
            self._log_safe('warning', f'source=notification_test request_id={request_id} result=uncertain', operator)
            raise NotificationTestError('notification_delivery_unknown', '邮件提交结果不确定，请先查收邮箱，不要立即重复发送', 504) from None
        except NotificationSendTimeout:
            raise NotificationTestError(
                "notification_send_timeout",
                "通知渠道响应超时，请检查网络或稍后重试",
                status_code=504,
            )
        except NotificationProviderRejected as exc:
            status = getattr(exc, "status_code", None) or 502
            raise NotificationTestError(
                "notification_provider_rejected",
                "通知渠道拒绝了本次请求",
                status_code=status,
            )
        except NotificationNetworkError:
            raise NotificationTestError(
                "notification_send_failed",
                "通知渠道网络异常，请稍后重试",
                status_code=502,
            )
        except NotificationTestError:
            raise
        except NotificationSendError as exc:
            raise NotificationTestError(exc.code, exc.public_message, exc.status_code or 502) from None
        except Exception:
            # 未知异常的消息可能包含敏感值，不回显给前端，只给稳定文案
            raise NotificationTestError(
                "notification_send_failed",
                "通知测试发送失败，请稍后重试",
                status_code=502,
            )

        self._log_safe(
            "info",
            f"source=notification_test rule_id={rule_id} request_id={request_id} "
            f"channel_id={target['channel_id']} mode={mode} result=success",
            operator,
        )

        return {
            "success": True,
            "sent": True,
            "skipped": False,
            "mode": mode,
            "event_type": event_type if mode == 'event' else None,
            # 发送器返回只代表渠道已接受提交，不代表已经进入用户收件箱。
            "message": (
                "测试邮件已提交（手动测试，不是账号异常告警），请检查收件箱或垃圾邮件"
                if target["channel_type"] == "email"
                else "测试通知已提交（手动测试，不是账号异常告警），请检查接收端"
            ),
            "request_id": request_id,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": round((time.monotonic() - started_at) * 1000),
            "channel": {
                "id": target["channel_id"],
                "name": target["channel_name"],
                "type": target["channel_type"],
            },
            "rule_enabled": bool(target.get("enabled")),
            "channel_enabled": bool(target.get("channel_enabled")),
        }
