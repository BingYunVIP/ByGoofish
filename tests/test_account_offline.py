import asyncio
import json
from pathlib import Path
import smtplib
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from XianyuAutoAsync import XianyuLive, ConnectionState
from app.db_manager import DBManager
from app.services import account_offline as offline, auth_recovery
from app.services.smtp_delivery import DeliveryUncertain


class AccountOfflineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.tmp.name)/'offline.db')
        self.db=DBManager(self.path)
        self.owner=self.db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        self.db.save_cookie('offline-fixture','unb=fixture',self.owner)
        self.channel=self.db.create_notification_channel('fixture','email',json.dumps({
            'smtp_server':'smtp.example.test','smtp_port':465,'email_user':'sender@example.test',
            'email_password':'fixture','recipient_email':'recipient@example.test'}),self.owner)
        self.rule=self.db.create_notification_rule('offline-fixture',self.channel,event_types=['account_offline'])
        self.manager=SimpleNamespace(instances={})
        self.now=time.time()
        self.db.conn.execute("INSERT INTO account_offline_incidents VALUES(?,?,?,?,NULL)",('offline-fixture','fixture-outage',self.now,'message_connection_lost'))
        self.db.conn.commit()
        self.email_patch=patch.object(XianyuLive,'_send_email_notification',new=AsyncMock(return_value=True))
        self.email=self.email_patch.start()

    async def asyncTearDown(self):
        self.email_patch.stop();self.db.close();self.tmp.cleanup()

    def require(self):
        return None

    async def check(self,seconds=0):
        return await offline.check_once(self.db,self.manager,self.now+seconds)

    async def test_persistent_offline_incident_notifies_once_and_survives_restart(self):
        self.assertEqual(await self.check(91),1)
        self.assertEqual(await self.check(391),0)
        self.db.close();self.db=DBManager(self.path)
        self.assertEqual(await self.check(691),0)
        self.assertEqual(self.email.await_count,1)
        self.email.assert_awaited_once()
        self.assertTrue(self.email.await_args.kwargs['strict'])

    async def test_transient_disconnect_has_grace(self):
        self.assertEqual(await self.check(),0)
        self.assertEqual(await self.check(89),0)
        self.assertEqual(await self.check(91),1)

    async def test_confirmed_connection_closes_incident_then_new_outage_notifies(self):
        await self.check(91)
        self.manager.instances['offline-fixture']=SimpleNamespace(connection_state=ConnectionState.CONNECTED,
            ws=SimpleNamespace(closed=False),last_heartbeat_response=self.now+92)
        await self.check(92)
        self.assertIsNotNone(self.db.conn.execute('SELECT resolved_at FROM account_offline_incidents').fetchone()[0])
        self.manager.instances.clear()
        await self.check(93)
        self.assertEqual(await self.check(184),1)
        self.assertEqual(self.email.await_count,2)

    async def test_running_task_stale_heartbeat_is_offline(self):
        self.manager.instances['offline-fixture']=SimpleNamespace(connection_state=ConnectionState.CONNECTED,
            ws=SimpleNamespace(closed=False),last_heartbeat_response=self.now-100)
        self.assertEqual(await self.check(91),1)

    async def test_disabled_account_empty_subscription_and_disabled_channel_are_silent(self):
        self.db.save_cookie_status('offline-fixture',False)
        await self.check();self.email.assert_not_awaited()
        self.db.save_cookie_status('offline-fixture',True)
        self.db.update_notification_rule(self.rule,event_types=[])
        await self.check(391);self.email.assert_not_awaited()
        self.db.update_notification_rule(self.rule,event_types=['account_offline'])
        self.db.conn.execute('UPDATE notification_channels SET enabled=0');self.db.conn.commit()
        await self.check(691);self.email.assert_not_awaited()

    async def test_definite_auth_rejection_has_bounded_backoff(self):
        self.email.side_effect=smtplib.SMTPAuthenticationError(535,b'fixture')
        await self.check(91);await self.check(121)
        self.assertEqual(self.email.await_count,1)
        await self.check(392);await self.check(1000);await self.check(9999)
        self.assertEqual(self.email.await_count,3)
        self.assertEqual(self.db.conn.execute('SELECT status FROM account_offline_notices').fetchone()[0],'failed')

    async def test_unknown_submission_is_not_retried(self):
        self.email.side_effect=DeliveryUncertain('fixture')
        await self.check(91);await self.check(9999)
        self.assertEqual(self.email.await_count,1)
        self.assertEqual(self.db.conn.execute('SELECT status FROM account_offline_notices').fetchone()[0],'unknown')

    async def test_inflight_restart_claim_becomes_unknown(self):
        self.db.conn.execute('INSERT OR REPLACE INTO account_offline_incidents VALUES(?,?,?,?,NULL)',('offline-fixture','incident',self.now,'message_verification_required'))
        self.db.conn.execute('INSERT INTO account_offline_notices VALUES(?,?,?,?,?,?,?)',('offline-fixture','incident',self.rule,'sending',1,0,self.now));self.db.conn.commit()
        offline.initialize(self.db)
        await self.check(999)
        self.email.assert_not_awaited()

    async def test_notice_owned_by_same_user_as_account(self):
        self.db.conn.execute("INSERT INTO users(username,email,password_hash) VALUES('other','other@example.test','fixture')")
        other=self.db.conn.execute("SELECT id FROM users WHERE username='other'").fetchone()[0]
        self.db.conn.execute('UPDATE notification_channels SET user_id=?',(other,));self.db.conn.commit()
        await self.check(91);self.email.assert_not_awaited()

    async def test_verified_auth_issue_never_sends_duplicate_offline_notice(self):
        auth_recovery.set_state(self.db,'offline-fixture','required','message_verification_required')
        self.manager.instances['offline-fixture']=SimpleNamespace(connection_state=ConnectionState.CONNECTED,
            ws=SimpleNamespace(closed=False),last_heartbeat_response=self.now)
        self.assertEqual(await self.check(300),0)
        self.email.assert_not_awaited()

    async def test_no_manager_does_not_create_startup_outage(self):
        self.assertEqual(await offline.check_once(self.db,None,self.now),0)
        self.assertEqual(self.db.conn.execute('SELECT count(*) FROM account_offline_notices').fetchone()[0],0)

    async def test_required_verification_is_offline_even_with_old_open_socket(self):
        auth_recovery.set_state(self.db,'offline-fixture','required','message_verification_required')
        self.manager.instances['offline-fixture']=SimpleNamespace(connection_state=ConnectionState.CONNECTED,
            ws=SimpleNamespace(closed=False),last_heartbeat_response=self.now)
        self.assertEqual(await self.check(),0)
        self.email.assert_not_awaited()
