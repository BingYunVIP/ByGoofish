#!/usr/bin/env python3
"""Isolated real HTTP/SQLite/browser credential and in-stock list acceptance; no real sends."""
import argparse
import json
import os
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import AsyncMock


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--stage',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    db_path=Path(os.environ.get('DB_PATH','')).resolve()
    if not db_path.parent.name.startswith('bygoofish-isolated-') or db_path.exists():
        p.error('Use scripts/dev_isolated.sh with a fresh database')
    os.environ['APP_STATIC_DIR']=str(args.stage.resolve())
    import uvicorn
    from playwright.sync_api import sync_playwright,expect
    from app.reply_server import app
    from app.db_manager import db_manager as db
    from app import cookie_manager
    from XianyuAutoAsync import XianyuLive
    from utils.direct_network import direct_browser_options
    uid=db.get_user_by_username('admin')['id']
    db.send_verification_email=AsyncMock(return_value=True)
    for name in ('fixture-shop-a','fixture-shop-b'):
        db.save_cookie(name,f'unb={name}; _m_h5_tk=synthetic_1',uid)
        db.conn.execute('UPDATE cookies SET nickname=?,remark=? WHERE id=?',(name,'测试',name))
    db.conn.commit()
    db.batch_save_item_basic_info([{'cookie_id':'fixture-shop-a','item_id':'fixture-item','item_title':'初始在售商品'}],confirmed_on_sale=True)
    cookie_manager.manager=SimpleNamespace(instances={},get_cookie_status=lambda _:False,get_task_state=lambda _:'stopped')
    platform_reads=[]
    async def fake_platform_list(self,*args,**kwargs):
        platform_reads.append(self.cookie_id)
        return {'success':True,'total_count':1,'total_saved':1,'total_pages':1,'group_name':'在售'}
    XianyuLive.get_all_items=fake_platform_list
    args.output.mkdir(parents=True,exist_ok=True)
    sock=socket.socket();sock.bind(('127.0.0.1',0))
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=sock.getsockname()[1],log_level='error',access_log=False))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    until=time.monotonic()+10
    while not server.started:
        if time.monotonic()>until:raise RuntimeError('isolated API startup timeout')
        time.sleep(.05)
    base=f'http://127.0.0.1:{sock.getsockname()[1]}'
    errors=[];external=[];requests=[];checks=[]
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(**direct_browser_options({'headless':True,'channel':'chromium'}))
            context=browser.new_context(viewport={'width':1280,'height':900})
            def intercept(route):
                if urlsplit(route.request.url).hostname!='127.0.0.1':
                    external.append('blocked');route.abort()
                else:route.continue_()
            context.route('**/*',intercept)
            def observe(page):
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('request',lambda r:requests.append((r.method,urlsplit(r.url).path)))
            def login(page,identifier,password):
                page.get_by_placeholder('请输入账号').fill(identifier)
                page.get_by_placeholder('请输入密码').fill(password)
                page.get_by_role('button',name='登录',exact=True).click()
                expect(page.get_by_role('button',name='系统设置',exact=True)).to_be_visible()
            def settings(page):
                page.get_by_role('button',name='系统设置',exact=True).click()
                expect(page.get_by_role('heading',name='修改登录密码',exact=True)).to_be_visible()
            def panel(page,title):
                return page.locator('section').filter(has=page.get_by_role('heading',name=title,exact=True)).last
            page=context.new_page();observe(page);page.goto(base,wait_until='networkidle');login(page,'admin','admin123');settings(page)
            second=context.new_page();observe(second);second.goto(base,wait_until='networkidle');settings(second)
            page.bring_to_front()
            for theme in ('light','dark'):
                page.evaluate("theme=>{document.documentElement.classList.toggle('dark',theme==='dark');document.documentElement.dataset.theme=theme}",theme)
                for width in (375,768,1280):
                    page.set_viewport_size({'width':width,'height':900});page.wait_for_timeout(100)
                    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+2')
                    for field in panel(page,'修改登录密码').locator('input').all()+panel(page,'修改登录邮箱').locator('input').all():
                        b=field.bounding_box();assert b and b['x']>=0 and b['x']+b['width']<=width+1
                    page.screenshot(path=str(args.output/f'settings-{theme}-{width}.png'),full_page=True)
            page.set_viewport_size({'width':1280,'height':900})
            password_panel=panel(page,'修改登录密码')
            password_panel.get_by_label('当前密码',exact=True).fill('admin123')
            password_panel.get_by_label('新密码',exact=True).fill('ChangedPass88')
            password_panel.get_by_label('确认新密码',exact=True).fill('ChangedPass88')
            def commit_before_old_session_error(route):
                response=route.fetch()
                # A background request can observe revocation before this successful response arrives.
                page.evaluate("localStorage.removeItem('auth_token');window.dispatchEvent(new CustomEvent('auth:logout',{detail:{message:'登录状态已失效'}}))")
                route.fulfill(response=response)
            page.route('**/change-password',commit_before_old_session_error)
            password_panel.get_by_role('button',name='修改密码',exact=True).click()
            expect(page.get_by_placeholder('请输入密码')).to_have_value('')
            expect(page.get_by_text('密码已修改，请使用新密码重新登录',exact=True)).to_be_visible()
            expect(second.get_by_placeholder('请输入密码')).to_have_value('')
            checks.append('password commit, persistent notice, cleared inputs and cross-tab logout')
            login(page,'admin','ChangedPass88');settings(page)
            email_panel=panel(page,'修改登录邮箱')
            email_panel.get_by_label('当前密码',exact=True).fill('wrong-password')
            email_panel.get_by_label('新登录邮箱',exact=True).fill('next-owner@example.invalid')
            email_panel.get_by_role('button',name='发送验证码',exact=True).click()
            expect(page.get_by_text('当前密码错误',exact=True)).to_be_visible()
            expect(email_panel.get_by_role('button',name='发送验证码',exact=True)).to_be_enabled()
            email_panel.get_by_label('当前密码',exact=True).fill('ChangedPass88')
            email_panel.get_by_role('button',name='发送验证码',exact=True).click()
            expect(email_panel.get_by_role('button',name='秒后重发',exact=False)).to_be_disabled()
            code=db.conn.execute("SELECT code FROM email_verifications WHERE email='next-owner@example.invalid' ORDER BY id DESC LIMIT 1").fetchone()[0]
            email_panel.get_by_label('新邮箱验证码',exact=True).fill(code)
            email_panel.get_by_role('button',name='修改登录邮箱',exact=True).click()
            expect(page.get_by_placeholder('请输入账号')).to_have_value('next-owner@example.invalid')
            expect(page.get_by_placeholder('请输入密码')).to_have_value('')
            expect(page.get_by_text('登录邮箱已修改，请使用新邮箱重新登录',exact=True)).to_be_visible()
            login(page,'next-owner@example.invalid','ChangedPass88');settings(page)
            expect(panel(page,'修改登录邮箱').get_by_label('当前登录邮箱')).to_have_value('next-owner@example.invalid')
            checks.append('email code cooldown, change, email/password login, latest profile')
            # Hold an old authorized Axios response, then install a newer real session before 401.
            pending=[]
            stale=context.new_page();observe(stale)
            stale.route('**/account/profile',lambda route:pending.append(route))
            stale.goto(base,wait_until='domcontentloaded');settings(stale)
            until=time.monotonic()+5
            while not pending:
                if time.monotonic()>until:raise RuntimeError('missing held profile request')
                stale.wait_for_timeout(50)
            newer=page.request.post(base+'/login',data={'email':'next-owner@example.invalid','password':'ChangedPass88'}).json()['token']
            stale.evaluate('(token)=>localStorage.setItem("auth_token",token)',newer)
            pending[0].fulfill(status=401,content_type='application/json',body='{"detail":"expired old session"}')
            stale.wait_for_timeout(150)
            assert stale.evaluate('localStorage.getItem("auth_token")')==newer
            expect(stale.get_by_role('button',name='系统设置',exact=True)).to_be_visible()
            stale.close();checks.append('late 401 cannot remove a newer authenticated session')
            page.bring_to_front();page.get_by_role('button',name='商品与发货',exact=True).click()
            expect(page.get_by_text('初始在售商品',exact=True)).to_be_visible()
            db.batch_save_item_basic_info([{'cookie_id':'fixture-shop-b','item_id':'new-item','item_title':'新增在售商品'}],confirmed_on_sale=True)
            expect(page.get_by_text('新增在售商品',exact=True)).to_be_visible(timeout=20000)
            db.reconcile_item_listing_status('fixture-shop-a',[])
            page.evaluate('window.dispatchEvent(new Event("focus"))')
            expect(page.get_by_text('初始在售商品',exact=True)).to_have_count(0)
            page.get_by_role('button',name='同步商品',exact=True).click()
            expect(page.get_by_text('在售商品已更新（2 个账号）',exact=True)).to_be_visible()
            assert platform_reads==['fixture-shop-a','fixture-shop-b']
            # A hidden tab performs no local refresh or platform polling.
            page.evaluate('Object.defineProperty(document,"visibilityState",{configurable:true,get:()=>"hidden"})')
            before=len([r for r in requests if r==('GET','/items')])
            page.evaluate('window.dispatchEvent(new Event("focus"))');page.wait_for_timeout(150)
            assert len([r for r in requests if r==('GET','/items')])==before
            checks.append('15-second in-stock refresh, focused add/remove without reload, all-account sync and hidden-tab pause')
            assert not errors,errors
            assert not external,external
            (args.output/'result.json').write_text(json.dumps({'checks':checks,'page_errors':errors,'external_requests':external,'simulated_mail_submissions':db.send_verification_email.await_count},ensure_ascii=False,indent=2))
            browser.close()
    finally:
        server.should_exit=True;thread.join(timeout=10);sock.close()
    print('credential_and_in_stock_browser_acceptance_passed')


if __name__=='__main__':main()
