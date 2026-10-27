# -*- coding: utf-8 -*-
"""已删除功能留下的系统设置清理：公告设置一律删除，系统 SMTP 只删除从未填写的默认项。"""

import sqlite3
import unittest

from app.db_manager import DBManager


def settings_db(rows):
    conn = sqlite3.connect(':memory:')
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE system_settings (key TEXT PRIMARY KEY, value TEXT, description TEXT)')
    cursor.executemany('INSERT INTO system_settings VALUES (?, ?, ?)', [(k, v, '') for k, v in rows])
    return conn, cursor


class SettingsCleanupTests(unittest.TestCase):
    def keys(self, cursor):
        return sorted(row[0] for row in cursor.execute('SELECT key FROM system_settings'))

    def test_announcement_settings_are_removed_but_lookalikes_are_kept(self):
        conn, cursor = settings_db([('announcement_enabled', 'true'), ('announcement_source_url', 'x'),
                                    ('announcementX', 'keep'), ('registration_enabled', 'false')])
        DBManager._cleanup_removed_settings(cursor)
        self.assertEqual(self.keys(cursor), ['announcementX', 'registration_enabled'])

    def test_never_configured_system_smtp_is_removed(self):
        conn, cursor = settings_db([('smtp_server', ''), ('smtp_password', ''), ('smtp_port', '587'),
                                    ('smtp_from', ''), ('smtp_use_tls', 'true')])
        DBManager._cleanup_removed_settings(cursor)
        self.assertEqual(self.keys(cursor), [])

    def test_configured_system_smtp_is_never_silently_destroyed(self):
        conn, cursor = settings_db([('smtp_server', 'smtp.qq.com'), ('smtp_password', 'code'), ('smtp_port', '465')])
        DBManager._cleanup_removed_settings(cursor)
        self.assertEqual(self.keys(cursor), ['smtp_password', 'smtp_port', 'smtp_server'])

    def test_cleanup_is_idempotent(self):
        conn, cursor = settings_db([('announcement_enabled', 'true'), ('smtp_server', ''), ('smtp_password', '')])
        DBManager._cleanup_removed_settings(cursor)
        DBManager._cleanup_removed_settings(cursor)
        self.assertEqual(self.keys(cursor), [])


if __name__ == '__main__':
    unittest.main()
