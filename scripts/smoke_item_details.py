#!/usr/bin/env python3
"""Offline product-detail browser acceptance against an isolated real API/SQLite database."""
import asyncio
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
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    path=Path(os.environ.get('DB_PATH','')).resolve()
    if not path.parent.name.startswith('bygoofish-isolated-') or path.exists():
        parser.error('Use scripts/dev_isolated.sh with a fresh database')
    os.environ['APP_STATIC_DIR']=str(args.stage.resolve())
    import uvicorn
    from app import reply_server
    from app.reply_server import app
    from app.db_manager import db_manager as db
    from app import cookie_manager
    from app.auth import SESSION_TOKENS
    from app.services import item_details
    from app.services.item_details import initialize
    from playwright.sync_api import sync_playwright,expect
    from utils.direct_network import direct_browser_options

    db.conn.execute('UPDATE users SET must_change_password=0');db.conn.commit()
    owner=db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
    account='detail-fixture'
    db.save_cookie(account,'fixture=value',owner)
    cookie_manager.manager=SimpleNamespace(instances={},get_cookie_status=lambda _:False,get_task_state=lambda _:'stopped')
    db.conn.execute("UPDATE cookies SET nickname='隔离商品店铺',remark='测试' WHERE id=?",(account,))
    card={'title':'隔离商品','detail_params':{'itemId':'fixture-item'},'track_params':{'label':'推荐标签'},'card_type':1003}
    db.conn.execute("INSERT INTO item_info(cookie_id,item_id,item_title,item_detail,listing_status,last_seen_at) VALUES(?,?,?,?,'on_sale',CURRENT_TIMESTAMP)",
                    (account,'fixture-item','隔离商品',json.dumps(card)))
    db.conn.commit();initialize(db)
    calls=[]
    behavior={'category':'access_denied','delay':0.4}
    class FixtureInstance:
        async def refresh_item_detail(self,item_id):
            if item_details.claim(db,account,item_id,explicit=True):
                calls.append(item_id)
                await asyncio.sleep(behavior['delay'])
                gen=item_details.generation(db,account)
                category=behavior['category']
                item_details.record_diagnostic(db,account,item_id,gen,category,http_status=403 if category else 200,codes=['SUCCESS'] if not category else [],elapsed_ms=400)
                if category:
                    item_details.failure(db,account,item_id,category,expected_generation=gen)
                else:
                    db.update_item_detail(account,item_id,'自动平台正文\n第二行',source='platform')
            item=db.get_item_info(account,item_id)
            return {'success':item['detail_status']=='ready','requested':True,'item':item}
    async def dispatch(cookie_id,operation,timeout=90):
        assert cookie_id==account
        return await operation(FixtureInstance())
    reply_server._run_on_account_loop=dispatch
    token=secrets.token_urlsafe(32);SESSION_TOKENS[token]={'user_id':owner}
    args.output.mkdir(parents=True,exist_ok=True)
    sock=socket.socket();sock.bind(('127.0.0.1',0))
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=sock.getsockname()[1],log_level='error',access_log=False))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    until=time.monotonic()+10
    while not server.started:
        if time.monotonic()>until:raise RuntimeError('isolated API startup timeout')
        time.sleep(.05)
    base=f'http://127.0.0.1:{sock.getsockname()[1]}'
    errors=[];external=[];checks=[]
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(**direct_browser_options({'headless':True,'channel':'chromium','args':['--no-sandbox']}))
            context=browser.new_context()
            def route_request(route):
                if urlsplit(route.request.url).hostname!='127.0.0.1':
                    external.append('external blocked');route.abort()
                else:route.continue_()
            context.route('**/*',route_request)
            context.add_init_script('localStorage.setItem("auth_token",'+json.dumps(token)+');localStorage.setItem("active_page","items");')
            page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(base,wait_until='networkidle')
            page.get_by_role('button',name='商品与发货',exact=True).click()
            expect(page.get_by_text('未获取',exact=True)).to_be_visible()
            page.get_by_role('button',name='编辑本地详情',exact=True).click()
            textarea=page.get_by_label('本地商品详情')
            expect(textarea).to_have_value('')
            assert 'font-mono' not in textarea.get_attribute('class')
            text='真实商品说明\n支持问答、绘图和写作。\n'+'详细内容。'*80
            textarea.fill(text)
            for theme in ('light','dark'):
                page.evaluate("theme => {document.documentElement.classList.toggle('dark',theme==='dark');document.documentElement.dataset.theme=theme}",theme)
                for width in (375,768,1280):
                    page.set_viewport_size({'width':width,'height':900});page.wait_for_timeout(120)
                    bounds=textarea.bounding_box()
                    assert bounds['x']>=0 and bounds['x']+bounds['width']<=width+1
                    assert textarea.evaluate('(e)=>e.scrollHeight<=e.clientHeight+2')
                    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+2')
                    page.screenshot(path=str(args.output/f'detail-{theme}-{width}.png'),full_page=True)
                    checks.append(f'{theme}/{width}: prose only, controls fit, auto resize')
            page.get_by_role('button',name='保存',exact=True).click()
            expect(page.get_by_text('人工维护',exact=True)).to_be_visible()
            expect(page.get_by_label('本地商品详情')).to_have_count(0)
            assert db.get_item_info(account,'fixture-item')['item_detail']==text.strip()
            page.get_by_role('button',name='编辑本地详情',exact=True).click()
            textarea=page.get_by_label('本地商品详情');expect(textarea).to_have_value(text.strip())
            textarea.fill('');page.get_by_role('button',name='保存',exact=True).click()
            expect(page.get_by_text('人工维护',exact=True)).to_be_visible()
            assert not db.update_item_detail(account,'fixture-item','自动覆盖',source='platform')
            page.reload(wait_until='networkidle')
            expect(page.get_by_text('人工维护',exact=True)).to_be_visible()
            page.get_by_role('button',name='编辑本地详情',exact=True).click()
            expect(page.get_by_label('本地商品详情')).to_have_value('')
            page.get_by_role('button',name='取消',exact=True).click()
            db.conn.execute("UPDATE item_info SET detail_source='unknown',detail_status='blocked',detail_failure='authentication_required' WHERE cookie_id=?",(account,));db.conn.commit()
            page.reload(wait_until='networkidle')
            expect(page.get_by_text('获取失败',exact=True)).to_be_visible()
            page.get_by_role('button',name='编辑本地详情',exact=True).click()
            expect(page.get_by_text('原始原因未细分',exact=False)).to_be_visible()
            assert not calls, 'opening modal must not fetch platform'
            page.get_by_role('button',name='查看诊断',exact=True).click()
            expect(page.get_by_text('当时未采集详细诊断，历史原因未细分。',exact=True)).to_be_visible()
            assert not calls
            page.get_by_role('button',name='检查并获取',exact=True).click()
            expect(page.get_by_text('商品详情访问被拒绝，尚无证据表明需要安全验证；请查看诊断。',exact=True)).to_be_visible()
            assert len(calls)==1
            expect(page.get_by_role('button',name='检查并获取',exact=True)).to_be_disabled()
            page.get_by_role('button',name='查看诊断',exact=True).click()
            expect(page.get_by_text('403',exact=True)).to_be_visible()
            expect(page.get_by_text('access_denied',exact=True)).to_be_visible()
            for theme in ('light','dark'):
                page.evaluate("theme=>{document.documentElement.classList.toggle('dark',theme==='dark');document.documentElement.dataset.theme=theme}",theme)
                for width in (375,768,1280):
                    page.set_viewport_size({'width':width,'height':900})
                    for name in ('检查并获取','查看诊断','取消','保存'):
                        button=page.get_by_role('button',name=name,exact=True)
                        button.scroll_into_view_if_needed();box=button.bounding_box()
                        assert box and box['x']>=0 and box['x']+box['width']<=width+1,(name,box,width)
                    page.screenshot(path=str(args.output/f'diagnostic-{theme}-{width}.png'),full_page=True)
                    checks.append(f'{theme}/{width}: diagnostic, disabled cooldown, actual control bounds')
            assert len(calls)==1
            page.get_by_role('button',name='取消',exact=True).click()
            item_details.credentials_changed(db,account,'fixture=value','fixture=new')
            behavior['category']=''
            page.reload(wait_until='networkidle')
            page.get_by_role('button',name='编辑本地详情',exact=True).click()
            page.get_by_role('button',name='检查并获取',exact=True).click()
            textarea=page.get_by_label('本地商品详情');textarea.fill('尚未保存的人工草稿')
            expect(page.get_by_text('已获取并保存平台商品说明。',exact=True)).to_be_visible()
            expect(textarea).to_have_value('尚未保存的人工草稿')
            assert db.get_item_info(account,'fixture-item')['item_detail']=='自动平台正文\n第二行'
            assert len(calls)==2
            page.get_by_role('button',name='取消',exact=True).click()
            page.get_by_role('button',name='编辑本地详情',exact=True).click()
            expect(page.get_by_label('本地商品详情')).to_have_value('自动平台正文\n第二行')
            page.get_by_role('button',name='取消',exact=True).click()
            # Close/reopen during a late successful operation must not replace a fresh draft.
            db.conn.execute("UPDATE item_info SET item_detail='',detail_source='unknown',detail_status='missing',detail_retry_at=NULL WHERE cookie_id=?",(account,));db.conn.commit()
            behavior['delay']=1
            page.reload(wait_until='networkidle')
            page.get_by_role('button',name='编辑本地详情',exact=True).click()
            page.get_by_role('button',name='检查并获取',exact=True).click()
            page.get_by_role('button',name='取消',exact=True).click()
            page.get_by_role('button',name='编辑本地详情',exact=True).click()
            page.get_by_label('本地商品详情').fill('新弹窗草稿')
            page.wait_for_timeout(1500)
            expect(page.get_by_label('本地商品详情')).to_have_value('新弹窗草稿')
            assert len(calls)==3
            checks.extend(['read-only diagnosis and cancelled modal cause zero platform requests','403 has no unsupported login assertion','cooldown prevents repeat','real API successful prose updates list and modal','unsaved draft preserved','late old-modal response ignored'])
            checks.extend(['real API persists manual prose','explicit clear survives automatic write and reload','blocked acquisition is visible without raw metadata'])
            assert not errors,errors
            assert not external,external
            (args.output/'result.json').write_text(json.dumps({'checks':checks,'page_errors':errors,'external_requests':external},ensure_ascii=False,indent=2))
            browser.close()
    finally:
        server.should_exit=True;thread.join(timeout=10);sock.close()
    print('商品详情隔离浏览器验收通过')


if __name__=='__main__':main()
