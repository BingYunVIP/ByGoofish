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
    db.save_cookie('test-owned','unb=test-owned; '+('cookie'+'2')+'=local-fixture',uid)
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
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));dead_port=sock.getsockname()[1]  # 释放后无人监听：连接立即被拒绝
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
                with sqlite3.connect(work/'local.db') as d:stored=d.execute('SELECT config FROM notification_channels WHERE id=?',(channel,)).fetchone()[0]
                check('blank secret preserves stored value',r.status_code==200 and stored.startswith('enc:v1:'))
                webhook_config={'webhook_url':'https://example.invalid/private-hook','headers':{'Authorization':'Bearer LOCAL_HEADER'},'http_method':'POST'}
                webhook=client.post('/notification-channels',headers=owner,json={'name':'local-webhook','type':'webhook','config':json.dumps(webhook_config)}).json()['id']
                public_webhook=client.get(f'/notification-channels/{webhook}',headers=owner).json()
                check('webhook headers and URL masked','LOCAL_HEADER' not in json.dumps(public_webhook) and 'private-hook' not in json.dumps(public_webhook))
                r=client.put(f'/notification-channels/{webhook}',headers=owner,json={'config':public_webhook['config']})
                with sqlite3.connect(work/'local.db') as d:stored_webhook=d.execute('SELECT config FROM notification_channels WHERE id=?',(webhook,)).fetchone()[0]
                check('blank webhook fields preserve credentials',r.status_code==200 and stored_webhook.startswith('enc:v1:'))
                # —— 精简与整合：公告更新、系统级邮件设置已移除；AI 与邮件测试接口只接受登录用户 ——
                check('announcement endpoint removed',client.get('/api/announcement',headers=owner).status_code==404)
                check('system email test endpoint removed',client.post('/system-settings/email-test',headers=admin,json={}).status_code in (404,405))
                all_settings=client.get('/system-settings',headers=admin).json();all_settings=all_settings.get('data',all_settings)
                check('no stale announcement or system SMTP settings',not any(k.startswith(('announcement_','smtp_')) for k in all_settings))
                check('AI model list requires login',client.post('/ai-models',json={}).status_code==401)
                check('email channel test requires login',client.post('/notification-channels/email-test',json={}).status_code==401)
                # 邮件渠道测试的目标限制与参数校验（都在发信之前拒绝，不会发出任何邮件）
                email_form={'smtp_server':'smtp.qq.com','smtp_port':465,'email_user':'a@qq.com','email_password':'x','recipient_email':'a@qq.com'}
                r=client.post('/notification-channels/email-test',headers=owner,json={**email_form,'email_password':''})
                check('email test needs a password when none is saved',r.status_code==400)
                for host in ('127.0.0.1','169.254.169.254'):
                    r=client.post('/notification-channels/email-test',headers=owner,json={**email_form,'smtp_server':host})
                    check('email test refuses internal target '+host,r.status_code==400 and r.json()['detail']['code']=='notification_target_forbidden')
                r=client.post('/notification-channels/email-test',headers=owner,json={**email_form,'recipient_email':'not-an-email'})
                check('email test validates addresses',r.status_code==400)
                # AI 接口地址：自动补 /v1，非管理员不能指向本机或内网，公网必须 https
                ai_url=client.get('/ai-reply-settings/test-owned',headers=owner).json()['base_url']
                check('AI default address is the configured relay',ai_url=='https://api.bingyun.vip/v1')
                body={'ai_enabled':False,'model_name':'m','api_key':'s'+'k-fixture-key-0123456789','base_url':'api.bingyun.vip','user_agent':'ua-from-form/1.0'}
                r=client.put('/ai-reply-settings/test-owned',headers=owner,json=body)
                saved=client.get('/ai-reply-settings/test-owned',headers=owner).json()
                check('AI address gets /v1 appended and user agent is kept',r.status_code==200 and saved['base_url']=='https://api.bingyun.vip/v1' and saved['user_agent']=='ua-from-form/1.0')
                check('AI key never returned',saved['api_key']=='' and saved['api_key_configured'] is True)
                for bad in ('http://127.0.0.1:9/v1','http://169.254.169.254/v1','http://api.bingyun.vip/v1','ftp://x.com','https://user:pw@x.com/v1'):
                    r=client.put('/ai-reply-settings/test-owned',headers=owner,json={**body,'api_key':'','base_url':bad})
                    check('AI address rejected: '+bad,r.status_code==400)
                check('rejected address left saved value unchanged',client.get('/ai-reply-settings/test-owned',headers=owner).json()['base_url']=='https://api.bingyun.vip/v1')
                check('system AI address validated for admin',client.put('/system-settings/ai_api_url',headers=admin,json={'value':'http://api.bingyun.vip/v1'}).status_code==400)
                check('system AI address normalized',client.put('/system-settings/ai_api_url',headers=admin,json={'value':'api.bingyun.vip'}).status_code==200
                      and client.get('/system-settings',headers=admin).json().get('ai_api_url')=='https://api.bingyun.vip/v1')
                # 模型列表接口：先在联网前拒绝的情形（不依赖任何外部服务）
                def models(headers,payload):
                    time.sleep(2.2)  # 该接口两次请求至少间隔 2 秒
                    return client.post('/ai-models',headers=headers,json=payload)
                check('models: missing key is rejected',models(owner,{'base_url':'https://api.bingyun.vip/v1'}).status_code==400)
                check('models: saved key is not reused for a different address',
                      'API Key' in models(owner,{'base_url':'https://other.example.invalid/v1','cookie_id':'test-owned'}).json()['detail'])
                check('models: other users cannot use my account key',models(other,{'base_url':'https://api.bingyun.vip/v1','cookie_id':'test-owned'}).status_code==404)
                check('models: system scope is admin only',models(owner,{'base_url':'https://api.bingyun.vip/v1','scope':'system'}).status_code==403)
                internal={'base_url':'http://127.0.0.1:9/v1','api_key':'sk-x-0123456789012345'}
                check('models: non-admin cannot target internal hosts',models(owner,internal).status_code==400)
                check('models: rapid repeat by the same user is rate limited',client.post('/ai-models',headers=owner,json=internal).status_code==429)
                check('models: metadata address refused even for admin',models(admin,{'base_url':'http://169.254.169.254/v1','api_key':'sk-x-0123456789012345'}).status_code==400)
                # 旧版遗留设置在服务重启时被清理；填写过的系统 SMTP 不会被静默销毁
                with sqlite3.connect(work/'local.db') as d:
                    d.executemany('INSERT OR REPLACE INTO system_settings(key,value,description) VALUES(?,?,?)',
                                  [('announcement_enabled','true',''),('announcement_source_url','https://example.invalid/a.json',''),
                                   ('smtp_server','',''),('smtp_password','',''),('smtp_port','587','')])
                # 已取得的真实会话与真实静态构建做前端验收，不替换任何API响应。
                def same_row(left,right):
                    # right 在 left 的右侧，且垂直方向在同一行
                    return abs((left['y']+left['height']/2)-(right['y']+right['height']/2))<12 and right['x']>=left['x']+left['width']-2
                if args.frontend_dir:
                    from playwright.sync_api import sync_playwright,expect
                    from utils.direct_network import direct_browser_options
                    with sync_playwright() as pw:
                        with pw.chromium.launch(**direct_browser_options({'headless':True})) as browser:
                            page=browser.new_page(viewport={'width':1440,'height':1000})
                            errors=[];fetches=[];requested=[]
                            page.on('pageerror',lambda e:errors.append(str(e)))
                            page.on('request',lambda r:requested.append(r.url))
                            page.on('request',lambda r:fetches.append(r.url) if r.url.endswith('/cookies/details') else None)
                            page.goto(f'http://127.0.0.1:{port}/',wait_until='networkidle')
                            page.evaluate("localStorage.setItem('active_page','about')")  # 旧版本留下的、已删除的页面
                            page.reload(wait_until='networkidle')
                            page.get_by_placeholder('请输入账号').fill('owner')
                            page.get_by_placeholder('请输入密码').fill(password)
                            page.get_by_role('button',name='登录',exact=True).click()
                            page.get_by_role('button',name='账号管理',exact=True).wait_for()
                            check('removed page in storage falls back to dashboard',
                                  page.evaluate("localStorage.getItem('active_page')")=='dashboard')
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
                            # —— 精简后的界面（owner 是普通用户）——
                            check('sidebar has no About entry',page.get_by_role('button',name='关于',exact=True).count()==0)
                            check('no announcement request is made',not any('/api/announcement' in url for url in requested))
                            page.get_by_role('button',name='AI 回复',exact=True).click()
                            page.get_by_label('接口地址').wait_for()
                            url_box=page.get_by_label('接口地址')
                            expect(url_box).to_have_value('https://api.bingyun.vip/v1')
                            url_box.fill('api.example.com');url_box.blur()
                            check('AI address gets /v1 on blur',url_box.input_value()=='https://api.example.com/v1')
                            url_box.fill('https://api.example.com/v1/chat/completions');url_box.blur()
                            check('pasted endpoint path is trimmed',url_box.input_value()=='https://api.example.com/v1')
                            for text in ('免费领取 token','没有 API Key？每天可免费领取额度','兼容 OpenAI 协议的服务根地址','AI 不够用','跳转到我的中转站'):
                                check('AI page has no ad/help text: '+text,page.get_by_text(text).count()==0)
                            model_box=page.get_by_label('模型名称');fetch_button=page.get_by_role('button',name='获取模型')
                            check('model fetch button sits right of the model name',fetch_button.count()==1 and same_row(model_box.bounding_box(),fetch_button.bounding_box()))
                            url_box.fill('http://127.0.0.1:9/v1');url_box.blur()
                            page.get_by_placeholder('输入新密钥以替换').fill('s'+'k-fixture-key-0123456789')
                            page.wait_for_timeout(2300)
                            fetch_button.click()
                            expect(page.get_by_text('目标地址指向本机或内网，仅管理员可以使用')).to_be_visible()
                            check('model fetch shows the backend reason inline',True)
                            page.get_by_role('button',name='通知与日志',exact=True).click()
                            page.get_by_role('button',name='新建渠道').click()
                            page.get_by_label('渠道类型').select_option('email')
                            expect(page.get_by_label('SMTP 服务器')).to_have_value('smtp.qq.com')
                            expect(page.get_by_label('SMTP 端口')).to_have_value('465')
                            check('QQ mail defaults are smtp.qq.com and 465',True)
                            name_box=page.get_by_label('发件人显示名');test_button=page.get_by_role('button',name='发送测试邮件')
                            check('display name and test send share one row',same_row(name_box.bounding_box(),test_button.bounding_box()))
                            for text in ('使用上方表单当前填写的配置直接发送','QQ 邮箱等服务通常要求填写授权码'):
                                check('mail form has no help text: '+text,page.get_by_text(text).count()==0)
                            test_button.click()
                            expect(page.get_by_text('请先完整填写 SMTP 服务器')).to_be_visible()
                            check('mail test reports missing fields without sending',True)
                            page.get_by_role('button',name='取消',exact=True).click()
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
                            # —— 管理员的系统设置页 ——
                            page.get_by_placeholder('请输入账号').fill('admin')
                            page.get_by_placeholder('请输入密码').fill(admin_password)
                            page.get_by_role('button',name='登录',exact=True).click()
                            page.get_by_role('button',name='系统设置',exact=True).click()
                            page.get_by_role('tab',name='账号与同步',exact=True).wait_for()
                            check('admin sidebar has no About entry',page.get_by_role('button',name='关于',exact=True).count()==0)
                            tabs=[tab.inner_text().strip() for tab in page.get_by_role('tab').all()]
                            check('settings tabs: no announcement and no mail service',tabs==['账号与同步','默认 AI 配置','快捷短语'])
                            page.get_by_role('tab',name='默认 AI 配置',exact=True).click()
                            api_box=page.get_by_label('API 地址')
                            expect(api_box).to_have_value('https://api.bingyun.vip/v1')
                            api_box.fill('api.example.com');api_box.blur()
                            check('system AI address gets /v1 on blur',api_box.input_value()=='https://api.example.com/v1')
                            for text in ('填写兼容 OpenAI 协议的服务根地址','AI 不够用','跳转到我的中转站'):
                                check('system AI tab has no help text: '+text,page.get_by_text(text).count()==0)
                            model_box=page.get_by_label('默认模型');fetch_button=page.get_by_role('button',name='获取模型')
                            check('default model fetch button sits right of the field',fetch_button.count()==1 and same_row(model_box.bounding_box(),fetch_button.bounding_box()))
                            api_box.fill(f'http://127.0.0.1:{dead_port}/v1');api_box.blur()
                            page.get_by_placeholder('sk-...').fill('s'+'k-fixture-key-0123456789')
                            page.wait_for_timeout(2300)
                            fetch_button.click()
                            expect(page.get_by_text('读取模型列表失败')).to_be_visible()
                            check('admin model fetch reports connection failure inline',True)
                            page.get_by_role('button',name='退出登录',exact=True).click()
                            expect(page.get_by_placeholder('请输入账号')).to_be_visible()
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
                with sqlite3.connect(work/'local.db') as d:
                    left=[r[0] for r in d.execute("SELECT key FROM system_settings WHERE key LIKE 'announcement\\_%' ESCAPE '\\' OR key LIKE 'smtp\\_%' ESCAPE '\\'")]
                check('restart cleans removed settings',left==[])
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
