#!/usr/bin/env python3
"""Three real SMTP submissions from isolated event outcomes; never a channel-test API.

Uses an existing enabled rule's saved subscriptions and email channel. Production
credentials stay in memory; business data, account sessions and settings are untouched.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import time
from unittest.mock import patch


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-db',type=Path,required=True)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--source-rule',type=int)
    group.add_argument('--source-channel',type=int,help='Create an explicit subscription in the isolated database only')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--send',action='store_true',required=True)
    args=parser.parse_args()
    dbpath=Path(os.environ.get('DB_PATH','')).resolve()
    if not dbpath.parent.name.startswith('bygoofish-isolated-') or dbpath==args.source_db.resolve():
        parser.error('Use scripts/dev_isolated.sh with a fresh isolated database')
    if args.output.exists():parser.error('Checkpoint already exists; never repeat submitted mail')
    source=sqlite3.connect('file:'+str(args.source_db.resolve())+'?mode=ro',uri=True)
    if args.source_rule is not None:
        row=source.execute('''SELECT ch.config,n.event_types FROM message_notifications n
            JOIN cookies c ON c.id=n.cookie_id JOIN notification_channels ch ON ch.id=n.channel_id
            WHERE n.id=? AND n.enabled=1 AND ch.enabled=1 AND ch.type='email' AND ch.user_id=c.user_id''',
            (args.source_rule,)).fetchone()
    else:
        row=source.execute("SELECT config,'[\"credentials_invalid\"]' FROM notification_channels WHERE id=? AND enabled=1 AND type='email'",
                           (args.source_channel,)).fetchone()
    source.close()
    if not row:parser.error('Source must be an enabled, owned email channel/subscription')
    from loguru import logger
    logger.remove()
    from app.db_manager import db_manager as db
    from app.notification_events import parse_event_types
    from app.services import event_journal as journal,smtp_delivery
    from XianyuAutoAsync import XianyuLive
    events=parse_event_types(row[1])
    if not events:parser.error('Source rule subscribes to no events; no send allowed')
    event=next((e for e in events if e in ('credentials_invalid','message_verification_required','account_offline')),events[0])
    owner=db.get_user_by_username('admin')['id'];account='reminder-acceptance-fixture'
    db.save_cookie(account,'unb=fixture',owner)
    channel=db.create_notification_channel('提醒次数隔离验收','email',row[0],owner)
    db.create_notification_rule(account,channel,event_types=events)
    live=XianyuLive.__new__(XianyuLive);live.cookie_id=account
    report={'event':event,'clock':'isolated scheduler timestamp advanced; real SMTP at least 8 seconds apart',
            'production_unchanged':True,'isolated_subscription_only':args.source_channel is not None,'submissions':[],'inbox_delivery':'not independently confirmed'}
    args.output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    def save():
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2));args.output.chmod(0o600)
    original=smtp_delivery.send_message
    def real_send(message,*a,**kw):
        if len(report['submissions'])>=3:raise RuntimeError('Unexpected fourth mail blocked')
        entry={'attempted':True,'smtp_accepted':False};report['submissions'].append(entry);save()
        message.replace_header('Subject','【提醒次数验收·虚拟账号·无需处理】'+str(message['Subject']))
        result=original(message,*a,**kw);entry['smtp_accepted']=True;save();return result
    async def run():
        async def first():
            result=await journal.dispatch(db,live,event,'虚拟事件提醒验收，无需处理，不代表真实账号异常',context='acceptance-round')
            if result!=1:raise RuntimeError('SMTP first reminder not accepted; stop without retry')
        with patch('app.services.smtp_delivery.send_message',side_effect=real_send):
            await first()
            stamp=db.conn.execute('SELECT updated_at FROM operational_notices').fetchone()[0]
            assert await journal.process_due(db,now=stamp+299)==0
            await asyncio.sleep(8)
            if await journal.process_due(db,now=stamp+301)!=1:raise RuntimeError('SMTP second reminder not accepted; stop without retry')
            stamp=db.conn.execute('SELECT updated_at FROM operational_notices').fetchone()[0]
            assert await journal.process_due(db,now=stamp+100000)==0
            report['third_blocked']=True
            journal.resolve(db,account,(event,))
            await asyncio.sleep(8)
            await first()
            report['recovery_reset']=True
            log=db.conn.execute('SELECT log_id FROM operational_incidents WHERE resolved_at IS NULL').fetchone()[0]
            journal.acknowledge(db,owner,log)
            assert await journal.process_due(db,now=time.time()+100000)==0
            report['acknowledgement_stops']=True;save()
    try:asyncio.run(run())
    finally:save()
    assert len(report['submissions'])==3 and all(s['smtp_accepted'] for s in report['submissions'])
    print(json.dumps({'smtp_accepted':3,'third_blocked':report['third_blocked'],'recovery_reset':report['recovery_reset'],'production_unchanged':True}))


if __name__=='__main__':main()
