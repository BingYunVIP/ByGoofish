"""Formal notification delivery with per-rule, non-sensitive receipts."""
import secrets
from loguru import logger


async def deliver(instance, notifications, event_type, message, attachment_path=None, receipt_sink=None):
    sent = 0
    for rule in notifications:
        if not rule.get('enabled', True):
            continue
        request_id = secrets.token_hex(6)
        kind = rule.get('channel_type')
        methods = {
            'dingtalk': '_send_dingtalk_notification', 'ding_talk': '_send_dingtalk_notification',
            'feishu': '_send_feishu_notification', 'lark': '_send_feishu_notification',
            'bark': '_send_bark_notification', 'email': '_send_email_notification',
            'webhook': '_send_webhook_notification', 'wechat': '_send_wechat_notification',
            'telegram': '_send_telegram_notification',
        }
        result = 'unsupported'
        try:
            if kind in methods:
                config = instance._parse_notification_config(rule.get('channel_config'))
                from app.notification_events import get_event
                definition = get_event(event_type)
                if definition:
                    config['_event_subject'] = f"【{definition['severity'].upper()}】{definition['label']} · 闲鱼超级管家"
                method = getattr(instance, methods[kind])
                if kind == 'email' and receipt_sink is not None:
                    value = await method(config, message, attachment_path, strict=True)
                else:
                    value = await method(config, message, attachment_path) if kind == 'email' and attachment_path else await method(config, message)
                result = 'failed_or_unknown' if value is False else ('handler_completed' if value is None else 'submitted')
                if value is not False:
                    sent += 1
        except Exception as error:
            from app.services.smtp_delivery import DeliveryUncertain
            result = 'unknown' if isinstance(error, DeliveryUncertain) else 'failed'
        if receipt_sink is not None:
            receipt_sink.append({'rule_id': rule.get('id'), 'request_id': request_id, 'status': result})
        # Never record message bodies, addresses, provider responses or credentials.
        logger.info(f'source=notification_delivery event_type={event_type} rule_id={rule.get("id", 0)} '
                    f'channel={kind} request_id={request_id} result={result}')
    return sent
