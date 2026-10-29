#!/usr/bin/env python3
"""Isolated actual HTTP/SQLite/Chromium message actions; platform frames are fixtures."""
import argparse
import json
import os
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import AsyncMock


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--stage',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    dbpath=Path(os.environ.get('DB_PATH','')).resolve()
    if not dbpath.parent.name.startswith('bygoofish-isolated-') or dbpath.exists():p.error('Use fresh dev_isolated.sh')
    os.environ['APP_STATIC_DIR']=str(args.stage.resolve())
    import uvicorn
    from playwright.sync_api import sync_playwright,expect
    from app import reply_server as server_module,cookie_manager
    from app.db_manager import db_manager as db
    from app.services.message_actions import custom_content
    from XianyuAutoAsync import ConnectionState
    from utils.direct_network import direct_browser_options
    uid=db.get_user_by_username('admin')['id'];account='actions-fixture'
    for name in (account,'other-fixture'):db.save_cookie(name,'unb=fixture',uid)
    db.conn.execute("UPDATE chat_native_capabilities SET status='verified',reason=''");db.conn.commit()
    now=int(time.time()*1000)
    def message(mid,text,sender='buyer',cid='source-chat'):
        return {'message':{'messageId':mid,'cid':cid+'@goofish','sender':sender,'createAt':now-10000,
            'extension':{'senderUserId':sender,'senderNick':'测试买家' if sender=='buyer' else '目标买家'},'content':custom_content(text=text)}}
    original=message('source','你好[微笑]，测试引用与转发')
    image=message('image','');image['message']['content']=custom_content(image={'url':'/static/fixture-image.png','width':10,'height':10})
    event=message('event','[我已付款，等待你发货]')
    histories={'source-chat':[original,image,event], 'target-chat':[message('target','目标会话内容','target-buyer','target-chat')]}
    def conv(cid,buyer,nick):
        return {'singleChatUserConversation':{'modifyTime':now,'singleChatConversation':{'cid':cid+'@goofish','pairFirst':account,'pairSecond':buyer},'lastMessage':message('last',nick,buyer,cid)}}
    live=SimpleNamespace(cookie_id=account,myid=account,cookies_str='unb=fixture',connection_state=ConnectionState.CONNECTED,ws=SimpleNamespace(closed=False))
    async def history(cid,*a,**k):return {'userMessageModels':list(reversed(histories[cid])),'hasMore':False}
    live.get_im_messages=history
    live.get_im_conversations=AsyncMock(return_value={'userConvs':[conv('source-chat','buyer','测试买家'),conv('target-chat','target-buyer','目标买家')],'hasMore':False})
    frames=[]
    async def wire(lwp,body):
        frames.append({'lwp':lwp,'body':body})
        if lwp.endswith('batchForward'):
            entry=body[0]['forwardMessages'][0];cid=body[0]['toCid'].split('@')[0]
            record=message('forward-result','转发已确认',account,cid);record['message']['content']=entry['content'];record['message']['extension'].update(entry['extension']);histories[cid].append(record)
            return {'code':200,'body':{'sendResults':{entry['uuid']:{'messageId':'forward-result'}}}}
        record=message('reply-result','',account);record['message']['content']=body[0]['content'];record['message']['extension'].update(body[0]['extension']);histories['source-chat'].append(record)
        return {'code':200,'body':{'messageId':'reply-result'}}
    live._send_im_request=wire
    live.send_im_text=AsyncMock(return_value={'code':200,'body':{'messageId':'ordinary'}})
    cookie_manager.manager=SimpleNamespace(instances={account:live},get_cookie_status=lambda _:True,get_task_state=lambda _:'running')
    async def run(account,operation,**kwargs):return await operation(live)
    server_module._run_on_account_loop=run
    db.conn.execute('INSERT INTO chat_emoji_cache VALUES(?,?,?,0)',(account,json.dumps([{'alias':'[微笑]','name':'微笑','url':'/static/fixture-emoji.png'}]),time.time()));db.conn.commit()
    sock=socket.socket();sock.bind(('127.0.0.1',0));server=uvicorn.Server(uvicorn.Config(server_module.app,host='127.0.0.1',port=sock.getsockname()[1],log_level='error',access_log=False));thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    until=time.monotonic()+10
    while not server.started:
        if time.monotonic()>until:raise RuntimeError('startup timeout')
        time.sleep(.05)
    args.output.mkdir(parents=True,exist_ok=True);errors=[];external=[];checks=[]
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(**direct_browser_options({'headless':True,'channel':'chromium'}));context=browser.new_context(viewport={'width':1280,'height':900},permissions=['clipboard-read','clipboard-write'])
            def restrict(route):
                if urlsplit(route.request.url).hostname!='127.0.0.1':external.append('blocked');route.abort()
                elif '/static/fixture-' in route.request.url:route.fulfill(status=200,content_type='image/png',body=__import__('base64').b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aPZsAAAAASUVORK5CYII='))
                else:route.continue_()
            context.route('**/*',restrict)
            page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)));base=f'http://127.0.0.1:{sock.getsockname()[1]}'
            page.goto(base,wait_until='networkidle');page.get_by_placeholder('请输入账号').fill('admin');page.get_by_placeholder('请输入密码').fill('admin123');page.get_by_role('button',name='登录',exact=True).click()
            page.get_by_role('button',name='消息中心',exact=True).click()
            page.get_by_role('button',name='测试买家',exact=False).first.click()
            original_node=page.locator('[data-message-id="source"]');expect(original_node).to_be_visible()
            expect(original_node.locator('img[alt="[微笑]"]')).to_be_visible()
            page.get_by_role('button',name='选择表情',exact=True).click()
            emoji=page.get_by_role('button',name='表情 微笑',exact=True);expect(emoji.locator('img')).to_be_visible();emoji.click()
            composer=page.get_by_placeholder('输入消息，或粘贴／拖入图片…');expect(composer).to_have_value('[微笑]')
            original_node.click(button='right');menu=page.get_by_role('menu',name='消息操作');expect(menu).to_be_visible();menu.get_by_role('menuitem',name='复制文字',exact=True).click()
            assert page.evaluate('navigator.clipboard.readText()')=='你好[微笑]，测试引用与转发'
            checks.append('official image in panel/body, original code insertion and clipboard copy')
            original_node.click(button='right');menu.get_by_role('menuitem',name='引用回复',exact=True).click()
            expect(page.get_by_label('正在引用回复')).to_be_visible();composer.fill('原生引用功能验收')
            page.get_by_role('button',name='发送',exact=True).click()
            expect(page.locator('[data-message-id="reply-result"]')).to_be_visible()
            expect(page.get_by_label('正在引用回复')).to_have_count(0)
            assert frames[0]['body'][0]['content']['contentType']==101
            assert frames[0]['body'][0]['extension']['replyMessageId']=='source'
            expect(page.locator('[data-message-id="reply-result"]').get_by_role('button',name='定位引用原消息')).to_be_visible()
            checks.append('native quote request and receipt/history merge with quote display')
            original_node.click(button='right');menu.get_by_role('menuitem',name='转发',exact=True).click()
            dialog=page.get_by_role('dialog',name='转发消息');expect(dialog).to_be_visible()
            before=len(frames);dialog.get_by_role('option',name='目标买家',exact=False).click();assert len(frames)==before
            dialog.get_by_role('button',name='确认转发',exact=True).click();expect(dialog).to_be_hidden();assert len(frames)==before+1
            checks.append('one-account target selection and explicit confirmation before native forwarding')
            # The source needs a scoped draft after reference selection, including refresh.
            original_node.click(button='right');menu.get_by_role('menuitem',name='引用回复',exact=True).click()
            composer.fill('保留引用草稿');page.reload(wait_until='domcontentloaded');page.get_by_role('button',name='消息中心',exact=True).click();page.get_by_role('button',name='测试买家',exact=False).first.click()
            expect(page.get_by_label('正在引用回复')).to_be_visible();expect(composer).to_have_value('保留引用草稿')
            page.get_by_role('button',name='目标买家',exact=False).first.click();expect(page.get_by_label('正在引用回复')).to_have_count(0)
            page.get_by_role('button',name='测试买家',exact=False).first.click();expect(page.get_by_label('正在引用回复')).to_be_visible()
            page.get_by_role('button',name='取消引用回复',exact=True).click()
            checks.append('quote draft persists refresh and is isolated between conversations')
            for theme in ('light','dark'):
                page.evaluate("t=>{document.documentElement.classList.toggle('dark',t==='dark');document.documentElement.dataset.theme=t}",theme)
                for width in (375,768,1280,1440):
                    page.set_viewport_size({'width':width,'height':900});page.wait_for_timeout(120)
                    for x,y in [(2,2),(width-2,2),(2,898),(width-2,898)]:
                        original_node.evaluate('(element,point)=>element.dispatchEvent(new MouseEvent("contextmenu",{bubbles:true,cancelable:true,clientX:point.x,clientY:point.y}))',{'x':x,'y':y})
                        expect(menu).to_be_visible();page.wait_for_timeout(60);box=menu.bounding_box();assert box and box['x']>=0 and box['y']>=0 and box['x']+box['width']<=width+1 and box['y']+box['height']<=901, (width,theme,x,y,box,menu.evaluate('e=>({style:e.getAttribute("style"),position:getComputedStyle(e).position,top:getComputedStyle(e).top,left:getComputedStyle(e).left,transform:getComputedStyle(document.body).transform,sheets:[...document.styleSheets].map(s=>s.href)})'))
                        page.keyboard.press('ArrowDown');assert menu.evaluate('e=>e.contains(document.activeElement)')
                        page.keyboard.press('Escape');expect(menu).to_be_hidden()
                    original_node.get_by_role('button',name='消息更多操作').click(force=True)
                    expect(menu).to_be_visible();page.screenshot(path=str(args.output/f'menu-{theme}-{width}.png'),full_page=True);page.keyboard.press('Escape')
            checks.append('all four menu corners, keyboard, mobile more, 375/768/1280/1440 light/dark')
            original_node.click(button='right');menu.get_by_role('menuitem',name='引用回复',exact=True).focus();page.keyboard.press('Escape')
            composer.click(button='right');expect(menu).to_be_hidden()
            page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw Error('denied')}}})")
            original_node.click(button='right');menu.get_by_role('menuitem',name='复制文字',exact=True).click()
            expect(menu.get_by_role('textbox')).to_have_value('你好[微笑]，测试引用与转发');page.keyboard.press('Escape')
            checks.append('composer native context menu preserved and clipboard failure offers selectable text')

            page.set_viewport_size({'width':1280,'height':900})
            event_node=page.locator('[data-message-id="event"]');event_node.click(button='right');expect(menu.get_by_role('menuitem',name='引用回复')).to_be_disabled();expect(menu.get_by_role('menuitem',name='转发')).to_be_disabled();page.keyboard.press('Escape')
            assert not errors,errors;assert not external,external
            (args.output/'result.json').write_text(json.dumps({'checks':checks,'page_errors':errors,'external_requests':external,'simulated_native_sends':len(frames)},ensure_ascii=False,indent=2));browser.close()
    finally:server.should_exit=True;thread.join(timeout=10);sock.close()
    print('message_actions_browser_acceptance_passed')


if __name__=='__main__':main()
