"""Account prompts use persisted evidence, never keyword matching in old logs."""
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from app.db_manager import DBManager
from app.services import auth_recovery
from app import reply_server


class RiskStatusPresentationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DBManager(str(Path(self.tmp.name) / 'status.db'))
        self.owner = self.db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        self.db.save_cookie('status-fixture', 'unb=fixture', self.owner)
        self.patch = patch('app.db_manager.db_manager', self.db)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.db.close()
        self.tmp.cleanup()

    def snapshot(self):
        return reply_server.get_risk_control_status({'user_id': self.owner})['accounts'][0]

    def test_history_cannot_create_current_verification(self):
        self.db.add_risk_control_log('status-fixture','message_verification_required',event_description='滑块等待验证')
        self.assertFalse(self.snapshot()['requires_manual_verification'])
        self.assertFalse(self.snapshot()['requires_action'])

    def test_login_and_message_have_different_actions(self):
        for category,scope,action in [('login_verification_required','login','qr_login'),
                                      ('message_verification_required','message','sync_web_session')]:
            auth_recovery.set_state(self.db,'status-fixture','required',category)
            snap=self.snapshot()
            self.assertEqual(snap['verification_scope'],scope)
            self.assertEqual(snap['recovery_action'],action)
            self.assertTrue(snap['requires_manual_verification'])

    def test_invalid_signing_is_error_without_human_verification(self):
        auth_recovery.set_state(self.db,'status-fixture','required','signing_token_missing')
        snap=self.snapshot()
        self.assertEqual(snap['event_type'],'credentials_invalid')
        self.assertEqual(snap['severity'],'error')
        self.assertFalse(snap['requires_manual_verification'])

    def test_session_expiry_is_not_a_captcha(self):
        auth_recovery.set_state(self.db,'status-fixture','required','login_session_expired')
        snap=self.snapshot()
        self.assertFalse(snap['requires_manual_verification'])
        self.assertEqual(snap['action_codes'],['qr_login'])

    def test_offline_observation_window_and_severity_filter(self):
        self.db.conn.execute('INSERT INTO account_offline_incidents VALUES(?,?,?,?,NULL)',
            ('status-fixture','outage',time.time()-30,'message_connection_lost'))
        self.db.conn.commit()
        self.assertFalse(self.snapshot()['requires_action'])
        self.db.conn.execute('UPDATE account_offline_incidents SET offline_since=?',(time.time()-91,))
        self.db.conn.commit()
        self.assertEqual(self.snapshot()['event_type'],'account_offline')
        auth_recovery.set_state(self.db,'status-fixture','required','message_verification_required')
        self.assertEqual(self.snapshot()['event_type'],'message_verification_required')
        self.assertEqual(self.db.get_risk_control_logs_count(severity='error'),1)
        self.assertEqual(self.db.get_risk_control_logs_count(severity='warning'),0)
        self.assertEqual(self.db.get_risk_control_logs(severity='error')[0]['action_codes'],['sync_web_session','check_connection'])

    def test_recovered_account_ignores_past_errors(self):
        auth_recovery.set_state(self.db,'status-fixture','required','message_verification_required')
        auth_recovery.set_state(self.db,'status-fixture','recovered')
        snap=self.snapshot()
        self.assertFalse(snap['requires_action'])
        self.assertFalse(snap['auth_paused'])
        self.assertFalse(snap['requires_manual_verification'])
