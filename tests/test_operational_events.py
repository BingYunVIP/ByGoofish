"""Catalog, auth-stage evidence and durable notification behavior."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.db_manager import DBManager
from app.notification_events import EVENT_IDS, get_event_definitions, normalize_event_types, resolve_event_type
from app.services import auth_recovery, event_journal
from app.services.notification_templates import DEFAULT_TEMPLATE, notification_plain_text, render_account_notification
from XianyuAutoAsync import XianyuLive


class OperationalEventsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.temp.name)/'events.db')
        self.db=DBManager(self.path)
        self.owner=self.db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        self.db.save_cookie('event-fixture','unb=fixture; cookie2=fixture',self.owner)
        self.channel=self.db.create_notification_channel('fixture','email',json.dumps({
            'smtp_server':'smtp.example.test','smtp_port':465,'email_user':'sender@example.test',
            'email_password':'fixture','recipient_email':'recipient@example.test'}),self.owner)
        self.rule=self.db.create_notification_rule('event-fixture',self.channel,event_types=EVENT_IDS)
        self.live=XianyuLive.__new__(XianyuLive)
        self.live.cookie_id='event-fixture'
        self.live._send_email_notification=AsyncMock(return_value=True)

    async def asyncTearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_catalog_has_complete_metadata_and_no_legacy_ids(self):
        self.assertEqual(len(EVENT_IDS),len(set(EVENT_IDS)))
        for definition in get_event_definitions():
            for field in ('severity','description','impact','action','actions','actionable','default_subscribed'):
                self.assertIn(field,definition)
            self.assertIn(definition['severity'],('error','warning','info'))
            if definition['severity']!='error':
                self.assertFalse(definition['default_subscribed'])
        for obsolete in ('captcha_manual','cookie_invalid','login_success','platform_challenge'):
            with self.assertRaises(ValueError): normalize_event_types([obsolete])
            with self.assertRaises(ValueError): resolve_event_type(obsolete)

    def test_default_and_explicit_empty_subscriptions(self):
        defaults=normalize_event_types(None)
        self.assertIn('login_verification_required',defaults)
        self.assertIn('message_verification_required',defaults)
        self.assertNotIn('transient_warning',defaults)
        self.assertNotIn('login_recovered',defaults)
        self.assertEqual(normalize_event_types([]),[])

    def test_auth_issue_classification_is_based_on_actual_stage(self):
        cases=(('login_verification_required','login','qr_login',True),
               ('message_verification_required','message','sync_web_session',True),
               ('signing_token_missing','none','view_diagnostic',False),
               ('connection_failed','none','check_connection',False))
        for category,scope,action,human in cases:
            state=auth_recovery.set_state(self.db,'event-fixture','required',category)
            self.assertEqual(state['verification_scope'],scope)
            self.assertEqual(state['requires_manual_verification'],human)
            if action=='view_diagnostic':
                self.assertIn(action,state['action_codes'])
            else:self.assertEqual(state['recovery_action'],action)
            self.assertTrue(state['auth_paused'])

    def test_restart_and_cancel_preserve_real_verification_reason(self):
        auth_recovery.set_state(self.db,'event-fixture','required','message_verification_required')
        active=auth_recovery.begin_verification(self.db,'event-fixture',check_only=True)
        state=auth_recovery.set_state(self.db,'event-fixture','required','cancelled',expected_attempt=active['attempt_id'])
        self.assertEqual(state['event_type'],'message_verification_required')
        auth_recovery.begin_verification(self.db,'event-fixture',check_only=True)
        auth_recovery.initialize(self.db)
        self.assertEqual(auth_recovery.get(self.db,'event-fixture')['event_type'],'message_verification_required')

    async def test_same_incident_concurrent_delivery_and_restart_emit_once(self):
        await asyncio.gather(*(event_journal.dispatch(self.db,self.live,'message_verification_required','fixture') for _ in range(6)))
        self.assertEqual(self.live._send_email_notification.await_count,1)
        self.db.close();self.db=DBManager(self.path)
        await event_journal.dispatch(self.db,self.live,'message_verification_required','fixture')
        self.assertEqual(self.live._send_email_notification.await_count,1)
        self.assertEqual(self.db.conn.execute('SELECT count(*) FROM operational_incidents').fetchone()[0],1)

    async def test_recovered_then_new_incident_and_distinct_order_context(self):
        await event_journal.dispatch(self.db,self.live,'message_verification_required','fixture')
        event_journal.resolve(self.db,'event-fixture',('message_verification_required',))
        await event_journal.dispatch(self.db,self.live,'message_verification_required','fixture')
        await event_journal.dispatch(self.db,self.live,'delivery_failed','fixture',context='order-a')
        await event_journal.dispatch(self.db,self.live,'delivery_failed','fixture',context='order-b')
        self.assertEqual(self.live._send_email_notification.await_count,4)

    async def test_warn_can_be_subscribed_but_is_silent_by_default(self):
        self.db.update_notification_rule(self.rule,event_types=normalize_event_types(None))
        await event_journal.dispatch(self.db,self.live,'transient_warning','网络波动')
        self.live._send_email_notification.assert_not_awaited()
        self.assertEqual(self.db.get_risk_control_logs()[0]['severity'],'warning')
        self.db.update_notification_rule(self.rule,event_types=['transient_warning'])
        await event_journal.dispatch(self.db,self.live,'transient_warning','网络波动')
        self.live._send_email_notification.assert_awaited_once()

    async def test_disabled_empty_and_foreign_channel_never_send(self):
        self.db.update_notification_rule(self.rule,event_types=[])
        await event_journal.dispatch(self.db,self.live,'credentials_invalid','fixture')
        self.db.update_notification_rule(self.rule,event_types=EVENT_IDS,enabled=False)
        await event_journal.dispatch(self.db,self.live,'credentials_invalid','fixture')
        self.live._send_email_notification.assert_not_awaited()

    async def test_unknown_delivery_never_repeats(self):
        from app.services.smtp_delivery import DeliveryUncertain
        self.live._send_email_notification.side_effect=DeliveryUncertain('fixture')
        await event_journal.dispatch(self.db,self.live,'credentials_invalid','fixture')
        await event_journal.dispatch(self.db,self.live,'credentials_invalid','fixture')
        self.live._send_email_notification.assert_awaited_once()
        self.assertEqual(self.db.conn.execute('SELECT status FROM operational_notices').fetchone()[0],'unknown')

    async def test_catalog_matches_mail_subject_body_and_redacted_audit(self):
        await event_journal.dispatch(self.db,self.live,'message_verification_required','token=private-fixture-value')
        args=self.live._send_email_notification.await_args.args
        self.assertIn('消息安全验证',args[0]['_event_subject'])
        self.assertIn('官网',notification_plain_text(args[1]))
        self.assertNotIn('private-fixture-value',str(self.db.get_risk_control_logs()))

    def test_template_includes_impact_and_real_next_action_without_saved_template(self):
        message=render_account_notification(self.db,'event-fixture','login_session_expired','会话失效')
        plain=notification_plain_text(message)
        self.assertIn('登录会话已失效',plain)
        self.assertIn('重新扫码',plain)
        self.assertIn('建议操作',plain)
