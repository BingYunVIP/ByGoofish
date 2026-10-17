# -*- coding: utf-8 -*-
"""账号 AI 设置与系统默认配置的合并：默认地址、旧默认值、系统密钥不外泄。"""

import os
import tempfile
import unittest

from app.db_manager import DBManager

DEFAULT_URL = 'https://api.bingyun.vip/v1'


class AISettingsDefaultsTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.db = DBManager(os.path.join(self._temp.name, 'ai.db'))
        user = self.db.get_user_by_username('admin')['id']
        self.db.save_cookie('acc', 'unb=acc; cookie2=fixture', user)

    def tearDown(self):
        self.db.conn.close()
        self._temp.cleanup()

    def system(self, **values):
        for key, value in values.items():
            self.db.set_system_setting(key, value)

    def save(self, **settings):
        return self.db.save_ai_reply_settings('acc', {'ai_enabled': True, **settings})

    def test_no_row_uses_the_built_in_default_address(self):
        effective = self.db.get_ai_reply_settings('acc')
        self.assertEqual(effective['base_url'], DEFAULT_URL)
        self.assertFalse(effective['ai_enabled'])

    def test_saved_address_is_normalized_and_blank_means_default(self):
        self.save(base_url='api.example.com')
        self.assertEqual(self.db.get_ai_reply_settings('acc')['base_url'], 'https://api.example.com/v1')
        self.save(base_url='')
        self.assertEqual(self.db.get_ai_reply_settings('acc')['base_url'], DEFAULT_URL)

    def test_legacy_default_address_is_treated_as_unconfigured(self):
        self.system(ai_api_url='https://relay.example.com/v1', ai_api_key='system-key', ai_model='sys-model')
        self.db.conn.execute(
            "INSERT OR REPLACE INTO ai_reply_settings (cookie_id, ai_enabled, model_name, api_key, base_url) "
            "VALUES ('acc', 1, 'qwen-plus', '', 'https://ai.corleom.com/v1')")
        self.db.conn.commit()
        effective = self.db.get_ai_reply_settings('acc')
        self.assertEqual((effective['base_url'], effective['model_name'], effective['api_key']),
                         ('https://relay.example.com/v1', 'sys-model', 'system-key'))

    def test_account_inherits_system_key_only_for_the_system_address(self):
        self.system(ai_api_url='https://relay.example.com/v1', ai_api_key='system-key')
        self.save(base_url='', api_key='')
        self.assertEqual(self.db.get_ai_reply_settings('acc')['api_key'], 'system-key')
        self.save(base_url='https://relay.example.com', api_key='')  # 与系统地址相同（补全 /v1 后）
        self.assertEqual(self.db.get_ai_reply_settings('acc')['api_key'], 'system-key')

    def test_system_key_is_never_sent_to_a_custom_address(self):
        self.system(ai_api_url='https://relay.example.com/v1', ai_api_key='system-key')
        self.save(base_url='https://elsewhere.example.net/v1', api_key='')
        effective = self.db.get_ai_reply_settings('acc')
        self.assertEqual(effective['base_url'], 'https://elsewhere.example.net/v1')
        self.assertEqual(effective['api_key'], '')

    def test_account_own_key_works_with_any_address(self):
        self.system(ai_api_url='https://relay.example.com/v1', ai_api_key='system-key')
        self.save(base_url='https://elsewhere.example.net/v1', api_key='own-key')
        self.assertEqual(self.db.get_ai_reply_settings('acc')['api_key'], 'own-key')


if __name__ == '__main__':
    unittest.main()
