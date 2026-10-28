"""Credential updates, code consumption and revocation must be one transaction."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch

import httpx
from app import auth,reply_server
from app.db_manager import DBManager
from app.services import account_credentials
from app.services.smtp_delivery import DeliveryUncertain,MailNotConfigured


class CredentialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=DBManager(str(Path(self.temp.name)/'auth.db'))
        self.owner=self.db.get_user_by_username('admin')['id']
        self.db.create_user('other','other@example.invalid','OtherPass88')
        self.patches=[patch('app.db_manager.db_manager',self.db),patch('app.auth.db_manager',self.db),
                      patch.object(reply_server,'db_manager',self.db),
                      patch.object(reply_server,'email_code_send_limiter',reply_server.email_code_send_limiter.__class__()),
                      patch.object(reply_server,'login_failure_tracker',reply_server.login_failure_tracker.__class__())]
        for p in self.patches:p.start()
        auth.SessionStore()
        self.token='synthetic-owner-session'
        self.other_token='synthetic-other-session'
        auth.SESSION_TOKENS[self.token]={'user_id':self.owner}
        auth.SESSION_TOKENS[self.other_token]={'user_id':self.owner+1}
        self.headers={'Authorization':'Bearer '+self.token}
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=reply_server.app),base_url='http://test')
        self.db.send_verification_email=AsyncMock(return_value=True)

    async def asyncTearDown(self):
        await self.client.aclose()
        for p in reversed(self.patches):p.stop()
        self.db.close();self.temp.cleanup()

    def code(self,email='next@example.invalid'):
        self.db.save_verification_code(email,'123456',f'email_change:{self.owner}')

    async def email_change(self,email='next@example.invalid',code='123456'):
        return await self.client.post('/change-email',headers=self.headers,json={
            'current_password':'admin123','new_email':email,'verification_code':code})

    async def test_password_change_http_reauth_and_other_user_isolation(self):
        response=await self.client.post('/change-password',headers=self.headers,json={'current_password':'admin123','new_password':'ChangedPass88'})
        result=response.json();self.assertTrue(result['success']);self.assertTrue(result['reauth_required'])
        self.assertEqual(result['profile']['username'],'admin')
        self.assertNotIn('password',json.dumps(result))
        self.assertEqual((await self.client.get('/account/profile',headers=self.headers)).status_code,401)
        self.assertIsNotNone(auth.SESSION_TOKENS.get(self.other_token))
        old=await self.client.post('/login',json={'username':'admin','password':'admin123'})
        new=await self.client.post('/login',json={'username':'admin','password':'ChangedPass88'})
        self.assertFalse(old.json()['success']);self.assertTrue(new.json()['success'])

    async def test_email_change_http_and_new_email_login(self):
        self.code();response=await self.email_change()
        result=response.json();self.assertTrue(result['success'])
        self.assertEqual(result['profile']['email'],'next@example.invalid')
        self.assertIsNone(auth.SESSION_TOKENS.get(self.token))
        logged=await self.client.post('/login',json={'email':'next@example.invalid','password':'admin123'})
        self.assertTrue(logged.json()['success'])
        profile=await self.client.get('/account/profile',headers={'Authorization':'Bearer '+logged.json()['token']})
        self.assertEqual(profile.json()['email'],'next@example.invalid')

    async def test_email_write_failure_rolls_back_code_and_sessions_then_same_code_works(self):
        self.code()
        self.db.conn.execute("CREATE TRIGGER fail_email BEFORE UPDATE OF email ON users BEGIN SELECT RAISE(ABORT,'fixture write failure'); END")
        self.db.conn.commit()
        before=self.db.get_user_by_id(self.owner)['email']
        failed=await self.email_change();self.assertEqual(failed.json()['error_code'],'write_failed')
        self.assertEqual(self.db.get_user_by_id(self.owner)['email'],before)
        self.assertIsNotNone(auth.SESSION_TOKENS.get(self.token))
        self.assertEqual(self.db.conn.execute("SELECT used FROM email_verifications WHERE email='next@example.invalid'").fetchone()[0],0)
        self.db.conn.execute('DROP TRIGGER fail_email');self.db.conn.commit()
        self.assertTrue((await self.email_change()).json()['success'])

    async def test_revocation_failure_rolls_back_password_and_email(self):
        self.code()
        self.db.conn.execute("CREATE TRIGGER fail_revoke BEFORE DELETE ON auth_sessions BEGIN SELECT RAISE(ABORT,'fixture revoke failure'); END")
        self.db.conn.commit()
        changed=await self.client.post('/change-password',headers=self.headers,json={'current_password':'admin123','new_password':'ChangedPass88'})
        self.assertEqual(changed.json()['error_code'],'write_failed')
        self.assertTrue(self.db.verify_user_password('admin','admin123'))
        failed=await self.email_change();self.assertEqual(failed.json()['error_code'],'write_failed')
        self.assertEqual(self.db.conn.execute("SELECT used FROM email_verifications WHERE email='next@example.invalid'").fetchone()[0],0)
        self.assertIsNotNone(auth.SESSION_TOKENS.get(self.token))

    async def test_code_wrong_limit_expiry_used_and_user_binding(self):
        self.code()
        for number in range(5):
            result=(await self.email_change(code='000000')).json()
            self.assertEqual(result['error_code'],'code_attempts_exceeded' if number==4 else 'code_wrong')
        self.assertEqual((await self.email_change()).json()['error_code'],'code_attempts_exceeded')
        self.code();self.db.conn.execute("UPDATE email_verifications SET expires_at=0 WHERE used=0");self.db.conn.commit()
        self.assertEqual((await self.email_change()).json()['error_code'],'code_expired')
        self.code();self.db.conn.execute("UPDATE email_verifications SET used=1");self.db.conn.commit()
        self.assertEqual((await self.email_change()).json()['error_code'],'code_unavailable')
        self.code();self.headers={'Authorization':'Bearer '+self.other_token}
        result=await self.client.post('/change-email',headers=self.headers,json={'current_password':'OtherPass88','new_email':'next@example.invalid','verification_code':'123456'})
        self.assertEqual(result.json()['error_code'],'code_unavailable')

    async def test_current_password_duplicate_same_email_and_password_rules(self):
        for payload,error in [({'current_password':'wrong','new_password':'ChangedPass88'},'current_password_wrong'),
                              ({'current_password':'admin123','new_password':'abc123'},'password_invalid')]:
            r=await self.client.post('/change-password',headers=self.headers,json=payload)
            self.assertEqual(r.json()['error_code'],error)
        self.code('other@example.invalid');self.assertEqual((await self.email_change('OTHER@example.invalid')).json()['error_code'],'email_in_use')
        self.code();self.assertTrue((await self.email_change()).json()['success'])
        # Direct invocation with a stale identity still uses the current database user.
        result=account_credentials.change(self.db,self.owner,'admin123',new_email='next@example.invalid',code='123456')
        self.assertEqual(result['error_code'],'email_unchanged')

    async def test_repeated_submission_cannot_consume_or_change_twice(self):
        self.code()
        first=await self.email_change();self.assertTrue(first.json()['success'])
        second=await self.email_change();self.assertEqual(second.status_code,401)
        self.assertEqual(self.db.get_user_by_id(self.owner)['email'],'next@example.invalid')

    async def test_parallel_connections_cannot_both_commit_from_old_password(self):
        import concurrent.futures
        other=DBManager(self.db.db_path)
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(account_credentials.change,manager,self.owner,'admin123',new_password=password)
                         for manager,password in [(self.db,'ParallelPass88'),(other,'DifferentPass88')]]
                results=[f.result() for f in futures]
            self.assertEqual(sum(r['success'] for r in results),1)
            self.assertEqual([r['error_code'] for r in results if not r['success']],['current_password_wrong'])
        finally:other.close()

    async def test_send_definite_failure_invalidates_only_its_code(self):
        self.db.send_verification_email.side_effect=OSError('fixture SMTP unavailable')
        result=await self.client.post('/change-email/send-code',headers=self.headers,json={'email':'next@example.invalid','current_password':'admin123'})
        self.assertEqual(result.json()['error_code'],'mail_send_failed')
        self.assertEqual(self.db.conn.execute("SELECT used FROM email_verifications WHERE email='next@example.invalid'").fetchone()[0],1)
        self.assertIsNotNone(auth.SESSION_TOKENS.get(self.token))

    async def test_send_uncertain_remains_usable_and_no_automatic_resend(self):
        self.db.send_verification_email.side_effect=DeliveryUncertain()
        result=await self.client.post('/change-email/send-code',headers=self.headers,json={'email':'next@example.invalid','current_password':'admin123'})
        self.assertEqual(result.json()['error_code'],'delivery_uncertain')
        code,used=self.db.conn.execute("SELECT code,used FROM email_verifications WHERE email='next@example.invalid'").fetchone()
        self.assertEqual(used,0)
        self.assertTrue((await self.email_change(code=code)).json()['success'])
        self.db.send_verification_email.assert_awaited_once()

    async def test_send_cooldown_and_configuration_failure(self):
        self.db.send_verification_email.side_effect=MailNotConfigured()
        payload={'email':'next@example.invalid','current_password':'admin123'}
        first=(await self.client.post('/change-email/send-code',headers=self.headers,json=payload)).json()
        self.assertEqual(first['error_code'],'mail_not_configured')
        second=(await self.client.post('/change-email/send-code',headers=self.headers,json=payload)).json()
        self.assertEqual(second['error_code'],'send_rate_limited');self.assertGreater(second['retry_after_seconds'],0)

    async def test_slow_send_and_simultaneous_wrong_password_cannot_modify_user(self):
        async def slow(*args,**kwargs):
            await asyncio.sleep(.05)
            return True
        self.db.send_verification_email.side_effect=slow
        sent=(await self.client.post('/change-email/send-code',headers=self.headers,json={'email':'next@example.invalid','current_password':'admin123'})).json()
        self.assertTrue(sent['success']);self.assertEqual(sent['retry_after_seconds'],60)
        self.assertIn('request_id',sent)
