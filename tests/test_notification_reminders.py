"""All event reminders: settings, durable budgets, lifecycle and current authorization."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.db_manager import DBManager
from app.notification_events import EVENT_IDS
from app.services import event_journal as journal
from XianyuAutoAsync import XianyuLive


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'reminders.db')
        self.db=DBManager(self.path);self.uid=self.db.get_user_by_username('admin')['id']
        self.db.save_cookie('reminder-fixture','unb=fixture',self.uid)
        self.channel=self.db.create_notification_channel('fixture','email','{}',self.uid)
        self.rule=self.db.create_notification_rule('reminder-fixture',self.channel,event_types=EVENT_IDS)
        self.live=XianyuLive.__new__(XianyuLive);self.live.cookie_id='reminder-fixture'
        self.email=AsyncMock(return_value=True)
        self.patch=patch.object(XianyuLive,'_send_email_notification',self.email);self.patch.start()
    async def asyncTearDown(self):
        self.patch.stop();self.db.close();self.tmp.cleanup()
    def last(self):
        return self.db.conn.execute('SELECT n.updated_at FROM operational_notices n JOIN operational_incidents i ON i.id=n.incident_id WHERE i.resolved_at IS NULL ORDER BY n.updated_at DESC LIMIT 1').fetchone()[0]
    async def emit(self,event='credentials_invalid',**kwargs):
        return await journal.dispatch(self.db,self.live,event,'fixture issue',**kwargs)

    async def test_every_catalog_event_defaults_to_two_five_minutes_apart(self):
        self.assertEqual(self.db.get_notification_rule(self.rule,self.uid)['reminder_limit'],2)
        self.assertEqual(self.db.get_all_message_notifications(self.uid)['reminder-fixture'][0]['reminder_interval_minutes'],5)
        for event in EVENT_IDS:
            before=self.email.await_count
            await self.emit(event,context='fixture-'+event)
            end=self.last()
            await journal.process_due(self.db,now=end+299)
            self.assertEqual(self.email.await_count,before+1,event)
            await journal.process_due(self.db,now=end+301)
            self.assertEqual(self.email.await_count,before+2,event)
            await journal.process_due(self.db,now=end+100000)
            self.assertEqual(self.email.await_count,before+2,event)
            journal.resolve(self.db,'reminder-fixture',(event,))

    async def test_concurrency_restart_and_changing_error_context_do_not_reset(self):
        await asyncio.gather(*(self.emit(context=str(i)) for i in range(8)))
        end=self.last();self.assertEqual(self.email.await_count,1)
        self.db.close();self.db=DBManager(self.path)
        await asyncio.gather(*(journal.process_due(self.db,now=end+301) for _ in range(8)))
        self.assertEqual(self.email.await_count,2)
        await self.emit(context='another-error');await journal.process_due(self.db,now=end+99999)
        self.assertEqual(self.email.await_count,2)
        self.assertEqual(self.db.conn.execute('SELECT count(*) FROM operational_incidents').fetchone()[0],1)

    async def test_real_recovery_starts_new_budget_but_acknowledgement_does_not(self):
        await self.emit();log_id=self.db.conn.execute('SELECT log_id FROM operational_incidents').fetchone()[0]
        self.assertFalse(journal.acknowledge(self.db,self.uid+999,log_id))
        self.assertTrue(journal.acknowledge(self.db,self.uid,log_id));self.assertTrue(journal.acknowledge(self.db,self.uid,log_id))
        await journal.process_due(self.db,now=self.last()+5000);await self.emit(context='changed')
        self.assertEqual(self.email.await_count,1)
        journal.resolve(self.db,'reminder-fixture',('credentials_invalid',))
        await self.emit();self.assertEqual(self.email.await_count,2)

    async def test_disabled_rule_channel_account_empty_subscription_and_foreign_owner_stop_due(self):
        await self.emit();end=self.last()
        for change in ('rule','channel','account','subscription','owner'):
            if change=='rule':self.db.update_notification_rule(self.rule,event_types=EVENT_IDS,enabled=False)
            if change=='channel':self.db.conn.execute('UPDATE notification_channels SET enabled=0');self.db.conn.commit()
            if change=='account':self.db.save_cookie_status('reminder-fixture',False)
            if change=='subscription':self.db.update_notification_rule(self.rule,event_types=[])
            if change=='owner':self.db.conn.execute('UPDATE notification_channels SET user_id=?',(self.uid+99,));self.db.conn.commit()
            await journal.process_due(self.db,now=end+301);self.assertEqual(self.email.await_count,1,change)
            self.db.update_notification_rule(self.rule,event_types=EVENT_IDS,enabled=True)
            self.db.save_cookie_status('reminder-fixture',True)
            self.db.conn.execute('UPDATE notification_channels SET enabled=1,user_id=?',(self.uid,));self.db.conn.commit()

    async def test_unknown_never_retries_and_definite_failure_consumes_budget(self):
        from app.services.smtp_delivery import DeliveryUncertain
        self.email.side_effect=DeliveryUncertain('fixture')
        await self.emit();await journal.process_due(self.db,now=self.last()+9999)
        self.assertEqual(self.email.await_count,1)
        journal.resolve(self.db,'reminder-fixture',('credentials_invalid',))
        self.email.side_effect=RuntimeError('definite failure')
        await self.emit();await journal.process_due(self.db,now=self.last()+301)
        await journal.process_due(self.db,now=self.last()+99999)
        self.assertEqual(self.email.await_count,3)

    async def test_settings_take_effect_without_reset_and_reject_invalid_values(self):
        await self.emit();end=self.last()
        self.db.update_notification_rule(self.rule,event_types=EVENT_IDS,reminder_limit=1)
        await journal.process_due(self.db,now=end+5000);self.assertEqual(self.email.await_count,1)
        self.db.update_notification_rule(self.rule,event_types=EVENT_IDS,reminder_limit=3,reminder_interval_minutes=10)
        await journal.process_due(self.db,now=end+301);self.assertEqual(self.email.await_count,1)
        await journal.process_due(self.db,now=end+601);self.assertEqual(self.email.await_count,2)
        for limit,interval in ((0,5),(11,5),(2,1),(2,1441),(True,5)):
            with self.assertRaises(ValueError):self.db.create_notification_rule('reminder-fixture',self.channel,reminder_limit=limit,reminder_interval_minutes=interval)

    async def test_order_and_chat_resolution_and_safe_buyer_summary(self):
        await self.emit('buyer_message',context='message-a',values={'chat_id':'chat-a'},reminder_detail='new buyer message')
        await self.emit('buyer_message',context='message-b',values={'chat_id':'chat-b'},reminder_detail='new buyer message')
        journal.resolve_chat(self.db,'reminder-fixture','chat-a',self.last()+10)
        await journal.process_due(self.db,now=self.last()+301);self.assertEqual(self.email.await_count,3)
        await self.emit('delivery_failed',context='order-a',values={'order_id':'order-a'})
        await self.emit('delivery_failed',context='order-b',values={'order_id':'order-b'})
        journal.resolve_order(self.db,'reminder-fixture','order-a','shipped')
        await journal.process_due(self.db,now=self.last()+301);self.assertEqual(self.email.await_count,6)

    async def test_initialization_state_and_historical_events_do_not_generate_extra_reminders(self):
        from app.services import auth_recovery
        auth_recovery.set_state(self.db,'reminder-fixture','required','message_verification_required')
        await self.emit('message_verification_required')
        end=self.last()
        auth_recovery.set_state(self.db,'reminder-fixture','none')
        await self.emit('message_verification_required',context='fresh-cookie-attempt')
        self.assertEqual(self.email.await_count,1)
        self.db.conn.execute('UPDATE operational_incidents SET reminders_enabled=0');self.db.conn.commit()
        await journal.process_due(self.db,now=end+99999)
        self.assertEqual(self.email.await_count,1)

    async def test_two_rules_and_accounts_have_separate_budgets(self):
        self.db.create_notification_rule('reminder-fixture',self.channel,event_types=['credentials_invalid'],reminder_limit=1)
        await self.emit();self.assertEqual(self.email.await_count,2)
        await journal.process_due(self.db,now=self.last()+301);self.assertEqual(self.email.await_count,3)
        self.db.save_cookie('second-fixture','unb=fixture',self.uid)
        self.db.create_notification_rule('second-fixture',self.channel,event_types=['credentials_invalid'])
        second=XianyuLive.__new__(XianyuLive);second.cookie_id='second-fixture'
        await journal.dispatch(self.db,second,'credentials_invalid','fixture')
        self.assertEqual(self.email.await_count,4)

    async def test_old_rule_defaults_migrate_once_and_preserve_notice_budget(self):
        await self.emit()
        self.db.conn.execute('ALTER TABLE message_notifications DROP COLUMN reminder_limit')
        self.db.conn.execute('ALTER TABLE message_notifications DROP COLUMN reminder_interval_minutes')
        self.db.conn.commit()
        journal.initialize(self.db)
        rule=self.db.get_notification_rule(self.rule)
        self.assertEqual((rule['reminder_limit'],rule['reminder_interval_minutes']),(2,5))
        self.assertEqual(self.db.conn.execute('SELECT attempts FROM operational_notices').fetchone()[0],1)
        self.db.update_notification_rule(self.rule,event_types=EVENT_IDS,reminder_limit=3,reminder_interval_minutes=9)
        journal.initialize(self.db)
        rule=self.db.get_notification_rule(self.rule)
        self.assertEqual((rule['reminder_limit'],rule['reminder_interval_minutes']),(3,9))

    async def test_interrupted_send_becomes_unknown_after_restart_and_never_repeats(self):
        await self.emit()
        self.db.conn.execute("UPDATE operational_notices SET status='sending'")
        self.db.conn.commit()
        self.db.close();self.db=DBManager(self.path)
        await journal.process_due(self.db,now=self.last()+99999)
        self.assertEqual(self.email.await_count,1)
        self.assertEqual(self.db.conn.execute('SELECT status FROM operational_notices').fetchone()[0],'unknown')

    async def test_api_validation_and_owned_acknowledgement(self):
        from app import reply_server as server
        from pydantic import ValidationError
        from fastapi.testclient import TestClient
        for field,value in (('reminder_limit',0),('reminder_limit',True),('reminder_interval_minutes',1)):
            with self.assertRaises(ValidationError):server.MessageNotificationIn(channel_id=self.channel,**{field:value})
        with patch.object(server,'db_manager',self.db), patch('app.db_manager.db_manager',self.db):
            server.app.dependency_overrides[server.get_current_user]=lambda:{'user_id':self.uid}
            try:
                with TestClient(server.app) as client:
                    r=client.put(f'/message-notifications/rule/{self.rule}',json={'reminder_limit':3,'reminder_interval_minutes':7})
                    self.assertEqual(r.status_code,200)
                    self.assertEqual(self.db.get_notification_rule(self.rule)['reminder_limit'],3)
                    await self.emit();log=self.db.conn.execute('SELECT log_id FROM operational_incidents').fetchone()[0]
                    self.assertEqual(client.post(f'/notification-incidents/{log}/acknowledge').status_code,200)
                    self.assertEqual(client.post('/notification-incidents/999999/acknowledge').status_code,404)
            finally:server.app.dependency_overrides.clear()
