#!/usr/bin/env python3
"""Send exactly one real verification email through the credential route using an isolated user."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import sqlite3


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-db',type=Path,required=True,help='Read-only source for the configured admin mail channel')
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    path=Path(os.environ.get('DB_PATH','')).resolve()
    if not path.parent.name.startswith('bygoofish-isolated-') or path.exists() or path==args.source_db.resolve():
        p.error('Use scripts/dev_isolated.sh with a fresh database')
    source=sqlite3.connect(args.source_db.resolve().as_uri()+'?mode=ro',uri=True)
    before=source.execute('SELECT id,email,password_hash FROM users ORDER BY id').fetchall()
    row=source.execute("""SELECT c.config FROM notification_channels c JOIN users u ON u.id=c.user_id
        WHERE u.username='admin' AND c.type='email' AND c.enabled=1 ORDER BY c.id LIMIT 1""").fetchone()
    if not row:raise RuntimeError('No enabled admin mail channel; no email was sent')
    config=json.loads(row[0])
    recipient=str(config.get('recipient_email') or '').split(',')[0].strip()
    if not recipient:raise RuntimeError('No configured recipient; no email was sent')
    import httpx
    from app.reply_server import app
    from app.db_manager import db_manager as db
    from app.auth import SESSION_TOKENS
    from app.auth_validators import validate_email
    assert validate_email(recipient)[0]
    uid=db.get_user_by_username('admin')['id']
    db.create_notification_channel('隔离验证码验收','email',json.dumps(config),uid)
    token=secrets.token_urlsafe(32);SESSION_TOKENS[token]={'user_id':uid}
    submissions=0
    original=db._send_email_via_smtp
    async def send_once(email,subject,content,*args,**kwargs):
        nonlocal submissions
        submissions+=1
        if submissions!=1 or email!=recipient:raise RuntimeError('Unexpected extra email attempt rejected')
        # Clearly distinguish this authorized test from changing the production owner's credentials.
        return await original(email,'[隔离验收] '+subject,
                              '这是本机凭证功能的隔离验收，不会修改您的生产登录邮箱或密码。\n\n'+content,*args,**kwargs)
    db._send_email_via_smtp=send_once
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://isolated') as client:
            headers={'Authorization':'Bearer '+token}
            sent=(await client.post('/change-email/send-code',headers=headers,json={'email':recipient,'current_password':'admin123'})).json()
            if sent.get('success') is not True:
                args.output.parent.mkdir(parents=True,exist_ok=True)
                args.output.write_text(json.dumps({'submissions':submissions,'result':'not_confirmed','error_code':sent.get('error_code'),'request_id':sent.get('request_id')},indent=2))
                raise RuntimeError('Email submission not confirmed; no automatic retry')
            code=db.conn.execute('SELECT code FROM email_verifications WHERE email=? ORDER BY id DESC LIMIT 1',(recipient,)).fetchone()[0]
            changed=(await client.post('/change-email',headers=headers,json={'current_password':'admin123','new_email':recipient,'verification_code':code})).json()
            assert changed['success']
            assert (await client.get('/account/profile',headers=headers)).status_code==401
            logged=(await client.post('/login',json={'email':recipient,'password':'admin123'})).json()
            assert logged['success']
            profile=(await client.get('/account/profile',headers={'Authorization':'Bearer '+logged['token']})).json()
            assert profile['email']==recipient
            after=source.execute('SELECT id,email,password_hash FROM users ORDER BY id').fetchall()
            assert before==after
            result={'submissions':submissions,'smtp_accepted':True,'inbox_delivery':'not independently confirmed',
                    'request_id':sent['request_id'],'isolated_email_update_and_login':True,'production_credentials_unchanged':True}
            args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2));os.chmod(args.output,0o600)
            print(json.dumps(result))
    try:asyncio.run(run())
    finally:source.close()


if __name__=='__main__':main()
