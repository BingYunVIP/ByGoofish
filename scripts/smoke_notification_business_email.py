#!/usr/bin/env python3
"""Real SMTP acceptance for all formal event scenarios in an isolated database.

No channel-test endpoint, production state mutations, IM sends, auth or orders.
The source rule supplies only the enabled email channel and a subscription matrix.
"""
import argparse
import asyncio
import json
import os
import re
from pathlib import Path
import secrets
import sqlite3
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-db', type=Path, required=True)
    p.add_argument('--source-rule', type=int, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--send', action='store_true')
    p.add_argument('--interval', type=float, default=8, help='不同事件之间的发信间隔（至少2秒）')
    p.add_argument('--events', nargs='+', help='限定场景；不要重发已取得SMTP回执的场景')
    args = p.parse_args()
    if not args.send:
        p.error('真实邮件验收须明确指定 --send；每种事件最多一封')
    isolated = Path(os.environ.get('DB_PATH','')).resolve()
    if not isolated.parent.name.startswith('bygoofish-isolated-') or isolated == args.source_db.resolve():
        p.error('必须通过 scripts/dev_isolated.sh 运行，不能使用生产数据库作为运行库')
    src = sqlite3.connect('file:' + str(args.source_db.resolve()) + '?mode=ro', uri=True)
    row = src.execute('''SELECT nc.config,mn.event_types FROM message_notifications mn
        JOIN notification_channels nc ON nc.id=mn.channel_id JOIN cookies c ON c.id=mn.cookie_id
        WHERE mn.id=? AND nc.type='email' AND nc.enabled=1 AND mn.enabled=1 AND nc.user_id=c.user_id''',
        (args.source_rule,)).fetchone()
    src.close()
    if not row:
        p.error('来源不是已启用且归属一致的邮件规则')
    config, saved = row
    # Secrets remain in memory and the automatically removed isolated DB only.
    from loguru import logger
    logger.remove()
    delivery_receipts = []
    def collect_receipt(log):
        line = log.record['message']
        if line.startswith('source=notification_delivery '):
            fields = dict(re.findall(r'(event_type|request_id|result)=([^ ]+)', line))
            delivery_receipts.append(fields)
    logger.add(collect_receipt, level='INFO')
    from app.db_manager import db_manager as db
    from app.notification_events import EVENT_IDS, EVENT_DEFINITIONS, rule_accepts_event
    from app.services.notification_templates import PREFIX, DEFAULT_TEMPLATE
    events = args.events or EVENT_IDS
    if any(e not in EVENT_IDS for e in events) or len(set(events)) != len(events):
        p.error('场景类型无效或重复')
    if args.output.exists():
        p.error('验收文件已存在，请先核对已有回执；禁止覆盖并重复发信')
    from app.services import smtp_delivery
    from scripts.notification_scenarios import setup_account, make_instance, run_scenario, ACCOUNT
    owner = db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
    setup_account(db, owner)
    channel = db.create_notification_channel('自动场景验收临时渠道', 'email', config, owner)
    db.create_notification_rule(ACCOUNT, channel, event_types=EVENT_IDS)
    for event in EVENT_IDS:
        db.set_user_setting(owner, PREFIX + event, DEFAULT_TEMPLATE)
    logger.remove()
    logger.add(collect_receipt, level='INFO')
    run_id = secrets.token_hex(6)
    report = {'run_id': run_id, 'mode':'synthetic-business-outcomes/real-smtp/no-channel-test',
              'production_settings_unchanged': True, 'production_subscription': {
                  event: rule_accepts_event(saved, event) for event in EVENT_IDS}, 'events':[]}
    current = {'event': '', 'submissions':0, 'smtp_stage':''}
    real_smtp = smtp_delivery.send_message
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def persist():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        args.output.chmod(0o600)

    def observe_real_smtp(message, *positional, **kwargs):
        current['submissions'] += 1
        if current['submissions'] != 1:
            raise RuntimeError('同一场景出现重复发信调用，已阻止')
        label = next(x['label'] for x in EVENT_DEFINITIONS if x['id']==current['event'])
        if label not in str(message['Subject']):
            raise RuntimeError('正式事件邮件标题与目录不一致，阻止发送')
        message.replace_header('Subject', f'【自动业务场景验收·无需处理】{label} · {run_id}')
        # This is an observer around REAL SMTP, never a simulated receipt.
        current['smtp_stage'] = 'connection_or_tls'
        return real_smtp(message, *positional, **kwargs)

    async def run():
        with patch('app.cookie_manager.manager', None), patch('app.services.smtp_delivery.send_message', side_effect=observe_real_smtp):
            for event in events:
                current.update(event=event, submissions=0, smtp_error_type='')
                receipts_start = len(delivery_receipts)
                started=time.monotonic()
                live = make_instance(db)
                with patch.object(db,'get_account_notifications',wraps=db.get_account_notifications) as lookup:
                    await run_scenario(event, live, db, owner)
                selected=lookup.call_args.kwargs.get('event_type') if lookup.call_args else None
                # Real low-level acceptance must also be observed below.
                ok = current['submissions']==1 and selected==event and event in accepted
                result={'event_type':event,'smtp_submissions':current['submissions'],
                        'smtp_accepted':ok,'smtp_error_type':current['smtp_error_type'],'smtp_stage':current['smtp_stage'],'selected_event':selected,
                        'receipts':delivery_receipts[receipts_start:],'elapsed_ms':int((time.monotonic()-started)*1000)}
                report['events'].append(result);persist();print(json.dumps(result),flush=True)
                if not ok:
                    raise RuntimeError('场景未取得真实SMTP接受回执；停止且不重试')
                await asyncio.sleep(max(2,args.interval))
    accepted=set()
    original_observer=observe_real_smtp
    def track_acceptance(message,*args,**kwargs):
        try:
            result=original_observer(message,*args,**kwargs)
        except Exception as error:
            current['smtp_error_type'] = type(error).__name__
            raise
        accepted.add(current['event'])
        return result
    observe_real_smtp=track_acceptance
    # Observe protocol phases without recording commands, credentials or replies.
    from contextlib import ExitStack
    def phase(method, before, after):
        def traced(*args, **kwargs):
            current['smtp_stage']=before
            value=method(*args, **kwargs)
            current['smtp_stage']=after
            return value
        return traced
    try:
        with ExitStack() as stack:
            for cls in (smtp_delivery.DeadlineSMTP, smtp_delivery.DeadlineSMTPSSL):
                stack.enter_context(patch.object(cls,'login',phase(cls.login,'authentication','ready')))
                stack.enter_context(patch.object(cls,'send_message',phase(cls.send_message,'submission','accepted')))
            asyncio.run(run())
    finally:
        persist()
    assert len(report['events'])==len(events) and len(accepted)==len(events)
    print(json.dumps({'run_id':run_id,'accepted':len(accepted),'inbox_delivery':'not independently confirmed'}))


if __name__=='__main__':
    main()
