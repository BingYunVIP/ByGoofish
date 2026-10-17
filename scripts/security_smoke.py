#!/usr/bin/env python3
"""隔离真实 HTTP / SQLite / Chromium 验收，不调用闲鱼、不发送邮件。"""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import sqlite3
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import httpx

BOOTSTRAP=r'''
import os,time
from contextlib import asynccontextmanager
from pathlib import Path
assert Path(os.environ['DB_PATH']).parent==Path.cwd()
assert Path.cwd().name.startswith('xianyu-security-')
from app.reply_server import app, password_login_sessions, qr_login_manager
from app.db_manager import db_manager as db
from utils.qr_login import QRLoginSession
from playwright.async_api import async_playwright
from utils.direct_network import direct_browser_options
from utils.captcha_remote_control import captcha_controller
for name in ('owner','other'):
    if not db.get_user_by_username(name):db.create_user(name,name+'@example.invalid',os.environ['AUDIT_PASSWORD'])
uid=db.get_user_by_username('owner')['id']
if not db.get_cookie_details('test-owned'):
    db.save_cookie('test-owned','unb=test-owned; cookie2=local-fixture',uid)
    db.conn.execute("UPDATE cookies SET auto_confirm=0,remark='retain',pause_duration=77,auto_rate_enabled=1 WHERE id='test-owned'")
    db.conn.execute("INSERT INTO default_replies(cookie_id,enabled,reply_content,reply_once) VALUES('test-owned',1,'LOCAL_TEMPLATE',1)")
    db.conn.commit()
qr_login_manager.sessions['local-owned-qr']=QRLoginSession('local-owned-qr',owner_id=uid)
password_login_sessions['local-owned-password']={'user_id':uid,'timestamp':time.time(),'status':'processing','account_id':'test-owned'}
@asynccontextmanager
async def lifespan(app):
    async with async_playwright() as p:
        browser=await p.chromium.launch(**direct_browser_options({'headless':True}))
        page=await browser.new_page()
        await page.set_content('<title>Local security test</title><div id="nocaptcha" style="width:300px;height:100px">local</div>')
        await captcha_controller.create_session('test-owned',page,owner_id=uid)
        yield
        await browser.close()
app.router.lifespan_context=lifespan
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--frontend-dir',type=Path)
    args=parser.parse_args();results=[]
    def check(name,condition):
        results.append({'check':name,'passed':bool(condition)})
        print(('[OK] ' if condition else '[FAIL] ')+name,flush=True)
        if not condition:raise AssertionError(name)
    with tempfile.TemporaryDirectory(prefix='xianyu-security-') as directory:
        work=Path(directory);password=secrets.token_urlsafe(24);admin_password=secrets.token_urlsafe(24)
        (work/'local_app.py').write_text(BOOTSTRAP)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        env={**os.environ,'PYTHONPATH':f'{work}:{ROOT}','DB_PATH':str(work/'local.db'),
             'AUDIT_PASSWORD':password,'ADMIN_PASSWORD':admin_password,'SQL_LOG_ENABLED':'false','PYTHONDONTWRITEBYTECODE':'1'}
        if args.frontend_dir:env['APP_STATIC_DIR']=str(args.frontend_dir.resolve())
        process=None
        def start():
            nonlocal process
            with (work/'service.log').open('ab') as output:
                process=subprocess.Popen([sys.executable,'-m','uvicorn','local_app:app','--host','127.0.0.1','--port',str(port)],cwd=work,env=env,stdout=output,stderr=output)
            for _ in range(100):
                if process.poll() is not None:raise RuntimeError('隔离服务启动失败，检查临时服务日志')
                try:
                    if httpx.get(f'http://127.0.0.1:{port}/verify',trust_env=False,timeout=1).status_code==200:return
                except httpx.HTTPError:pass
                time.sleep(.2)
            raise RuntimeError('隔离服务启动超时')
        def stop():
            if process and process.poll() is None:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait()
        try:
            start()
            with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,timeout=15) as client:
                def login(name,pw):
                    body=client.post('/login',json={'username':name,'password':pw}).json()
                    assert body.get('success'), 'local login'
                    return {'Authorization':'Bearer '+body['token']}
                owner=login('owner',password);other=login('other',password);admin=login('admin',admin_password)
                uid=client.get('/verify',headers=owner).json()['user_id']
                public=client.get('/cookies/details',headers=owner).json()
                check('account list contains no Cookie or password',len(public)==1 and 'value' not in public[0] and 'login_password' not in public[0])
                public=client.get('/cookie/test-owned/details',headers=owner).json()
                check('account details contains no credentials','value' not in public and 'password' not in public and public.get('cookie_configured'))
                for path in ['/cookies/details','/api/captcha/sessions','/api/captcha/session/test-owned']:
                    check('anonymous blocked '+path,client.get(path).status_code==401)
                check('captcha other user cannot read',client.get('/api/captcha/session/test-owned',headers=other).status_code==404)
                check('captcha own screenshot works',bool(client.get('/api/captcha/session/test-owned',headers=owner).json().get('screenshot')))
                check('captcha list scoped',client.get('/api/captcha/sessions',headers=other).json()['count']==0)
                check('captcha anonymous mouse blocked',client.post('/api/captcha/mouse_event',json={'session_id':'test-owned','event_type':'move','x':2,'y':2}).status_code==401)
                check('captcha other close blocked',client.delete('/api/captcha/session/test-owned',headers=other).status_code==404)
                check('own mouse works',client.post('/api/captcha/mouse_event',headers=owner,json={'session_id':'test-owned','event_type':'move','x':2,'y':2}).status_code==200)
                import asyncio,websockets
                async def websocket_access(headers,origin=None):
                    try:
                        protocols=['xianyu-auth',headers['Authorization'].removeprefix('Bearer ')] if headers else None
                        async with websockets.connect(f'ws://127.0.0.1:{port}/api/captcha/ws/test-owned',subprotocols=protocols,origin=origin) as ws:
                            body=json.loads(await asyncio.wait_for(ws.recv(),3))
                            return body.get('type')=='session_info'
                    except websockets.InvalidStatusCode:return False
                check('anonymous WebSocket rejected',not asyncio.run(websocket_access(None)))
                check('cross-user WebSocket rejected',not asyncio.run(websocket_access(other)))
                check('cross-origin WebSocket rejected',not asyncio.run(websocket_access(owner,'https://other.invalid')))
                check('owner WebSocket works',asyncio.run(websocket_access(owner,f'http://127.0.0.1:{port}')))
                for path in ['/qr-login/check/local-owned-qr','/password-login/check/local-owned-password']:
                    check('login session scoped '+path,client.get(path,headers=other).status_code==404)
                    check('login session owner '+path,client.get(path,headers=owner).status_code==200)
                for path,payload in [('/items/search',{'keyword':'local'}),('/items/search_multiple',{'keyword':'local'})]:
                    check('anonymous search blocked',client.post(path,json=payload).status_code==401)
                for tables in [
                    {'system_settings':{'columns':['key','value'],'rows':[['private-setting','bad']]}},
                    {'keywords':{'columns':['cookie_id','keyword','reply'],'rows':[['test-owned','bad','bad']]}},
                    {'cookies':{'columns':['id,user_id) VALUES (?,?) --'],'rows':[['bad',uid]]}},
                ]:
                    r=client.post('/backup/import',headers=other,files={'file':('test.json',json.dumps({'data':tables}),'application/json')})
                    check('unsafe user import rejected',r.status_code==400)
                backup=client.get('/backup/export',headers=owner)
                check('own backup exported',backup.status_code==200)
                r=client.post('/backup/import',headers=owner,files={'file':('own.json',backup.content,'application/json')})
                check('own backup roundtrip',r.status_code==200)
                channel=client.post('/notification-channels',headers=owner,json={'name':'local-credentials','type':'email','config':json.dumps({'smtp_server':'smtp.example.invalid','smtp_port':465,'email_user':'local@example.invalid','email_password':'local-secret-value','recipient_email':'local@example.invalid'})}).json()['id']
                listed=client.get(f'/notification-channels/{channel}',headers=owner).json()
                check('notification secret masked','local-secret-value' not in json.dumps(listed) and 'email_password' in listed['configured_fields'])
                r=client.put(f'/notification-channels/{channel}',headers=owner,json={'config':listed['config']})
                with sqlite3.connect(work/'local.db') as d:stored=json.loads(d.execute('SELECT config FROM notification_channels WHERE id=?',(channel,)).fetchone()[0])
                check('blank secret preserves stored value',r.status_code==200 and stored['email_password']=='local-secret-value')
                webhook_config={'webhook_url':'https://example.invalid/private-hook','headers':{'Authorization':'Bearer LOCAL_HEADER'},'http_method':'POST'}
                webhook=client.post('/notification-channels',headers=owner,json={'name':'local-webhook','type':'webhook','config':json.dumps(webhook_config)}).json()['id']
                public_webhook=client.get(f'/notification-channels/{webhook}',headers=owner).json()
                check('webhook headers and URL masked','LOCAL_HEADER' not in json.dumps(public_webhook) and 'private-hook' not in json.dumps(public_webhook))
                r=client.put(f'/notification-channels/{webhook}',headers=owner,json={'config':public_webhook['config']})
                with sqlite3.connect(work/'local.db') as d:stored_webhook=json.loads(d.execute('SELECT config FROM notification_channels WHERE id=?',(webhook,)).fetchone()[0])
                check('blank webhook fields preserve credentials',r.status_code==200 and stored_webhook['headers']==webhook_config['headers'] and stored_webhook['webhook_url']==webhook_config['webhook_url'])
                # 已取得的真实会话与真实静态构建做前端验收，不替换任何API响应。
                if args.frontend_dir:
                    from playwright.sync_api import sync_playwright,expect
                    from utils.direct_network import direct_browser_options
                    with sync_playwright() as pw:
                        with pw.chromium.launch(**direct_browser_options({'headless':True})) as browser:
                            page=browser.new_page(viewport={'width':1440,'height':1000})
                            errors=[];fetches=[]
                            page.on('pageerror',lambda e:errors.append(str(e)))
                            page.on('request',lambda r:fetches.append(r.url) if r.url.endswith('/cookies/details') else None)
                            page.goto(f'http://127.0.0.1:{port}/',wait_until='networkidle')
                            page.get_by_placeholder('请输入账号').fill('owner')
                            page.get_by_placeholder('请输入密码').fill(password)
                            page.get_by_role('button',name='登录',exact=True).click()
                            page.get_by_role('button',name='账号管理',exact=True).click()
                            page.wait_for_load_state('networkidle')
                            check('inactive pages are not mounted',page.locator('section[hidden]').count()==0)
                            check('account query is not repeated nine times',len(fetches)<=2)
                            check('desktop content clears fixed sidebar',page.locator('main').bounding_box()['x']>=248)
                            page.set_viewport_size({'width':1280,'height':900})
                            page.get_by_role('button',name='通知与日志',exact=True).click()
                            page.get_by_role('tab',name='账号通知',exact=True).click()
                            check('notification tab is clickable at 1280px',page.get_by_role('tab',name='账号通知',exact=True).get_attribute('aria-selected')=='true')
                            page.get_by_role('button',name='账号管理',exact=True).click()
                            page.get_by_title('编辑账号').first.click()
                            dialog=page.get_by_role('dialog',name='编辑账号')
                            expect(dialog).to_be_visible()
                            expect(page.get_by_text('自动确认发货',exact=True)).to_be_visible()
                            page.wait_for_timeout(100)
                            check('dialog initial focus',page.evaluate("!!document.activeElement.closest('[role=dialog]')"))
                            page.keyboard.press('Shift+Tab')
                            check('dialog focus stays inside',page.evaluate("!!document.activeElement.closest('[role=dialog]')"))
                            page.keyboard.press('Escape')
                            expect(dialog).to_be_hidden()
                            check('dialog restores trigger focus',page.evaluate("document.activeElement.title==='编辑账号'"))
                            page.set_viewport_size({'width':390,'height':844})
                            page.wait_for_timeout(300)
                            check('closed mobile sidebar does not cover content',page.locator('aside').bounding_box()['x']<=-247)
                            check('mobile layout no overflow',page.evaluate('document.documentElement.scrollWidth<=innerWidth+2'))
                            check('frontend no JavaScript errors',not errors)
                            browser_token=page.evaluate("localStorage.getItem('auth_token')")
                            page.set_viewport_size({'width':1440,'height':1000})
                            page.get_by_role('button',name='退出登录',exact=True).click()
                            expect(page.get_by_placeholder('请输入账号')).to_be_visible()
                            check('logout button revokes server session',not client.get('/verify',headers={'Authorization':'Bearer '+browser_token}).json()['authenticated'])
                request={'cookie_id':'test-owned','msg_time':'local','user_url':'','send_user_id':'local','send_user_name':'local','item_id':'local','send_message':'local','chat_id':'local'}
                check('anonymous reply blocked',client.post('/xianyu/reply',json=request).status_code==401)
                check('cross account reply blocked',client.post('/xianyu/reply',headers=other,json=request).status_code==404)
                check('authorized template preview',client.post('/xianyu/reply',headers=owner,json=request).status_code==200)
                check('preview does not consume',client.post('/xianyu/reply',headers=owner,json=request).status_code==200)
                for key in ['xianyu_qq_reply_2024','xianyu_api_secret_2024']:
                    r=client.post('/send-message',json={'api_key':key,'cookie_id':'test-owned','chat_id':'local','to_user_id':'local','message':'local'})
                    check('public keys rejected',r.json().get('success') is False and '秘钥' in r.json().get('message',''))
                check('scoped key generation protected',client.post('/cookies/test-owned/api-key',headers=other).status_code==404)
                scoped=client.post('/cookies/test-owned/api-key',headers=owner).json()['api_key']
                accepted=client.post('/send-message',json={'api_key':scoped,'cookie_id':'test-owned','chat_id':'local','to_user_id':'local','message':'local'})
                check('valid scoped key reaches owned runtime only','账号实例不存在' in accepted.json().get('message',''))
                client.get('/health/ready',headers=owner)
                time.sleep(.7)
                logs=client.get('/logs',headers=admin).json()
                check('live log collector survives account module import',any('/health/ready' in row.get('message','') for row in logs.get('logs',[])))
                r=client.post('/send-message',json={'api_key':scoped,'cookie_id':'other-id','chat_id':'local','to_user_id':'local','message':'local'})
                check('key cannot cross account',r.json().get('success') is False and '秘钥' in r.json().get('message',''))
                second=login('owner',password);new_password=secrets.token_urlsafe(24)
                check('password changed',client.post('/change-password',headers=owner,json={'current_password':password,'new_password':new_password}).json()['success'])
                check('all old tokens revoked',not client.get('/verify',headers=second).json()['authenticated'])
                owner=login('owner',new_password)
                stop();start()
                check('session survives service restart',client.get('/verify',headers=owner).json()['authenticated'])
                local_code='''from app.db_manager import db_manager as d
r=d.conn.execute("SELECT user_id FROM cookies WHERE id='test-owned'").fetchone()
assert d.save_cookie('test-owned','unb=test-owned; cookie2=updated',r[0])
assert tuple(d.conn.execute("SELECT auto_confirm,remark,pause_duration,auto_rate_enabled FROM cookies WHERE id='test-owned'").fetchone())==(0,'retain',77,1)
'''
                r=subprocess.run([sys.executable,'-B','-c',local_code],cwd=work,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                check('cookie settings preserved',r.returncode==0)
                backup_code='''import os,sqlite3
from pathlib import Path
from utils.db_backup import create_backup
r=create_backup()
assert r['success']
assert Path(r['path']).parent==Path(os.environ['DB_PATH']).parent
assert Path(r['path']).stat().st_mode & 0o777 == 0o600
with sqlite3.connect(r['path']) as conn:
    assert conn.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    assert conn.execute("SELECT count(*) FROM cookies WHERE id='test-owned'").fetchone()[0]==1
'''
                r=subprocess.run([sys.executable,'-B','-c',backup_code],cwd=work,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                check('automatic snapshot respects DB_PATH and private permissions',r.returncode==0)
                check('backup list follows actual database directory',client.get('/admin/backup/list',headers=admin).json()['total']==1)
                # 新管理密钥在恢复时不会复活；下载必须是完整快照。
                snap=client.get('/admin/backup/download',headers=admin)
                check('consistent admin snapshot',snap.status_code==200 and snap.content.startswith(b'SQLite format 3'))
                r=client.post('/admin/backup/upload',headers=admin,files={'backup_file':('snapshot.db',snap.content,'application/octet-stream')})
                check('restore staged without live replacement',r.status_code==200 and r.json().get('requires_restart'))
                staged=r.json()['staged_file']
                r=subprocess.run([sys.executable,str(ROOT/'scripts/restore_database.py'),'--file',staged,'--database',str(work/'local.db')],cwd=work,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                check('live restore refused by maintenance lock',r.returncode!=0)
                check('delete user',client.delete(f'/admin/users/{uid}',headers=admin).status_code==200)
                with sqlite3.connect(work/'local.db') as d:
                    count=sum(d.execute(f"SELECT count(*) FROM {t} WHERE cookie_id='test-owned'").fetchone()[0] for t in ['keywords','default_replies','default_reply_records'])
                    check('delete leaves no private rows',count==0)
                    check('integrity',d.execute('PRAGMA quick_check').fetchone()[0]=='ok')
                stop()
                r=subprocess.run([sys.executable,str(ROOT/'scripts/restore_database.py'),'--file',staged,'--database',str(work/'local.db')],cwd=work,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                check('offline snapshot restore succeeds',r.returncode==0)
                with sqlite3.connect(work/'local.db') as d:
                    check('restore revokes old sessions',d.execute('SELECT count(*) FROM auth_sessions').fetchone()[0]==0)
                    check('restore pauses accounts for transaction review',d.execute('SELECT count(*) FROM cookie_status WHERE enabled=1').fetchone()[0]==0)
        finally:
            stop()
            if args.output:
                args.output.parent.mkdir(parents=True,exist_ok=True)
                args.output.write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    print(f'{len(results)} real isolated checks passed; temporary service/data removed')


if __name__=='__main__':main()
