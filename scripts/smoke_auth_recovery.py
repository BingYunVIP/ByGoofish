#!/usr/bin/env python3
"""Isolated UI acceptance for the structured login/message event workflow."""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--stage',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    dbfile=Path(os.environ.get('DB_PATH','')).resolve()
    if not dbfile.parent.name.startswith('bygoofish-isolated-') or dbfile.exists():
        p.error('必须使用 dev_isolated.sh 的新隔离数据库')
    os.environ['APP_STATIC_DIR']=str(args.stage.resolve())
    import uvicorn
    from playwright.sync_api import sync_playwright,expect
    from app import reply_server,cookie_manager
    from app.db_manager import db_manager as db
    from app.auth import SESSION_TOKENS
    from app.notification_events import get_event_definitions
    from app.services import auth_recovery
    from utils.direct_network import direct_browser_options
    owner=db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
    fixtures=[('login-fixture','登录演示','login_verification_required'),
              ('message-fixture','消息演示','message_verification_required'),
              ('credentials-fixture','凭证演示','signing_token_missing')]
    for account,nickname,category in fixtures:
        db.save_cookie(account,'unb=fixture',owner)
        db.conn.execute('UPDATE cookies SET nickname=?,remark=? WHERE id=?',(nickname,'测试备注',account))
        db.conn.commit()
        auth_recovery.set_state(db,account,'required',category)
    channel=db.create_notification_channel('隔离邮件渠道','email',json.dumps({
        'smtp_server':'smtp.example.invalid','smtp_port':465,'email_user':'fixture@example.invalid',
        'email_password':'fixture','recipient_email':'fixture@example.invalid'}),owner)
    cookie_manager.manager=SimpleNamespace(instances={},get_cookie_status=lambda _:True,get_task_state=lambda _:'stopped')
    token=secrets.token_urlsafe(32);SESSION_TOKENS[token]={'user_id':owner}
    sock=socket.socket();sock.bind(('127.0.0.1',0))
    server=uvicorn.Server(uvicorn.Config(reply_server.app,host='127.0.0.1',port=sock.getsockname()[1],log_level='error',access_log=False))
    base=f'http://127.0.0.1:{sock.getsockname()[1]}'
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    deadline=time.monotonic()+10
    while not server.started:
        if time.monotonic()>deadline:raise RuntimeError('隔离服务未就绪')
        time.sleep(.05)
    args.output.mkdir(parents=True,exist_ok=True)
    report={'mode':'isolated DB; no platform requests or SMTP','checks':[],'page_errors':[],'external_requests':[]}
    try:
        with sync_playwright() as pw:
            with pw.chromium.launch(**direct_browser_options({'headless':True})) as browser:
                context=browser.new_context(viewport={'width':1440,'height':1100})
                def route(r):
                    if urlsplit(r.request.url).hostname!='127.0.0.1':
                        report['external_requests'].append(urlsplit(r.request.url).hostname);r.abort();return
                    if r.request.method=='POST' and any(x in r.request.url for x in ('qr-login','manual-session','/test')):
                        raise AssertionError('冒烟不得触发真实认证或外部投递')
                    r.continue_()
                context.route('**/*',route)
                context.add_init_script(f"if(window.top===window){{localStorage.setItem('auth_token',{json.dumps(token)});localStorage.setItem('active_page','accounts');}}")
                page=context.new_page();page.on('pageerror',lambda e:report['page_errors'].append(str(e)))
                page.goto(base,wait_until='networkidle')
                cards={name:page.locator('article').filter(has=page.get_by_role('heading',name=name,exact=True)) for _,name,_ in fixtures}
                login,message,credentials=[cards[name] for _,name,_ in fixtures]
                expect(login.get_by_role('alert')).to_contain_text('登录安全验证')
                expect(login.get_by_role('button',name='使用同一账号重新扫码',exact=True)).to_be_visible()
                expect(login.get_by_role('button',name='同步网页登录态',exact=True)).to_have_count(0)
                expect(message.get_by_role('alert')).to_contain_text('消息安全验证')
                expect(message.get_by_role('button',name='同步网页登录态',exact=True)).to_be_visible()
                expect(message.get_by_role('button',name='使用同一账号重新扫码',exact=True)).to_have_count(0)
                expect(message.get_by_role('button',name='检查连接',exact=True)).to_be_visible()
                expect(credentials.get_by_role('alert')).to_contain_text('凭证或令牌异常')
                expect(credentials.get_by_role('button',name='编辑账号凭证',exact=True)).to_be_visible()
                assert '预计' not in page.locator('body').inner_text()
                for width in (375,768,1280,1440):
                    page.set_viewport_size({'width':width,'height':1100});page.wait_for_timeout(250)
                    for card in cards.values():
                        for control in card.get_by_role('button').all():
                            if control.is_visible():
                                b=control.bounding_box();assert b['x']>=0 and b['x']+b['width']<=width+1
                    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+2')
                    page.screenshot(path=str(args.output/f'accounts-{width}.png'),full_page=True)
                for dark in (True,False):
                    page.get_by_role('radio',name='使用深色主题' if dark else '使用浅色主题',exact=True).click()
                    page.wait_for_timeout(200)
                    page.screenshot(path=str(args.output/f'accounts-{"dark" if dark else "light"}.png'),full_page=True)
                credentials.get_by_role('button',name='查看诊断',exact=True).click()
                dialog=page.get_by_role('dialog',name='凭证或令牌异常',exact=True)
                expect(dialog).to_be_visible()
                expect(dialog).to_contain_text('签名凭证缺失')
                page.keyboard.press('Escape');expect(dialog).to_be_hidden()
                message.get_by_role('button',name='同步网页登录态',exact=True).click()
                dialog=page.get_by_role('dialog',name='同步已验证的网页登录态',exact=True)
                expect(dialog).to_be_visible()
                expect(dialog).to_contain_text('消息')
                page.keyboard.press('Escape');expect(dialog).to_be_hidden()
                report['checks'].append('登录/消息/凭证故障准确提示与操作；375/768/1280/1440控件边界；浅深色；诊断与同步弹窗键盘操作')
                page.get_by_role('button',name='通知与日志',exact=True).click()
                page.get_by_role('tab',name='账号通知',exact=True).click()
                page.get_by_role('button',name='新建规则',exact=True).click()
                dialog=page.get_by_role('dialog')
                definitions=get_event_definitions();defaults=[x for x in definitions if x['default_subscribed']]
                expect(dialog.locator('button[aria-pressed="true"]')).to_have_count(len(defaults))
                for definition in definitions:
                    # Match the exact label, avoiding recovery/failure label overlap.
                    control=dialog.locator('button[aria-pressed]').filter(has_text=definition['description'])
                    expect(control).to_have_attribute('aria-pressed',str(definition['default_subscribed']).lower())
                dialog.get_by_role('combobox').nth(1).select_option(str(channel))
                dialog.get_by_placeholder('例如：仅错误提醒').fill('默认错误通知')
                dialog.get_by_role('button',name='保存',exact=True).click()
                expect(dialog).to_be_hidden()
                row=db.conn.execute('SELECT event_types FROM message_notifications').fetchone()
                assert set(json.loads(row[0]))=={x['id'] for x in defaults}
                page.get_by_title('编辑规则',exact=True).click()
                dialog=page.get_by_role('dialog')
                dialog.get_by_role('button',name='清空',exact=True).click()
                dialog.get_by_role('button',name='保存',exact=True).click()
                expect(dialog).to_be_hidden()
                assert json.loads(db.conn.execute('SELECT event_types FROM message_notifications').fetchone()[0])==[]
                page.get_by_role('tab',name='通知预览',exact=True).click()
                event_select=page.get_by_label('通知模板事件',exact=True)
                expect(event_select.locator('option')).to_have_count(len(definitions))
                event_select.select_option('message_verification_required')
                frame=page.frame_locator('iframe[title="通知邮件实时预览"]')
                expect(frame.locator('body')).to_contain_text('消息安全验证')
                expect(frame.locator('body')).to_contain_text('官网')
                expect(frame.locator('body')).to_contain_text('建议操作')
                for width in (375,768,1280):
                    page.set_viewport_size({'width':width,'height':1100});page.wait_for_timeout(150)
                    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+2')
                    page.screenshot(path=str(args.output/f'preview-{width}.png'),full_page=True)
                page.get_by_role('tab',name='风控日志',exact=True).click()
                expect(page.get_by_role('heading',name='事件与风控日志',exact=True)).to_be_visible()
                page.get_by_label('事件级别',exact=True).select_option('error')
                expect(page.get_by_text('建议操作：',exact=False).first).to_be_visible()
                page.screenshot(path=str(args.output/'event-log.png'),full_page=True)
                report['checks'].append('18项事件、11项Error默认订阅；真实隔离API保存与空订阅；目录邮件影响/操作；按Error筛选风控日志')
                assert not report['page_errors'] and not report['external_requests'],report
    finally:
        server.should_exit=True;thread.join(timeout=10);sock.close()
    (args.output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
