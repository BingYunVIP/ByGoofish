#!/usr/bin/env python3
"""隔离订单买家信息及风控长事件布局冒烟；所有外部请求均拒绝。"""
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    path = Path(os.environ.get('DB_PATH', '')).resolve()
    if not path.parent.name.startswith('bygoofish-isolated-') or path.exists():
        parser.error('必须使用scripts/dev_isolated.sh和全新隔离数据库')
    if not (args.stage / 'index.html').is_file():
        parser.error('构建目录缺少index.html')
    os.environ['APP_STATIC_DIR'] = str(args.stage.resolve())
    import uvicorn
    from playwright.sync_api import sync_playwright, expect
    from app.reply_server import app
    from app import cookie_manager
    from app.db_manager import db_manager
    from app.auth import SESSION_TOKENS
    from utils.direct_network import direct_browser_options

    owner = db_manager.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
    db_manager.save_cookie('buyer-smoke-account', 'fixture=value', owner)
    cookie_manager.manager = SimpleNamespace(instances={},get_cookie_status=lambda _:True,get_task_state=lambda _:'stopped')
    address = '示例省示例市示例区示例街道很长的测试地址一百号虚构收件楼101室'
    db_manager.insert_or_update_order('buyer-smoke-order',cookie_id='buyer-smoke-account',item_id='buyer-smoke-item',
        buyer_id='buyer-smoke-id',buyer_nick='测试买家昵称',receiver_name='测试收件人',receiver_phone='13800000000',
        receiver_address=address,quantity='1',amount='5.00',order_status='completed')
    db_manager.insert_or_update_order('buyer-smoke-missing',cookie_id='buyer-smoke-account',buyer_id='buyer-smoke-id',order_status='completed')
    for event in ['manual_delivery_verified', 'manual_delivery_verified_' * 9]:
        db_manager.add_risk_control_log('buyer-smoke-account',event,event_description='长事件名称布局测试',
                                      processing_status='success',processing_result='状态正常，详情始终可见')
    db_manager.conn.execute("UPDATE risk_control_logs SET created_at='2026-10-20 11:52:05'")
    db_manager.conn.commit()
    token=secrets.token_urlsafe(32);SESSION_TOKENS[token]={'user_id':owner}
    args.output.mkdir(parents=True,exist_ok=True)
    sock=socket.socket();sock.bind(('127.0.0.1',0));base=f'http://127.0.0.1:{sock.getsockname()[1]}'
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=sock.getsockname()[1],log_level='error',access_log=False))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    deadline=time.monotonic()+10
    while not server.started:
        if time.monotonic()>deadline:raise RuntimeError('isolated server startup timeout')
        time.sleep(.05)
    errors=[];external=[];mutations=[];checks=[]
    try:
        with sync_playwright() as pw:
            with pw.chromium.launch(**direct_browser_options({'headless':True})) as browser:
                context=browser.new_context(viewport={'width':1440,'height':1000},timezone_id='Asia/Singapore',reduced_motion='reduce')
                def intercept(route):
                    request=route.request
                    if urlsplit(request.url).hostname!='127.0.0.1':external.append('external');route.abort();return
                    if request.method!='GET':mutations.append(urlsplit(request.url).path);route.abort();return
                    route.continue_()
                context.route('**/*',intercept)
                context.add_init_script('localStorage.setItem("auth_token",'+json.dumps(token)+');localStorage.setItem("active_page","orders");')
                page=context.new_page();page.on('pageerror',lambda e:errors.append(type(e).__name__))
                page.goto(base,wait_until='networkidle')
                expect(page.get_by_role('button', name='刷新订单', exact=True)).to_have_count(0)
                for name in ['刷新状态', '拉取卖出订单']:
                    expect(page.get_by_role('button', name=name, exact=True).locator('svg.lucide-refresh-cw')).to_have_count(1)
                checks.append('redundant order refresh removed; both sync buttons use the same refresh icon')
                row=page.get_by_role('row').filter(has=page.get_by_text('订单ID: buyer-smoke-order',exact=True))
                expect(row).to_be_visible()
                for label,value in [('买家昵称','测试买家昵称'),('买家ID','buyer-smoke-id'),('收货人','测试收件人'),('联系电话','13800000000'),('收货地址',address)]:
                    expect(row.get_by_text(label+'：'+value,exact=True)).to_be_visible()
                address_cell=row.get_by_text('收货地址：'+address,exact=True)
                assert address_cell.evaluate('(e)=>e.scrollHeight<=e.clientHeight+1')
                page.screenshot(path=str(args.output/'buyer-desktop.png'),full_page=True)
                page.get_by_placeholder('搜索订单号/商品/买家...').fill('测试买家昵称')
                expect(row).to_be_visible();expect(page.get_by_text('订单ID: buyer-smoke-missing',exact=True)).to_have_count(0)
                checks.append('all five real fixture fields; full address wraps; nickname search; no automatic POST on refresh')
                page.get_by_placeholder('搜索订单号/商品/买家...').fill('')
                page.get_by_role('button',name='通知与日志',exact=True).click()
                page.get_by_role('tab',name='风控日志',exact=True).click()
                expect(page.get_by_text('manual_delivery_verified',exact=True)).to_be_visible()
                expect(page.get_by_text('2026-10-20 19:52:05',exact=True).first).to_be_visible()
                for width in [1440,1100,375]:
                    page.set_viewport_size({'width':width,'height':1000})
                    if width<1024:page.wait_for_function("document.querySelector('aside').getBoundingClientRect().right <= 1")
                    for event in ['manual_delivery_verified','manual_delivery_verified_'*9]:
                        label=page.get_by_text(event,exact=True)
                        expect(label).to_be_visible()
                        assert label.evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
                        bounds=label.bounding_box();assert bounds and bounds['x']>=0 and bounds['x']+bounds['width']<=width+1
                    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+1')
                    page.screenshot(path=str(args.output/f'risk-{width}.png'),full_page=True)
                page.emulate_media(color_scheme='dark',reduced_motion='reduce')
                expect(page.locator('html')).to_have_attribute('data-theme','dark')
                page.screenshot(path=str(args.output/'risk-mobile-dark.png'),full_page=True)
                # 新建窄屏页面，确认五项买家信息在卡片布局中均保留。
                page.reload(wait_until='networkidle')
                expect(row).to_be_visible()
                expect(row.get_by_text('收货地址：'+address,exact=True)).to_be_visible()
                page.screenshot(path=str(args.output/'buyer-mobile-dark.png'),full_page=True)
                checks.append('content sized event column; long identifiers; desktop narrow/mobile; dark; buyer mobile')
                assert not errors and not external and not mutations,(errors,external,mutations)
    finally:
        SESSION_TOKENS.pop(token,None);server.should_exit=True;thread.join(timeout=10)
    result={'mode':'isolated; no platform traffic or external sends','checks':checks,'page_errors':errors,'external_requests':external,'mutation_requests':mutations}
    (args.output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
