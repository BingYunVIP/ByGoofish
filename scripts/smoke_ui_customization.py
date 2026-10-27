#!/usr/bin/env python3
"""隔离数据库 + 构建产物浏览器冒烟；IM 使用固定离线数据，不访问真实账号。

scripts/dev_isolated.sh scripts/smoke_ui_customization.py --stage /tmp/build --output Custom/ui-smoke
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import shlex
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs
from zoneinfo import ZoneInfo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    db_path = Path(os.environ.get('DB_PATH', '')).resolve()
    if not db_path.parent.name.startswith('bygoofish-isolated-') or db_path.exists():
        parser.error('必须通过 scripts/dev_isolated.sh 在全新的一次性数据库中运行')
    if not (args.stage / 'index.html').is_file():
        parser.error('构建目录缺少 index.html')
    os.environ['APP_STATIC_DIR'] = str(args.stage.resolve())

    import uvicorn
    from playwright.sync_api import sync_playwright, expect
    from app.reply_server import app
    from app import cookie_manager
    from app.db_manager import db_manager
    from app.auth import SESSION_TOKENS
    from utils.direct_network import direct_browser_options
    from PIL import Image

    owner = db_manager.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
    db_manager.save_cookie('smoke-account', 'fixture=value', owner)
    db_manager.conn.execute("UPDATE cookies SET nickname='冒烟昵称',remark='冒烟备注' WHERE id='smoke-account'")
    db_manager.save_cookie('smoke-off-shelf', 'fixture=value', owner)
    db_manager.conn.execute("UPDATE cookies SET nickname='下架账号昵称',remark='小号' WHERE id='smoke-off-shelf'")
    # 仅提供本地账号状态供表单保存，不创建监听实例或账号请求任务。
    connected_instance = SimpleNamespace(ws=SimpleNamespace(closed=False), connection_state='connected', myid='smoke-account')
    cookie_manager.manager = SimpleNamespace(instances={'smoke-account': connected_instance}, get_cookie_status=lambda _: True,
                                            get_task_state=lambda _: 'stopped', update_cookie=lambda *args, **kwargs: None)
    db_manager.conn.execute("INSERT INTO item_info(cookie_id,item_id,item_title,item_price,item_detail,listing_status,last_seen_at) VALUES ('smoke-account','smoke-item','冒烟商品','10','简短说明','on_sale',CURRENT_TIMESTAMP)")
    db_manager.conn.execute("INSERT INTO item_info(cookie_id,item_id,item_title,item_price,item_detail,listing_status) VALUES ('smoke-off-shelf','old-item','已下架商品','9','旧商品','off_shelf')")
    for index in range(4):
        db_manager.conn.execute("INSERT INTO item_info(cookie_id,item_id,item_title,item_price,item_detail) VALUES ('smoke-account',?,?, '1','历史订单资料')",(f'history-{index}',f'历史订单商品{index}'))
    notification_channel_id = db_manager.create_notification_channel(
        '隔离冒烟渠道', 'email', json.dumps({'smtp_server': 'smtp.example.invalid', 'smtp_port': 465,
                                             'email_user': 'smoke@example.invalid', 'email_password': 'local-fixture',
                                             'recipient_email': 'smoke@example.invalid'}), owner)
    db_manager.set_message_notification('smoke-account', notification_channel_id, True,
                                        name='冒烟账号规则', event_types=['login_recovered'])
    phrase_image_dir = args.stage / 'uploads' / 'images'
    phrase_image_dir.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (24, 16), color=(255, 225, 0)).save(phrase_image_dir / 'smoke.jpg', format='JPEG')
    db_manager.create_quick_phrase('图片话术', '图片回复', '冒烟', 0, 'static/uploads/images/smoke.jpg')
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for label, days in [('today', 0), ('yesterday', 1), ('old', 20)]:
        created = (today - timedelta(days=days) + timedelta(hours=1)).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        db_manager.conn.execute("INSERT INTO orders(order_id,cookie_id,item_id,buyer_id,quantity,amount,order_status,created_at) VALUES (?, 'smoke-account','smoke-item','smoke-buyer',1,'10','completed',?)",
                                (f'smoke-{label}', created))
    db_manager.conn.commit()
    token = secrets.token_urlsafe(32)
    SESSION_TOKENS[token] = {'user_id': owner}
    args.output.mkdir(parents=True, exist_ok=True)

    # 需要同进程初始化的 SQLite/会话夹具；不启动第二个 Start.py 或任何账号任务。
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=sock.getsockname()[1], log_level='error', access_log=False))
    base = f'http://127.0.0.1:{sock.getsockname()[1]}'
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() >= deadline:
            raise RuntimeError('隔离冒烟服务启动超时')
        time.sleep(0.05)
    errors, external_requests, message_requests, order_requests = [], [], [], []
    conversation_requests = []
    notification_test_requests = []
    recovery_requests = []
    result = {'mode': 'isolated-database; IM fixtures; no real notification send', 'checks': []}
    try:
        with sync_playwright() as pw:
            with pw.chromium.launch(**direct_browser_options({'headless': True})) as browser:
                context = browser.new_context(viewport={'width': 1440, 'height': 1000}, timezone_id='Asia/Shanghai')

                def intercept(route):
                    request = route.request
                    parsed = urlsplit(request.url)
                    if '/message-notifications/' in parsed.path:
                        notification_test_requests.append(f'{request.method} {parsed.path}')
                    if parsed.hostname != '127.0.0.1':
                        external_requests.append(parsed.hostname)
                        route.abort()
                        return
                    def fulfill(data):
                        route.fulfill(status=200, content_type='application/json', body=json.dumps({'success': True, 'data': data}))
                    if parsed.path.startswith('/chat/conversations/'):
                        conversation_requests.append(request.url)
                        older = bool(parse_qs(parsed.query).get('cursor'))
                        def conversation(label, days):
                            return {'cid': label, 'otherUserId': label, 'otherUserName': label,
                                    'lastMessageTime': int((now - timedelta(days=days)).timestamp() * 1000),
                                    'lastMessageSummary': label, 'unreadCount': 1}
                        fulfill({'conversations': [conversation('更早会话', 20)] if older else [conversation('今日会话', 0), conversation('昨日会话', 1)],
                                 'hasMore': not older, 'nextCursor': int((now - timedelta(days=2 if not older else 21)).timestamp() * 1000)})
                    elif parsed.path.startswith('/chat/messages/'):
                        message_requests.append(parsed.path)
                        fulfill({'messages': [], 'hasMore': False})
                    elif parsed.path.startswith('/chat/send'):
                        raise AssertionError('冒烟测试不得发送消息')
                    elif '/message-notifications/rule/' in parsed.path and parsed.path.rstrip('/').endswith('/test'):
                        # UI 状态测试使用本地回执，不触发邮件、Webhook 或其他外部通知。
                        route.fulfill(status=200, content_type='application/json', body=json.dumps({
                            'success': True, 'sent': True, 'mode': 'event', 'event_type': 'login_verification_required',
                            'message': '隔离冒烟渠道测试回执', 'request_id': 'ui-smoke-request',
                            'sent_at': int(time.time()),
                            'channel': {'id': notification_channel_id, 'name': '隔离冒烟渠道', 'type': 'email'},
                        }))
                    else:
                        if parsed.path == '/api/orders':
                            order_requests.append(request.url)
                        route.continue_()

                context.route('**/*', intercept)
                context.add_init_script(
                    f"if (window.top === window) {{ localStorage.setItem('auth_token', {json.dumps(token)}); "
                    "localStorage.setItem('active_page', 'orders'); }"
                )
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base, wait_until='networkidle')
                selector = page.get_by_role('tablist', name='订单日期范围', exact=True)
                expect(selector.get_by_role('tab', name='7天内', exact=True)).to_have_attribute('aria-selected', 'true')
                expect(page.get_by_text('订单ID: smoke-today', exact=True)).to_be_visible()
                expect(page.get_by_text('订单ID: smoke-yesterday', exact=True)).to_be_visible()
                expect(page.get_by_text('订单ID: smoke-old', exact=True)).to_have_count(0)
                assert selector.get_by_role('tab').all_text_contents() == ['昨天', '今天', '3天内', '7天内', '1个月内', '自定义']
                selector.get_by_role('tab', name='昨天', exact=True).click()
                expect(page.get_by_text('订单ID: smoke-today', exact=True)).to_have_count(0)
                expect(page.get_by_text('订单ID: smoke-yesterday', exact=True)).to_be_visible()
                selector.get_by_role('tab', name='1个月内', exact=True).click()
                expect(page.get_by_text('订单ID: smoke-old', exact=True)).to_be_visible()
                selector.get_by_role('tab', name='自定义', exact=True).click()
                expect(page.get_by_role('button', name='应用', exact=True)).to_be_disabled()
                page.wait_for_load_state('networkidle')
                request_count = len(order_requests)
                custom_day = (today - timedelta(days=20)).strftime('%Y-%m-%d')
                page.get_by_label('订单日期范围开始日期').fill(custom_day)
                page.get_by_label('订单日期范围结束日期').fill(custom_day)
                # 草稿不应用、不发请求；点击应用后才提交日期范围。
                page.wait_for_timeout(300)
                assert len(order_requests) == request_count
                expect(page.get_by_text('订单ID: smoke-today', exact=True)).to_be_visible()
                group = page.get_by_label('订单日期范围开始日期').locator('..')
                assert group.get_attribute('class') == 'toolbar__group'
                assert group.locator('input').first.get_attribute('class') == 'ios-input rounded-md px-3 py-2 text-sm'
                assert group.locator('span').inner_text() == '-'
                assert group.get_by_role('button', name='应用').get_attribute('class') == 'ios-btn-secondary rounded-md px-4 py-2 text-sm'
                page.get_by_role('button', name='应用', exact=True).click()
                expect(page.get_by_text('订单ID: smoke-old', exact=True)).to_be_visible()
                expect(page.get_by_text('订单ID: smoke-today', exact=True)).to_have_count(0)
                result['checks'].append('订单复用总览PageTabs、指定toolbar__group类名、草稿零请求及点击应用后过滤')
                page.screenshot(path=str(args.output / 'orders-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                page.wait_for_timeout(400)
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 2')
                page.screenshot(path=str(args.output / 'orders-mobile.png'), full_page=True)
                page.set_viewport_size({'width': 1440, 'height': 1000})

                page.get_by_role('button', name='商品与发货', exact=True).click()
                page.get_by_role('button', name='编辑本地详情', exact=True).click()
                textarea = page.get_by_label('本地商品详情')
                short_height = textarea.bounding_box()['height']
                textarea.fill('\n'.join(f'第{i}行商品说明' for i in range(35)))
                expect(textarea).to_have_value('\n'.join(f'第{i}行商品说明' for i in range(35)))
                assert textarea.bounding_box()['height'] > short_height * 2
                textarea.fill('简短说明')
                assert textarea.bounding_box()['height'] <= short_height + 2
                textarea.fill('很长的商品说明 ' * 80)
                wide_height = textarea.bounding_box()['height']
                page.set_viewport_size({'width': 390, 'height': 844})
                page.wait_for_timeout(150)
                assert textarea.bounding_box()['height'] > wide_height
                assert textarea.evaluate('(element) => element.scrollHeight <= element.clientHeight + 2')
                page.get_by_role('button', name='取消', exact=True).click()
                page.set_viewport_size({'width': 1440, 'height': 1000})
                result['checks'].append('详情输入框打开、增长、缩短与窄屏换行自适应')

                page.get_by_role('button', name='账号管理', exact=True).click()
                title = page.get_by_role('heading', name='冒烟昵称', exact=True)
                expect(title).to_be_visible()
                badge = page.get_by_text('备注：冒烟备注', exact=True)
                expect(badge).to_be_visible()
                assert 'status-badge' in badge.get_attribute('class')
                assert badge.bounding_box()['x'] >= title.bounding_box()['x'] + title.bounding_box()['width']
                page.screenshot(path=str(args.output / 'account-remark-desktop.png'), full_page=True)
                page.get_by_role('button', name='编辑账号', exact=True).first.click()
                dialog = page.get_by_role('dialog', name='编辑账号', exact=True)
                dialog.get_by_placeholder('为账号添加备注').fill('修改后备注')
                dialog.get_by_role('button', name='保存', exact=True).click()
                expect(dialog).to_have_count(0)
                expect(page.get_by_role('heading', name='冒烟昵称', exact=True)).to_be_visible()
                expect(page.get_by_text('备注：修改后备注', exact=True)).to_be_visible()
                page.set_viewport_size({'width': 390, 'height': 844})
                page.wait_for_timeout(400)
                page.screenshot(path=str(args.output / 'account-remark-mobile.png'), full_page=True)
                assert page.get_by_text('备注：修改后备注', exact=True).evaluate('(element) => element.getBoundingClientRect().right <= innerWidth')
                page.set_viewport_size({'width': 1440, 'height': 1000})
                result['checks'].append('昵称h3保持、右侧备注标签、编辑保存即时刷新')

                page.get_by_role('button', name='消息中心', exact=True).click()
                message_selector = page.get_by_role('tablist', name='消息日期范围', exact=True)
                expect(message_selector.get_by_role('tab', name='7天内', exact=True)).to_have_attribute('aria-selected', 'true')
                expect(page.get_by_label('消息账号').locator('option', has_text='🟢 冒烟昵称（修改后备注）')).to_have_count(1)
                expect(page.get_by_text('今日会话', exact=True).first).to_be_visible()
                assert message_selector.get_by_role('tab').all_text_contents() == ['3天内', '7天内', '1个月内']
                expect(page.get_by_label('消息日期范围开始日期')).to_have_count(0)
                expect(page.get_by_label('消息日期范围结束日期')).to_have_count(0)
                expect(page.get_by_role('button', name='应用', exact=True)).to_have_count(0)
                message_selector.get_by_role('tab', name='3天内', exact=True).click()
                expect(page.get_by_text('昨日会话', exact=True).first).to_be_visible()
                expect(page.get_by_text('今日会话', exact=True).first).to_be_visible()
                page.screenshot(path=str(args.output / 'messages-date-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                page.wait_for_timeout(400)
                page.screenshot(path=str(args.output / 'messages-date-mobile.png'), full_page=True)
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 2')
                for width in [320, 390, 768, 1440]:
                    page.set_viewport_size({'width': width, 'height': 844})
                    page.wait_for_timeout(150)
                    assert message_selector.evaluate('(element) => element.scrollWidth <= element.clientWidth + 1')
                    for control in message_selector.get_by_role('tab').all():
                        assert control.evaluate('(element) => { const rect=element.getBoundingClientRect(); return rect.left >= 0 && rect.right <= innerWidth + 1 && element.scrollWidth <= element.clientWidth + 1; }')
                page.set_viewport_size({'width': 1440, 'height': 1000})
                message_selector.get_by_role('tab', name='1个月内', exact=True).click()
                page.get_by_role('button', name='加载更早会话', exact=True).click()
                expect(page.get_by_text('更早会话', exact=True).first).to_be_visible()
                # 实时事件流启用时不再空闲轮询；主动刷新同样不能丢掉历史页。
                page.wait_for_timeout(10500)
                page.get_by_role('button', name='刷新账号和会话', exact=True).click()
                page.wait_for_timeout(300)
                expect(page.get_by_text('更早会话', exact=True).first).to_be_visible()
                # 新工作台不自动打开并标记第一条会话，刷新应验证会话请求而非聊天请求。
                assert len(conversation_requests) >= 3
                message_selector.get_by_role('tab', name='3天内', exact=True).click()
                expect(page.get_by_text('更早会话', exact=True)).to_have_count(0)
                message_selector.get_by_role('tab', name='7天内', exact=True).click()
                expect(page.get_by_text('更早会话', exact=True)).to_have_count(0)
                result['checks'].append('消息仅3天/7天/1个月、默认7天、320/390/768/1440宽度三项完整显示、过滤与历史刷新保留')

                page.get_by_text('今日会话', exact=True).first.click()
                composer = page.get_by_label('消息内容', exact=True)
                expect(composer).to_be_visible()
                page.get_by_role('button', name='快捷短语', exact=True).click()
                page.get_by_text('冒烟 · 图片话术', exact=True).click()
                expect(composer).to_have_value('图片回复')
                expect(page.get_by_alt_text('待发送图片')).to_be_visible()
                assert page.get_by_role('button', name='发送', exact=True).is_enabled()
                result['checks'].append('图片快捷短语插入文本并带出待发送图片，不自动发送')

                page.get_by_role('button', name='系统设置', exact=True).click()
                page.get_by_role('tab', name='快捷短语', exact=True).click()
                phrase_section = page.get_by_role('heading', name='快捷短语', exact=True)
                expect(phrase_section).to_be_visible()
                expect(page.get_by_text('图片话术', exact=True)).to_be_visible()
                expect(page.get_by_text('图片回复', exact=True)).to_be_visible()
                phrase_upload = page.get_by_role('button', name='添加图片（可选）', exact=True)
                expect(phrase_upload).to_be_visible()
                expect(page.get_by_text('文字和图片至少填写一项；图片最大5MB。', exact=True)).to_have_count(0)
                for width in (375, 768, 1280, 1440):
                    page.set_viewport_size({'width': width, 'height': 1000})
                    page.wait_for_timeout(100)
                    a = page.get_by_placeholder('话术内容', exact=True).bounding_box()
                    b = phrase_upload.bounding_box()
                    assert abs(a['y'] - b['y']) < 4 and a['x'] + a['width'] <= b['x']
                    assert b['x'] + b['width'] <= width and a['width'] >= 60
                    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 2')
                    page.screenshot(path=str(args.output / f'phrase-inline-{width}.png'), full_page=True)
                result['checks'].append('快捷短语图片按钮与内容同一行，375/768/1280/1440px无裁切，提示已删除')

                page.get_by_role('button', name='通知与日志', exact=True).click()
                expect(page.get_by_role('tab', name='通知渠道', exact=True)).to_be_visible()
                tab_names = page.get_by_role('tab').all_text_contents()
                assert tab_names.index('通知渠道') < tab_names.index('通知预览') < tab_names.index('账号通知')
                page.get_by_role('tab', name='通知预览', exact=True).click()
                content = page.get_by_label('编写内容', exact=True)
                expect(page.get_by_title('通知邮件实时预览')).to_be_visible()
                assert '<!DOCTYPE html>' in content.input_value()
                assert '{{message}}' in content.input_value()
                content.fill('定制 {{account}}\n{{event}}\n{{message}}')
                expect(page.locator('[aria-live="polite"][aria-busy="false"]').filter(has_text='定制 示例账号')).to_be_visible()
                page.get_by_role('button', name='保存模板', exact=True).click()
                expect(page.get_by_text('通知模板已保存，后续正式通知即时生效', exact=True)).to_be_visible()
                saved = db_manager.get_user_setting(owner, 'notification_template_login_verification_required')
                assert saved['value'].startswith('定制 {{account}}')
                page.get_by_role('tab', name='通知渠道', exact=True).click()
                page.get_by_role('tab', name='通知预览', exact=True).click()
                expect(page.get_by_label('编写内容', exact=True)).to_have_value(saved['value'])
                content.fill('{{unknown}}')
                expect(page.get_by_role('alert')).to_contain_text('不支持的模板变量')
                expect(page.get_by_role('button', name='保存模板', exact=True)).to_be_disabled()
                page.get_by_role('button', name='恢复默认', exact=True).click()
                expect(page.get_by_role('button', name='保存模板', exact=True)).to_be_enabled()
                expect(page.frame_locator('iframe[title="通知邮件实时预览"]').locator('body')).to_contain_text('建议操作')
                expect(page.locator('iframe[title="通知邮件实时预览"]')).to_have_attribute('aria-busy', 'false')
                page.screenshot(path=str(args.output / 'notification-preview-desktop.png'), full_page=True)
                page.get_by_role('radio', name='使用深色主题', exact=True).click()
                page.wait_for_timeout(250)
                assert page.get_by_label('编写内容', exact=True).evaluate('(element) => getComputedStyle(element).backgroundColor !== "rgb(255, 255, 255)"')
                page.screenshot(path=str(args.output / 'notification-preview-dark.png'), full_page=True)
                page.get_by_role('radio', name='使用浅色主题', exact=True).click()
                page.wait_for_timeout(250)
                page.set_viewport_size({'width': 390, 'height': 844})
                page.wait_for_timeout(400)
                page.screenshot(path=str(args.output / 'notification-preview-mobile.png'), full_page=True)
                expect(page.frame_locator('iframe[title="通知邮件实时预览"]').locator('body')).to_contain_text('登录安全验证')
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 2')
                result['checks'].append('通知标签顺序、实时预览、后端保存、重进持久化、无效变量、移动端布局')

                # 设置、账号名称及商品数量都使用隔离数据库；未保存生产配置或发送外部通知。
                page.set_viewport_size({'width': 1440, 'height': 1000})
                page.get_by_role('button', name='系统设置', exact=True).click()
                expect(page.get_by_role('switch', name='允许用户注册')).to_have_attribute('aria-checked', 'false')
                expect(page.get_by_role('switch', name='显示默认登录信息')).to_have_attribute('aria-checked', 'false')
                expect(page.get_by_role('heading', name='修改登录邮箱', exact=True)).to_be_visible()
                password_heading = page.get_by_role('heading', name='修改登录密码', exact=True)
                email_heading = page.get_by_role('heading', name='修改登录邮箱', exact=True)
                password_box = password_heading.bounding_box()
                email_box = email_heading.bounding_box()
                assert abs(password_box['y'] - email_box['y']) < 2 and password_box['x'] < email_box['x']
                expect(page.get_by_text('常用兼容服务包括阿里云 DashScope 和 OpenAI。API Key 仅保存在当前系统配置中。', exact=True)).to_have_count(0)
                page.screenshot(path=str(args.output / 'settings-email-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                page.wait_for_timeout(250)
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 2')
                page.screenshot(path=str(args.output / 'settings-email-mobile.png'), full_page=True)

                page.set_viewport_size({'width': 1440, 'height': 1000})
                page.get_by_role('button', name='商品与发货', exact=True).click()
                item_accounts = page.get_by_label('选择账号')
                expect(item_accounts).to_have_value('')
                expect(item_accounts.locator('option', has_text='全部账号（1）')).to_have_count(1)
                expect(item_accounts.locator('option', has_text='冒烟昵称（修改后备注 · 1）')).to_have_count(1)
                expect(item_accounts.locator('option', has_text='下架账号昵称（小号 · 0）')).to_have_count(1)
                expect(page.get_by_text('冒烟商品', exact=True)).to_be_visible()
                expect(page.get_by_text('1 件在售商品', exact=True)).to_be_visible()
                for width in (375, 768, 1280, 1440):
                    page.set_viewport_size({'width': width, 'height': 1000})
                    page.wait_for_timeout(200)
                    expect(page.get_by_role('heading',name='冒烟商品',exact=True)).to_be_visible()
                    bounds=item_accounts.bounding_box()
                    assert bounds['x']>=0 and bounds['x']+bounds['width']<=width+1
                    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+2')
                    page.screenshot(path=str(args.output/f'on-sale-only-{width}.png'),full_page=True)
                page.get_by_role('radio',name='使用深色主题',exact=True).click()
                expect(page.locator('html')).to_have_attribute('data-theme','dark')
                page.wait_for_timeout(350)
                page.screenshot(path=str(args.output/'on-sale-only-dark.png'),full_page=True)
                page.get_by_role('radio',name='使用浅色主题',exact=True).click()
                expect(page.locator('html')).to_have_attribute('data-theme','light')
                page.wait_for_timeout(350)
                item_accounts.select_option('smoke-off-shelf')
                expect(page.get_by_text('已下架商品', exact=True)).to_have_count(0)
                item_accounts.select_option('')
                expect(page.get_by_text('显示已下架', exact=False)).to_have_count(0)
                expect(page.get_by_text('历史订单商品', exact=False)).to_have_count(0)
                assert db_manager.conn.execute("SELECT COUNT(*) FROM item_info WHERE cookie_id='smoke-account'").fetchone()[0] == 5
                result['checks'].append('商品页仅展示确认在售；1件在售加4件历史数量仍为1；历史商品和下架入口均不显示且数据保留')

                page.get_by_role('button', name='订单管理', exact=True).click()
                order_accounts = page.get_by_label('按账号筛选订单')
                expect(order_accounts.locator('option', has_text='冒烟昵称（修改后备注）')).to_have_count(1)
                page.get_by_role('button', name='买家互动', exact=True).click()
                expect(page.get_by_text('冒烟昵称（修改后备注）', exact=False)).to_be_visible()
                page.get_by_role('button', name='自动回复', exact=True).click()
                expect(page.get_by_label('应用账号').locator('option', has_text='冒烟昵称（修改后备注）')).to_have_count(1)
                page.get_by_role('button', name='AI 回复', exact=True).click()
                expect(page.get_by_label('当前账号').locator('option', has_text='冒烟昵称（修改后备注）')).to_have_count(1)
                page.get_by_role('button', name='商品自动化', exact=True).click()
                expect(page.get_by_role('combobox').first.locator('option', has_text='冒烟昵称（修改后备注）')).to_have_count(1)

                page.get_by_role('button', name='通知与日志', exact=True).click()
                page.get_by_role('tab', name='账号通知', exact=True).click()
                page.get_by_role('button', name='新建规则', exact=True).click()
                editor = page.get_by_role('dialog')
                expect(editor.locator('button[aria-pressed="true"]')).to_have_count(11)
                for label in ('买家消息', '自动发货成功', '登录认证已恢复'):
                    expect(editor.get_by_role('button', name=label, exact=False)).to_have_attribute('aria-pressed', 'false')
                editor.get_by_role('button', name='取消', exact=True).click()
                page.get_by_title('编辑规则').first.click()
                editor = page.get_by_role('dialog')
                expect(editor.locator('button[aria-pressed="true"]')).to_have_count(1)
                expect(editor.get_by_role('button', name='登录认证已恢复', exact=False)).to_have_attribute('aria-pressed', 'true')
                editor.get_by_role('button', name='取消', exact=True).click()
                expect(page.get_by_role('button', name='测试订阅投递冒烟账号规则', exact=True)).to_have_count(0)
                event_select = page.get_by_label('通知内容冒烟账号规则')
                expect(event_select).to_be_visible()
                expect(event_select.locator('option')).to_have_count(18)
                expect(page.get_by_text('冒烟昵称（修改后备注）', exact=False)).to_be_visible()
                channel_test = page.get_by_role('button', name='事件投递测试冒烟账号规则', exact=True)
                expect(channel_test).to_be_visible()
                select_box, button_box = event_select.bounding_box(), channel_test.bounding_box()
                assert select_box['x'] < button_box['x'] and abs(select_box['y'] - button_box['y']) < 24
                channel_test.click()
                expect(page.get_by_role('alertdialog', name='确认测试通知内容')).to_be_visible()
                # 渠道测试是真实外部投递动作；隔离冒烟只验证确认弹窗和窄屏布局，
                # 取消后不得产生任何投递请求，真实 QQ 邮箱验收另行按运维流程进行。
                page.get_by_role('button', name='取消', exact=True).last.click()
                expect(page.get_by_role('alertdialog', name='确认测试通知内容')).to_have_count(0)
                page.screenshot(path=str(args.output / 'notification-binding-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                page.wait_for_timeout(250)
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 2')
                for control in (event_select, channel_test):
                    box = control.bounding_box()
                    assert box['x'] >= 0 and box['x'] + box['width'] <= 391
                page.screenshot(path=str(args.output / 'notification-binding-mobile.png'), full_page=True)
                result['checks'].append('全站账号名称加备注；按通知事件进行订阅内测试，成功不留绿色回执，外部发送被拦截')
                assert not any(path.endswith('/test') and method == 'POST' for method, path in (entry.split(' ', 1) for entry in notification_test_requests))
                page.set_viewport_size({'width': 1440, 'height': 1000})
                result['page_errors'] = errors
                result['external_requests'] = external_requests
                assert not errors and not external_requests, json.dumps(
                    {'page_errors': errors, 'external_request_hosts': external_requests}, ensure_ascii=False,
                )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
    (args.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
