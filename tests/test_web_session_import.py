import json
from pathlib import Path
import shlex
import tempfile
import unittest
from urllib.parse import urlencode
from unittest.mock import Mock, patch
from types import SimpleNamespace

from app.services.web_session_import import parse_request, SessionImportError
from app.services import auth_recovery
from app.db_manager import DBManager
from app import reply_server as server
from fastapi import HTTPException

USER='web-fixture'
DEVICE='12345678-1234-4234-8234-123456789abc-'+USER
COOKIE='unb='+USER+'; cookie2=fixture; _m_h5_tk=fixture_1; _m_h5_tk_enc=fixture-enc; x5sec=verified-fixture'


def request(cookie=COOKIE, device=DEVICE, url='https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/?t=1&sign=fixture'):
    return 'curl '+shlex.quote(url)+' -b '+shlex.quote(cookie)+' --data-raw '+shlex.quote(urlencode({'data':json.dumps({'appKey':'444e9908a51d1cb236a27862abc769c9','deviceId':device})}))


class ParseWebSessionTests(unittest.TestCase):
    def test_chrome_cookie_and_encoded_data_are_extracted(self):
        cookie,device,count=parse_request(request(),USER)
        self.assertEqual(cookie,COOKIE);self.assertEqual(device,DEVICE);self.assertEqual(count,5)
    def test_cookie_header_and_continuations(self):
        text=request().replace('-b '+shlex.quote(COOKIE),'-H '+shlex.quote('Cookie: '+COOKIE)).replace(' --data-raw',' \\\n --data-raw')
        self.assertEqual(parse_request(text,USER)[0],COOKIE)
    def test_other_official_h5_request_uses_existing_device(self):
        text='curl https://h5api.m.goofish.com/h5/mtop.idle.web.user.page.head/1.0/ -b '+shlex.quote(COOKIE)
        self.assertEqual(parse_request(text,USER)[1],'')
    def test_foreign_domain_user_credentials_and_device_are_rejected(self):
        for text in [request(url='https://attacker.example/h5/api/1.0/'),request(cookie=COOKIE.replace(USER,'other')),
                     request(device=DEVICE.replace(USER,'other')),request(cookie='unb='+USER),request(device=''),request(cookie=COOKIE+'\r\nX-Fixture: invalid')]:
            with self.subTest(),self.assertRaises(SessionImportError):parse_request(text,USER)
    def test_pasted_data_is_never_executed_or_echoed_in_error(self):
        with patch('subprocess.run') as command:
            with self.assertRaises(SessionImportError) as e:parse_request("curl 'unterminated $(sensitive-command)",USER)
            self.assertNotIn('sensitive-command',str(e.exception));command.assert_not_called()
    def test_empty_large_or_response_content_is_rejected(self):
        for text in ('', 'x'*60001,'{"data":{"accessToken":"fixture"}}'):
            with self.assertRaises(SessionImportError):parse_request(text,USER)


class WebSessionRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=DBManager(str(Path(self.tmp.name)/'web.db'))
        self.owner=self.db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        self.db.save_cookie('account-fixture','unb='+USER+'; cookie2=old-fixture',self.owner)
        self.user={'user_id':self.owner,'username':'fixture'}
        auth_recovery.set_state(self.db,'account-fixture','required','challenge_page_error')
        self.patches=[patch('app.db_manager.db_manager',self.db),patch.object(server,'db_manager',self.db)]
        for p in self.patches:p.start()
    async def asyncTearDown(self):
        for p in reversed(self.patches):p.stop()
        self.db.close();self.tmp.cleanup()
    async def test_saves_same_account_device_and_checks_once_without_ready_claim(self):
        manager=SimpleNamespace(get_cookie_status=lambda _:True,update_cookie=Mock())
        original=self.db.get_cookie_details('account-fixture')
        with patch.object(server.cookie_manager,'manager',manager):
            result=await server.import_verified_web_session('account-fixture',request(),self.user)
        self.assertTrue(result['success']);self.assertEqual(result['recovery_state'],'checking')
        self.assertEqual(self.db.get_cookie('account-fixture'),COOKIE)
        self.assertEqual(self.db.conn.execute('SELECT device_id FROM account_auth_clients').fetchone()[0],DEVICE)
        manager.update_cookie.assert_called_once_with('account-fixture',COOKIE,save_to_db=False)
        self.assertEqual(self.db.get_cookie_details('account-fixture')['auto_confirm'],original['auto_confirm'])
        self.assertNotIn('cookie',result);self.assertNotIn('device_id',result)
    async def test_other_user_cannot_import(self):
        with self.assertRaises(HTTPException) as e:
            await server.import_verified_web_session('account-fixture',request(),{'user_id':self.owner+1})
        self.assertEqual(e.exception.status_code,404)
    async def test_wrong_account_preserves_credentials(self):
        old=self.db.get_cookie('account-fixture')
        with self.assertRaises(HTTPException):await server.import_verified_web_session('account-fixture',request(cookie=COOKIE.replace(USER,'other')),self.user)
        self.assertEqual(self.db.get_cookie('account-fixture'),old)
    async def test_disabled_account_saves_without_authentication(self):
        self.db.save_cookie_status('account-fixture',False)
        manager=SimpleNamespace(get_cookie_status=lambda _:False,update_cookie=Mock())
        with patch.object(server.cookie_manager,'manager',manager):
            result=await server.import_verified_web_session('account-fixture',request(),self.user)
        self.assertEqual(result['recovery_state'],'required');manager.update_cookie.assert_not_called()

    async def test_old_instance_is_paused_until_imported_task_replaces_it(self):
        previous=SimpleNamespace(manual_captcha_in_progress=False)
        def update(*args,**kwargs):
            self.assertTrue(previous.manual_captcha_in_progress)
            self.assertFalse(auth_recovery.get(self.db,'account-fixture')['token_claimed'])
        manager=SimpleNamespace(instances={'account-fixture':previous},get_cookie_status=lambda _:True,update_cookie=Mock(side_effect=update))
        with patch.object(server.cookie_manager,'manager',manager):
            await server.import_verified_web_session('account-fixture',request(),self.user)
        self.assertFalse(previous.manual_captcha_in_progress)

    async def test_task_update_failure_keeps_new_credentials_and_pauses_auth(self):
        manager=SimpleNamespace(get_cookie_status=lambda _:True,update_cookie=Mock(side_effect=RuntimeError('fixture')))
        with patch.object(server.cookie_manager,'manager',manager),self.assertRaises(HTTPException) as e:
            await server.import_verified_web_session('account-fixture',request(),self.user)
        self.assertEqual(e.exception.status_code,503)
        self.assertEqual(self.db.get_cookie('account-fixture'),COOKIE)
        self.assertEqual(auth_recovery.get(self.db,'account-fixture')['failure_category'],'connection_failed')
