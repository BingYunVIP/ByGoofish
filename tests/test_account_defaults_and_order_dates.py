"""新增账号缺省值和订单筛选日期边界回归，使用独立 SQLite。"""
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import httpx
from app.db_manager import DBManager
from app import reply_server
from app.auth import get_current_user


class AccountDefaultsAndOrderDateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DBManager(str(Path(self.tmp.name) / 'fixture.db'))
        self.user_id = self.db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        self.db.save_cookie('date-account', 'fixture=value', self.user_id)
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

    async def test_new_account_defaults_and_unknown_account_safety(self):
        self.assertEqual(self.db.get_system_setting('registration_enabled'), 'false')
        self.assertEqual(self.db.get_system_setting('show_default_login_info'), 'false')
        flags = self.db.get_buyer_interaction_settings('date-account')
        self.assertTrue(flags['auto_rate_enabled'])
        self.assertTrue(flags['auto_flower_enabled'])
        self.assertTrue(flags['auto_thanks_enabled'])
        self.assertFalse(flags['auto_receive_flower_enabled'])
        self.assertFalse(any(self.db.get_buyer_interaction_settings('missing').values()))
        self.assertEqual(self.db.get_system_setting('auto_polish_enabled'), 'true')

    async def test_public_access_defaults_are_closed_when_settings_are_missing(self):
        self.db.conn.execute(
            "DELETE FROM system_settings WHERE key IN ('registration_enabled', 'show_default_login_info')"
        )
        self.db.conn.commit()
        self.assertFalse(reply_server.get_registration_status()['enabled'])
        self.assertFalse(reply_server.get_login_info_status()['enabled'])
        public = reply_server.get_public_system_settings()
        self.assertEqual(public['registration_enabled'], 'false')
        self.assertEqual(public['show_default_login_info'], 'false')

    async def test_saved_switches_survive_cookie_refresh_and_adding_account(self):
        self.db.update_buyer_interaction_settings('date-account', auto_rate_enabled=False, auto_thanks_enabled=False)
        self.db.set_system_setting('auto_polish_enabled', 'false')
        self.db.set_system_setting('item_sync_enabled', 'false')
        self.assertTrue(self.db.save_cookie('date-account', 'fixture=updated', self.user_id))
        self.db.save_cookie('new-account', 'fixture=new', self.user_id)
        self.assertFalse(self.db.get_buyer_interaction_settings('date-account')['auto_rate_enabled'])
        self.assertFalse(self.db.get_buyer_interaction_settings('date-account')['auto_thanks_enabled'])
        self.assertTrue(self.db.get_buyer_interaction_settings('new-account')['auto_rate_enabled'])
        self.assertEqual(self.db.get_system_setting('auto_polish_enabled'), 'false')
        self.assertEqual(self.db.get_system_setting('item_sync_enabled'), 'false')

    async def test_cross_user_update_cannot_reset_defaults_or_cookie(self):
        other = self.db.conn.execute("INSERT INTO users(username,email,password_hash) VALUES ('date-other','date@example.test','fixture')").lastrowid
        self.db.conn.commit()
        self.assertFalse(self.db.save_cookie('date-account', 'fixture=attacker', other))
        self.assertEqual(self.db.get_cookie('date-account'), 'fixture=value')

    async def test_chat_account_prefers_remark_and_updates_after_edit(self):
        self.db.conn.execute("UPDATE cookies SET nickname='平台昵称',remark='  自定备注  ' WHERE id='date-account'")
        self.db.conn.commit()
        account = (await self.client.get('/chat/accounts')).json()['data'][0]
        self.assertEqual(account['displayName'], '平台昵称（自定备注）')
        self.assertEqual(account['nickname'], '平台昵称')
        self.assertEqual(account['remark'], '自定备注')
        with patch.object(reply_server.cookie_manager, 'manager', SimpleNamespace(instances={})):
            response = await self.client.put('/cookies/date-account/remark', json={'remark': '新备注'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await self.client.get('/chat/accounts')).json()['data'][0]['displayName'], '平台昵称（新备注）')

    async def test_chat_account_falls_back_when_remark_is_blank(self):
        self.db.conn.execute("UPDATE cookies SET nickname='平台昵称',remark='   ' WHERE id='date-account'")
        self.db.conn.commit()
        self.assertEqual((await self.client.get('/chat/accounts')).json()['data'][0]['displayName'], '平台昵称')
        self.db.conn.execute("UPDATE cookies SET nickname='' WHERE id='date-account'")
        self.db.conn.commit()
        self.assertEqual((await self.client.get('/chat/accounts')).json()['data'][0]['displayName'], 'date-account')

    async def test_order_boundaries_counts_and_pagination_share_range(self):
        rows = [
            ('before', 'completed', '2026-10-18 15:59:59'),
            ('start', 'completed', '2026-10-18 16:00:00'),
            ('middle', 'pending_ship', '2026-10-19 12:00:00'),
            ('end', 'completed', '2026-10-19 16:00:00'),
        ]
        self.db.conn.executemany("INSERT INTO orders(order_id,cookie_id,order_status,created_at) VALUES (?, 'date-account', ?, ?)", rows)
        self.db.conn.commit()
        params = {'start_time': '2026-10-19T00:00:00+08:00', 'end_time': '2026-10-20T00:00:00+08:00', 'page_size': 1}
        response = await self.client.get('/api/orders', params=params)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body['total'], body['total_pages']), (2, 2))
        self.assertEqual(body['data'][0]['order_id'], 'middle')
        self.assertEqual(body['status_counts'], {'all': 2, 'completed': 1, 'pending_ship': 1})
        body = (await self.client.get('/api/orders', params={**params, 'status': 'completed'})).json()
        self.assertEqual(body['total'], 1)
        self.assertEqual(body['status_counts']['all'], 2)
        self.assertEqual(body['data'][0]['order_id'], 'start')

    async def test_invalid_order_range_is_rejected(self):
        response = await self.client.get('/api/orders', params={'start_time': '2026-10-20', 'end_time': '2026-10-19'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual((await self.client.get('/api/orders', params={'start_time': 'invalid'})).status_code, 422)

    async def test_no_hidden_thousand_order_cap(self):
        self.db.conn.executemany("INSERT INTO orders(order_id,cookie_id,created_at) VALUES (?, 'date-account', '2026-10-20 00:00:00')",
                                 [(f'fixture-{i}',) for i in range(1001)])
        self.db.conn.commit()
        response = await self.client.get('/api/orders', params={'page': 51})
        self.assertEqual(response.json()['total'], 1001)
        self.assertEqual(len(response.json()['data']), 1)


if __name__ == '__main__':
    unittest.main()
