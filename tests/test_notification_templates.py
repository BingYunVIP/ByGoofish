"""通知模板的用户隔离、验证链接和实际发送入口回归；不访问外网。"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.db_manager import DBManager
from app import reply_server
from app.auth import get_current_user
from app.services.notification_templates import (
    PREFIX, DEFAULT_TEMPLATE, HTML_EMAIL_MARKER, validate_template, render_template,
    render_account_notification, preview_template, notification_plain_text, split_email_message,
)


class NotificationTemplateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DBManager(str(Path(self.tmp.name) / 'fixture.db'))
        self.owner = self.db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        self.other = self.db.conn.execute(
            "INSERT INTO users(username,email,password_hash) VALUES ('template-other','other@example.test','fixture')"
        ).lastrowid
        self.db.conn.commit()
        self.assertTrue(self.db.save_cookie('template-account', 'fixture=value', self.owner))
        self.user_id = self.owner
        reply_server.app.dependency_overrides[get_current_user] = lambda: {'user_id': self.user_id, 'username': 'fixture'}
        self.patches = [patch.object(reply_server, 'db_manager', self.db), patch('app.db_manager.db_manager', self.db)]
        for item in self.patches:
            item.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=reply_server.app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        reply_server.app.dependency_overrides.pop(get_current_user, None)
        for item in reversed(self.patches):
            item.stop()
        self.db.close()
        self.tmp.cleanup()

    async def test_save_preview_and_owner_isolation(self):
        content = '{{event}}\n{{account}}\n{{message}}'
        response = await self.client.put('/notification-templates/buyer_message', json={'content': content})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await self.client.get('/notification-templates')).json()['templates']['buyer_message'], content)
        self.assertEqual(render_account_notification(self.db, 'template-account', 'buyer_message', '原文'),
                         '买家消息\ntemplate-account\n原文')
        self.user_id = self.other
        self.assertEqual((await self.client.get('/notification-templates')).json()['templates']['buyer_message'], DEFAULT_TEMPLATE)
        await self.client.put('/notification-templates/buyer_message', json={'content': '其他用户'})
        self.assertNotEqual(render_account_notification(self.db, 'template-account', 'buyer_message', '原文'), '其他用户')
        preview = await self.client.post('/notification-templates/preview', json={'event_type': 'buyer_message', 'content': content})
        self.assertEqual(preview.status_code, 200)
        self.assertIn('示例账号', preview.json()['content'])

    async def test_html_preview_reports_rendering_mode_and_uses_example_data(self):
        response = await self.client.post('/notification-templates/preview', json={
            'event_type': 'message_verification_required', 'content': DEFAULT_TEMPLATE,
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['is_html'])
        self.assertIn('示例账号', response.json()['content'])
        self.assertEqual(response.json()['content'].count('class="button"'), 1)

    async def test_invalid_content_is_rejected_without_overwrite(self):
        for event, content, expected in [('unknown', 'text', 400), ('buyer_message', '{{secret}}', 400),
                                         ('buyer_message', '{{message', 400), ('buyer_message', 'x' * 8001, 422)]:
            response = await self.client.put(f'/notification-templates/{event}', json={'content': content})
            self.assertEqual(response.status_code, expected)
        self.assertIsNone(self.db.get_user_setting(self.owner, PREFIX + 'buyer_message'))

    async def test_restore_default_and_blank_template(self):
        await self.client.put('/notification-templates/buyer_message', json={'content': 'custom'})
        response = await self.client.put('/notification-templates/buyer_message', json={'content': '   '})
        self.assertEqual(response.json()['content'], DEFAULT_TEMPLATE)
        rendered = render_account_notification(self.db, 'template-account', 'buyer_message', '原文')
        self.assertTrue(rendered.startswith(HTML_EMAIL_MARKER))
        self.assertIn('原文', notification_plain_text(rendered))

    async def test_missing_account_keeps_original_and_invalid_template_uses_default(self):
        self.assertEqual(render_account_notification(self.db, 'missing', 'buyer_message', '原文'), '原文')
        self.db.set_user_setting(self.owner, PREFIX + 'buyer_message', '{{unknown}}')
        fallback = notification_plain_text(render_account_notification(self.db, 'template-account', 'buyer_message', '原文'))
        self.assertIn('原文', fallback)
        self.assertIn('建议操作', fallback)

    async def test_verification_link_is_retained_and_input_is_not_evaluated(self):
        self.db.set_user_setting(self.owner, PREFIX + 'message_verification_required', '{{account}}')
        message = render_account_notification(self.db, 'template-account', 'message_verification_required', 'original', verification_url='https://example.test/verify')
        self.assertIn('https://example.test/verify', message)
        self.assertEqual(render_template('{{message}}', {'message': '{{account}} <script>example</script>'}),
                         '{{account}} <script>example</script>')
        self.assertIn('示例', preview_template('message_verification_required', '{{message}}'))

    async def test_html_email_template_is_sanitized_and_has_plain_text_fallback(self):
        source = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><style>body { color: #18181b; }</style></head>
        <body><h1>{{event}}</h1><p>{{message}}</p><script>steal()</script>
        <img src="https://tracker.example/pixel.png"><a href="javascript:alert(1)">bad link</a></body></html>'''
        safe = validate_template('message_verification_required', source)
        self.assertNotIn('<script', safe.lower())
        self.assertNotIn('<img', safe.lower())
        self.assertNotIn('javascript:', safe.lower())
        rendered = render_template(safe, {
            'event': '人工验证提醒', 'message': '<script>买家输入</script>',
            'verification_url': 'https://example.test/verify?ticket=one',
        })
        self.assertTrue(rendered.startswith(HTML_EMAIL_MARKER))
        plain, html_body = split_email_message(rendered)
        self.assertIn('&lt;script&gt;买家输入&lt;/script&gt;', html_body)
        self.assertIn('打开验证页面', html_body)
        self.assertIn('人工验证提醒', plain)
        self.assertNotIn('body { color', plain)
        preview = preview_template('message_verification_required', DEFAULT_TEMPLATE)
        self.assertTrue(preview.startswith('<!DOCTYPE html>'))
        self.assertIn('href="https://example.test/verify"', preview)
        self.assertEqual(preview.count('<a class="button"'), 1)
        custom_link = '<!doctype html><html><body><a class="button" href="{{verification_url}}">Verify</a></body></html>'
        invalid_link = render_template(validate_template('message_verification_required', custom_link), {
            'verification_url': 'javascript:alert(1)', 'message': '',
        })
        self.assertNotIn('javascript:', invalid_link)
        self.assertNotIn('>Verify</a>', invalid_link)
        valid_link = render_template(validate_template('message_verification_required', custom_link), {
            'verification_url': 'https://example.test/verify', 'message': '',
        })
        self.assertIn('href="https://example.test/verify"', valid_link)

    async def test_event_test_renders_saved_template_without_bypassing_subscription(self):
        from app.services.notification_test import NotificationTestService
        from app.services.notification_sender import NotificationSendReceipt

        template = '<!DOCTYPE html><html><body><h1>{{event}}</h1><p>{{message}}</p></body></html>'
        self.db.set_user_setting(self.owner, PREFIX + 'message_verification_required', template)
        channel_id = self.db.create_notification_channel('event-template-test', 'email', '{}', self.owner)
        self.db.set_message_notification('template-account', channel_id, True,
                                         event_types=[])

        class RecordingSender:
            def __init__(self):
                self.message = ''

            async def send(inner_self, _channel_type, _config, message):
                inner_self.message = message
                return NotificationSendReceipt('email')

        sender = RecordingSender()
        service = NotificationTestService(self.db, sender=sender)
        skipped = await service.send_rule_test(channel_id, self.owner, mode='event', event_type='message_verification_required')
        self.assertTrue(skipped['skipped'])
        self.assertFalse(skipped['sent'])
        self.assertEqual(sender.message, '')
        self.db.set_message_notification('template-account', channel_id, True,
                                         event_types=['message_verification_required'])
        result = await service.send_rule_test(channel_id, self.owner, mode='event', event_type='message_verification_required')
        self.assertTrue(result['sent'])
        self.assertTrue(sender.message.startswith(HTML_EMAIL_MARKER))
        self.assertIn('手动触发的测试通知', notification_plain_text(sender.message))

    async def test_all_formal_notification_families_use_saved_template(self):
        from XianyuAutoAsync import XianyuLive
        instance = XianyuLive.__new__(XianyuLive)
        instance.cookie_id = 'template-account'
        instance.last_notification_time = {}
        instance.notification_lock = asyncio.Lock()
        instance.notification_cooldown = 0
        instance.token_refresh_notification_cooldown = 0
        instance._send_webhook_notification = AsyncMock()
        for event in ['delivery_timeout', 'buyer_message', 'credentials_invalid', 'delivery_failed']:
            self.db.set_user_setting(self.owner, PREFIX + event, f'定制{event} {{{{account}}}}')
        channels = [{'id': 999, 'enabled': True, 'channel_type': 'webhook', 'channel_config': '{}'}]
        with patch.object(self.db, 'get_account_notifications', return_value=channels) as subscriptions:
            await instance.send_system_notification('original', 'delivery_timeout')
            await instance.send_notification('buyer', 'buyer-id', 'hello')
            await instance.send_token_refresh_notification('Cookie失效', 'credentials_invalid')
            await instance.send_delivery_failure_notification('buyer', 'buyer-id', 'item', 'failure')
        self.assertEqual(instance._send_webhook_notification.await_count, 4)
        self.assertEqual([call.args[1] for call in instance._send_webhook_notification.await_args_list],
                         [f'定制{event} template-account' for event in ['delivery_timeout', 'buyer_message', 'credentials_invalid', 'delivery_failed']])
        self.assertEqual([call.kwargs['event_type'] for call in subscriptions.call_args_list],
                         ['delivery_timeout', 'buyer_message', 'credentials_invalid', 'delivery_failed'])

    async def test_quick_phrase_image_upload_reference_is_validated_and_persisted(self):
        upload_root = Path(self.tmp.name) / 'uploads'
        upload_root.mkdir()
        (upload_root / 'abcd1234_12345678.jpg').write_bytes(b'fixture')
        with patch.object(reply_server, 'uploads_dir', str(upload_root)):
            valid = 'static/uploads/images/abcd1234_12345678.jpg'
            response = await self.client.post('/quick-phrases', data={
                'title': '图片话术', 'content': '', 'category': '售后', 'image_url': valid,
            })
            self.assertEqual(response.status_code, 200, response.text)
            phrase_id = response.json()['id']
            phrases = await self.client.get('/quick-phrases')
            phrase = next(item for item in phrases.json()['data'] if item['id'] == phrase_id)
            self.assertEqual(phrase['image_url'], valid)
            self.assertEqual(phrase['content'], '')

            invalid = await self.client.post('/quick-phrases', data={
                'title': '无效图片', 'content': '你好', 'image_url': 'https://outside.example/image.jpg',
            })
            self.assertEqual(invalid.status_code, 400)
            self.assertFalse(any(item['title'] == '无效图片' for item in (await self.client.get('/quick-phrases')).json()['data']))

            updated = await self.client.put(f'/quick-phrases/{phrase_id}', data={'image_url': '__clear__'})
            self.assertEqual(updated.status_code, 200, updated.text)
            phrase = next(item for item in (await self.client.get('/quick-phrases')).json()['data'] if item['id'] == phrase_id)
            self.assertEqual(phrase['image_url'], '')

    async def test_templates_do_not_bypass_disabled_event_subscription(self):
        from XianyuAutoAsync import XianyuLive
        instance = XianyuLive.__new__(XianyuLive)
        instance.cookie_id = 'template-account'
        instance._send_webhook_notification = AsyncMock()
        self.db.set_user_setting(self.owner, PREFIX + 'delivery_timeout', 'custom')
        with patch.object(self.db, 'get_account_notifications', return_value=[]):
            self.assertEqual(await instance.send_system_notification('original'), 0)
        instance._send_webhook_notification.assert_not_awaited()

    async def test_slider_notification_also_uses_template_and_retains_link(self):
        import json
        from utils.slider_patch import send_notification
        self.db.set_user_setting(self.owner, PREFIX + 'login_verification_required', '请验证 {{account}}')
        channels = [{'id': 999, 'enabled': True, 'channel_type': 'email', 'channel_config': json.dumps({
            'smtp_server': 'smtp.example.test', 'smtp_port': 465, 'email_user': 'fixture@example.test',
            'email_password': 'fixture', 'recipient_email': 'fixture@example.test',
        })}]
        with patch.object(self.db, 'get_account_notifications', return_value=channels), \
             patch('app.services.smtp_delivery.send_message') as sender:
            sent = await asyncio.to_thread(send_notification, 'template-account', 'fixture', 'original',
                                          verification_url='https://example.test/verify')
        self.assertTrue(sent)
        message = sender.call_args.args[0].get_content().rstrip('\n')
        self.assertEqual(message, '请验证 template-account\n验证链接: https://example.test/verify')


if __name__ == '__main__':
    unittest.main()
