#!/usr/bin/env python3
"""Isolated HTTP/SQLite/Chromium automatic-reply acceptance. No external sends."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import asyncio
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
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    db_path=Path(os.environ.get('DB_PATH','')).resolve()
    if not db_path.parent.name.startswith('bygoofish-isolated-') or db_path.exists():
        parser.error('Use scripts/dev_isolated.sh with a fresh database')
    os.environ['APP_STATIC_DIR']=str(args.stage.resolve())
    import uvicorn
    from playwright.sync_api import sync_playwright,expect
    from app.reply_server import app
    from app.db_manager import db_manager as db
    from app import cookie_manager
    from app.services import auto_reply as replies
    from app.services.chat_workspace import set_handover
    from XianyuAutoAsync import XianyuLive, ConnectionState
    from utils.direct_network import direct_browser_options
    uid=db.get_user_by_username('admin')['id']
    account='fixture-reply-shop'
    for name in (account,'fixture-reply-peer'):
        db.save_cookie(name,'unb=synthetic',uid)
        db.conn.execute('UPDATE cookies SET nickname=?,remark=? WHERE id=?',(name,'测试',name))
    db.conn.commit()
    db.save_default_reply(account,True,'初始文案',False,'https://fixture.example/reply.png')
    live=XianyuLive.__new__(XianyuLive)
    live.cookie_id=live.myid=account
    live.connection_state=ConnectionState.CONNECTED
    live.background_tasks=set();live.message_debounce_delay=0
    live.get_ai_reply=AsyncMock(return_value=None)
    live.send_im_text=AsyncMock(return_value={'code':200,'body':{'messageId':'fixture-text'}})
    live.send_im_image=AsyncMock(return_value={'code':200,'body':{'messageId':'fixture-image'}})
    live._get_image_size_from_url=AsyncMock(return_value=(100,100))
    cookie_manager.manager=SimpleNamespace(instances={account:live},get_cookie_status=lambda _:True,get_task_state=lambda _:'running')
    async def seed(text,mid):
        await replies.accept(live,db,{'cid':'fixture-chat','senderId':'fixture-reply-peer','senderName':'测试买家',
            'text':text,'time':int((time.time()+1)*1000),'messageId':mid,'type':'text'})
        while live.background_tasks:
            await asyncio.gather(*list(live.background_tasks));await asyncio.sleep(0)
    def run_seed(text, mid):
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run,seed(text,mid)).result(timeout=10)
    run_seed('正常来信','first')
    sock=socket.socket();sock.bind(('127.0.0.1',0))
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=sock.getsockname()[1],log_level='error',access_log=False))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    until=time.monotonic()+10
    while not server.started:
        if time.monotonic()>until:raise RuntimeError('isolated API startup timeout')
        time.sleep(.05)
    args.output.mkdir(parents=True,exist_ok=True)
    errors=[];external=[];checks=[]
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(**direct_browser_options({'headless':True,'channel':'chromium'}))
            context=browser.new_context(viewport={'width':1280,'height':900})
            def restrict(route):
                if urlsplit(route.request.url).hostname!='127.0.0.1':external.append('blocked');route.abort()
                else:route.continue_()
            context.route('**/*',restrict)
            page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
            base=f'http://127.0.0.1:{sock.getsockname()[1]}'
            page.goto(base,wait_until='networkidle')
            page.get_by_placeholder('请输入账号').fill('admin');page.get_by_placeholder('请输入密码').fill('admin123')
            page.get_by_role('button',name='登录',exact=True).click()
            page.get_by_role('button',name='自动回复',exact=True).click()
            page.get_by_role('tab',name='账号默认回复',exact=False).click()
            activity=page.get_by_role('region',name='自动回复处理记录')
            expect(activity.get_by_text('正常来信',exact=True)).to_be_visible()
            expect(activity.get_by_text('平台已确认发送',exact=True)).to_be_visible()
            page.get_by_title('编辑默认回复',exact=True).first.click()
            page.get_by_placeholder('输入默认回复内容').fill('浏览器保存的新文案')
            page.get_by_placeholder('https://example.com/image.jpg').fill('https://fixture.example/latest.png')
            def save(route):
                if route.request.method!='PUT':route.continue_();return
                expect(page.get_by_placeholder('输入默认回复内容')).to_be_disabled()
                expect(page.get_by_role('button',name='保存中…',exact=True)).to_be_disabled()
                route.fulfill(response=route.fetch())
            page.route('**/default-replies/'+account,save)
            page.get_by_role('button',name='保存',exact=True).click()
            expect(page.get_by_text('配置已保存，对新来信生效',exact=True)).to_be_visible()
            assert db.get_default_reply(account)['reply_content']=='浏览器保存的新文案'
            page.unroute('**/default-replies/'+account,save)
            checks.append('real HTTP save and readback, busy lock, persisted newest content')
            live.send_im_image.side_effect=TimeoutError('fixture')
            run_seed('图片回执超时，文字已被平台确认。'*12,'unknown')
            set_handover(db,account,'fixture-chat',time.time()+100)
            run_seed('人工接管期间的新来信','paused')
            page.evaluate('document.dispatchEvent(new Event("visibilitychange"))')
            expect(activity.get_by_text('结果待核查，请勿重复发送',exact=True)).to_be_visible()
            expect(activity.get_by_text('人工接管中',exact=True)).to_be_visible()
            expect(activity.get_by_text('文字：已发送',exact=True)).to_have_count(2)
            expect(activity.get_by_text('图片：待核查',exact=True)).to_be_visible()
            checks.append('per-part result, unknown never claims success, handover reason/countdown')
            for theme in ('light','dark'):
                page.evaluate("t=>{document.documentElement.classList.toggle('dark',t==='dark');document.documentElement.dataset.theme=t}",theme)
                for width in (375,768,1280):
                    page.set_viewport_size({'width':width,'height':900});page.wait_for_timeout(100)
                    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+2')
                    for card in activity.locator('article').all():
                        box=card.bounding_box();assert box and box['x']>=0 and box['x']+box['width']<=width+1
                    page.screenshot(path=str(args.output/f'reply-{theme}-{width}.png'),full_page=True)
            page.set_viewport_size({'width':1280,'height':900})
            page.get_by_title('编辑默认回复',exact=True).first.click()
            expect(page.get_by_placeholder('输入默认回复内容')).to_have_value('浏览器保存的新文案')
            page.route('**/default-replies/'+account,lambda route:route.fulfill(json={'success':False}) if route.request.method=='PUT' else route.continue_())
            page.get_by_role('button',name='保存',exact=True).click()
            expect(page.get_by_text('保存结果未确认：服务端未确认保存成功',exact=True)).to_be_visible()
            expect(page.get_by_placeholder('输入默认回复内容')).to_be_visible()
            checks.append('reopened current configuration and explicit failure without fake success')
            page.get_by_role('button',name='取消',exact=True).click()
            page.locator('#reply-account').select_option('fixture-reply-peer')
            expect(activity.get_by_text('暂无新消息处理记录',exact=True)).to_be_visible()
            expect(activity.get_by_text('正常来信',exact=True)).to_have_count(0)
            assert not errors,errors
            assert not external,external
            checks.append('account-scoped activity and 375/768/1280 light/dark actual bounds')
            (args.output/'result.json').write_text(json.dumps({'checks':checks,'page_errors':errors,'external_requests':external,
                'simulated_text_sends':live.send_im_text.await_count,'simulated_image_sends':live.send_im_image.await_count},ensure_ascii=False,indent=2))
            browser.close()
    finally:
        server.should_exit=True;thread.join(timeout=10);sock.close()
    print('auto_reply_browser_acceptance_passed')


if __name__=='__main__':main()
