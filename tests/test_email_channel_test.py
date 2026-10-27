# -*- coding: utf-8 -*-
"""邮件渠道测试：未保存配置的发信、授权码沿用规则、内网目标限制与限流。

SMTP 发送动作用注入的发送函数代替（单元测试不真发信）；真实发信由冒烟验收覆盖。
"""

import asyncio
import json
import os
import tempfile
import unittest
from email.message import EmailMessage

from app.db_manager import DBManager
from app.services.notification_sender import NotificationSender
from app.services.notification_test import (
    NotificationTestError,
    NotificationTestRateLimiter,
    NotificationTestService,
)
from app.services.smtp_delivery import format_sender


class FakeDB:
    def __init__(self, channel=None):
        self.channel = channel

    def get_notification_channel(self, channel_id, user_id):
        return self.channel if self.channel and self.channel['id'] == channel_id else None


def saved_channel(**config):
    base = {'smtp_server': 'smtp.qq.com', 'smtp_port': 465, 'email_user': 'me@qq.com',
            'email_password': 'saved-code', 'recipient_email': 'me@qq.com'}
    return {'id': 7, 'type': 'email', 'config': json.dumps({**base, **config})}


class EmailConfigTestTests(unittest.TestCase):
    def make_service(self, channel=None, limiter=None):
        self.sent = []
        sender = NotificationSender(email_sender=lambda config, message: self.sent.append((config, message)))
        return NotificationTestService(FakeDB(channel), sender=sender, limiter=limiter,
                                       log_fn=lambda *args, **kwargs: None)

    def run_test(self, service, config, **kwargs):
        return asyncio.run(service.send_email_config_test(config, 1, **kwargs))

    @staticmethod
    def form(**overrides):
        values = {'smtp_server': 'smtp.qq.com', 'smtp_port': 465, 'email_user': 'me@qq.com',
                  'email_password': 'typed-code', 'smtp_from': '', 'recipient_email': 'me@qq.com'}
        values.update(overrides)
        return values

    def test_sends_with_form_values_and_labels_the_mail_as_manual_test(self):
        service = self.make_service()
        result = self.run_test(service, self.form(smtp_from='闲鱼超级管家'))
        config, message = self.sent[0]
        self.assertEqual((config['smtp_server'], config['smtp_port'], config['email_password']),
                         ('smtp.qq.com', 465, 'typed-code'))
        self.assertEqual(config['smtp_from'], '闲鱼超级管家')
        self.assertIn('手动测试', message)
        self.assertIn(result['request_id'], message)
        self.assertTrue(result['sent'])
        self.assertIn('不是账号异常告警', result['message'])

    def test_rejects_incomplete_or_malformed_fields(self):
        service = self.make_service()
        for overrides in ({'smtp_server': ''}, {'email_password': ''}, {'recipient_email': ''},
                          {'recipient_email': 'not-an-email'}, {'email_user': 'x'}):
            with self.assertRaises(NotificationTestError) as ctx:
                self.run_test(service, self.form(**overrides))
            self.assertEqual(ctx.exception.status_code, 400, overrides)
        self.assertEqual(self.sent, [])

    def test_blank_password_reuses_saved_one_only_when_server_and_user_unchanged(self):
        service = self.make_service(saved_channel())
        self.run_test(service, self.form(email_password=''), channel_id=7)
        self.assertEqual(self.sent[0][0]['email_password'], 'saved-code')

        # 换了服务器：不能把已保存的授权码带过去
        with self.assertRaises(NotificationTestError) as ctx:
            self.run_test(service, self.form(email_password='', smtp_server='smtp.163.com'), channel_id=7)
        self.assertEqual(ctx.exception.status_code, 400)
        # 换了发件邮箱同理
        with self.assertRaises(NotificationTestError):
            self.run_test(service, self.form(email_password='', email_user='other@qq.com'), channel_id=7)
        self.assertEqual(len(self.sent), 1)

    def test_other_users_channel_is_not_readable(self):
        service = self.make_service(None)  # get_notification_channel 按用户过滤，查不到
        with self.assertRaises(NotificationTestError):
            self.run_test(service, self.form(email_password=''), channel_id=7)

    def test_private_and_metadata_targets_are_blocked(self):
        service = self.make_service()
        with self.assertRaises(NotificationTestError) as ctx:
            self.run_test(service, self.form(smtp_server='127.0.0.1'), allow_private=False)
        self.assertEqual(ctx.exception.code, 'notification_target_forbidden')
        for allow_private in (False, True):  # 元数据地址即使管理员也不允许
            with self.assertRaises(NotificationTestError) as ctx:
                self.run_test(service, self.form(smtp_server='169.254.169.254'), allow_private=allow_private)
            self.assertEqual(ctx.exception.code, 'notification_target_forbidden')
        self.assertEqual(self.sent, [])
        self.run_test(service, self.form(smtp_server='127.0.0.1'), allow_private=True)  # 管理员可以用本机
        self.assertEqual(len(self.sent), 1)

    def test_repeated_tests_are_rate_limited(self):
        service = self.make_service(limiter=NotificationTestRateLimiter(limit=1, window_seconds=60))
        self.run_test(service, self.form())
        with self.assertRaises(NotificationTestError) as ctx:
            self.run_test(service, self.form())
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertTrue(ctx.exception.retry_after)
        self.assertEqual(len(self.sent), 1)


class SenderHeaderTests(unittest.TestCase):
    def test_display_name_keeps_the_login_address(self):
        message = EmailMessage()
        message['From'] = format_sender('闲鱼超级管家', 'me@qq.com')
        self.assertTrue(message['From'].endswith('<me@qq.com>'))
        self.assertEqual(format_sender('', 'me@qq.com'), 'me@qq.com')
        self.assertEqual(format_sender('ME@qq.com', 'me@qq.com'), 'me@qq.com')

    def test_newlines_in_display_name_cannot_inject_headers(self):
        message = EmailMessage()
        message['From'] = format_sender('x\r\nBcc: evil@example.com', 'me@qq.com')
        self.assertIsNone(message['Bcc'])


class MailSenderConfigTests(unittest.TestCase):
    """注册验证码等系统邮件只使用管理员的邮件通知渠道，不再有第二份 SMTP 设置。"""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.db = DBManager(os.path.join(self._temp.name, 'mail.db'))
        self.admin_id = self.db.get_user_by_username('admin')['id']

    def tearDown(self):
        self.db.conn.close()
        self._temp.cleanup()

    def add(self, channel_type, user_id, **config):
        base = {'smtp_server': 'smtp.qq.com', 'smtp_port': 465, 'email_user': 'me@qq.com', 'email_password': 'code'}
        return self.db.create_notification_channel('渠道', channel_type, json.dumps({**base, **config}), user_id)

    def test_none_without_an_admin_email_channel(self):
        self.assertIsNone(self.db.get_mail_sender_config())
        other = self.db.create_user('bob', 'bob@example.invalid', 'Passw0rd!x')
        self.add('email', self.db.get_user_by_username('bob')['id'])
        self.assertIsNone(self.db.get_mail_sender_config())  # 别的用户的渠道不能用来发系统邮件

    def test_first_enabled_admin_email_channel_is_used(self):
        self.add('dingtalk', self.admin_id, webhook_url='https://example.invalid/hook')
        disabled = self.add('email', self.admin_id, email_user='old@qq.com')
        self.db.conn.execute('UPDATE notification_channels SET enabled = 0 WHERE id = ?', (disabled,))
        self.db.conn.commit()
        self.add('email', self.admin_id, email_user='new@qq.com', smtp_from='闲鱼超级管家')
        config = self.db.get_mail_sender_config()
        self.assertEqual((config['email_user'], config['smtp_port'], config['smtp_from']),
                         ('new@qq.com', 465, '闲鱼超级管家'))

    def test_incomplete_channel_is_ignored(self):
        self.add('email', self.admin_id, email_password='')
        self.assertIsNone(self.db.get_mail_sender_config())


if __name__ == '__main__':
    unittest.main()
