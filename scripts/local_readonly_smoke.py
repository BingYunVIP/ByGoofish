#!/usr/bin/env python3
"""真实本地浏览器只读巡检，不操作交易、账号开关或发送通知。"""
from collections import Counter
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright, expect
from utils.direct_network import direct_browser_options


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--password-file',type=Path,help='读取本机私密密码文件，不把密码写进命令行或日志')
    parser.add_argument('--output',type=Path,default=ROOT/'Custom/live-browser-smoke.json')
    args=parser.parse_args()
    password=args.password_file.read_text().strip() if args.password_file else os.getenv('XIANYU_SMOKE_PASSWORD', os.getenv('ADMIN_PASSWORD', 'admin123'))
    base = 'http://127.0.0.1:8080'
    result = {'timestamp': datetime.now().astimezone().isoformat(), 'pages': []}
    with sync_playwright() as pw:
        with pw.chromium.launch(**direct_browser_options({'headless': True})) as browser:
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []; failed = []; requests = Counter()
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.on('request', lambda r: requests.update([urlsplit(r.url).path])
                    if r.method == 'GET' and urlsplit(r.url).hostname == '127.0.0.1' else None)
            page.on('response', lambda r: failed.append(r.status) if r.status >= 400 else None)
            page.goto(base, wait_until='networkidle')
            page.get_by_placeholder('请输入账号').fill(os.getenv('XIANYU_SMOKE_USERNAME', 'admin'))
            page.get_by_placeholder('请输入密码').fill(password)
            with page.expect_response(lambda r: urlsplit(r.url).path == '/login' and r.request.method == 'POST') as login:
                page.get_by_role('button', name='登录', exact=True).click()
            assert login.value.json().get('success'), '本地管理登录失败，未重试'
            expect(page.get_by_role('button', name='账号管理', exact=True)).to_be_visible()
            page.wait_for_load_state('networkidle')
            page.wait_for_timeout(1000)
            result['initial_cookie_list_requests'] = requests['/cookies/details']
            result['initial_get_requests'] = sum(requests.values())
            result['hidden_sections'] = page.locator('section[hidden]').count()
            result['desktop_content_offset'] = page.locator('main').bounding_box()['x']
            headers = {'Authorization': 'Bearer ' + page.evaluate("localStorage.getItem('auth_token')")}
            ready = page.request.get(base + '/health/ready', headers=headers)
            result['readiness'] = ready.json()
            accounts = page.request.get(base + '/cookies/details', headers=headers).json()
            result['account_secrets_absent'] = all(
                not row.get('value') and not row.get('password') for row in accounts)
            channels = page.request.get(base + '/notification-channels', headers=headers).json()
            result['notification_passwords_absent'] = all(
                not json.loads(row['config']).get('email_password') for row in channels if row['type'] == 'email')
            names = ['总览', '账号管理', '商品与发货', '订单管理', '买家互动', '卡密库存',
                     '消息中心', '自动回复', 'AI 回复', '商品自动化', '用户管理', '通知与日志', '系统设置']
            for name in names:
                page.get_by_role('button', name=name, exact=True).first.click()
                page.wait_for_load_state('networkidle')
                result['pages'].append({'name': name, 'has_content': bool(page.locator('#root').inner_text()),
                                        'overflow': page.evaluate('document.documentElement.scrollWidth>innerWidth+2')})
            page.set_viewport_size({'width': 1280, 'height': 900})
            page.get_by_role('button', name='通知与日志', exact=True).click()
            page.get_by_role('tab', name='账号通知', exact=True).click()
            result['notification_tab_clickable'] = page.get_by_role('tab', name='账号通知', exact=True).get_attribute('aria-selected') == 'true'
            page.get_by_role('button', name='账号管理', exact=True).first.click()
            page.wait_for_load_state('networkidle')
            if accounts:
                page.get_by_title('编辑账号').first.click()
                dialog = page.get_by_role('dialog', name='编辑账号')
                expect(dialog).to_be_visible()
                expect(page.get_by_text('自动确认发货', exact=True)).to_be_visible()
                page.wait_for_timeout(100)
                result['dialog_focus_inside'] = page.evaluate("!!document.activeElement.closest('[role=dialog]')")
                page.keyboard.press('Escape')
                expect(dialog).to_be_hidden()
            else:
                result['fresh_database_empty_accounts'] = True
                result['dialog_focus_inside'] = None
            page.set_viewport_size({'width': 390, 'height': 844})
            result['mobile_overflow'] = page.evaluate('document.documentElement.scrollWidth>innerWidth+2')
            result['page_errors'] = errors
            result['http_error_statuses'] = failed
            page.request.post(base + '/logout', headers=headers)
    assert not errors and not failed and result['hidden_sections'] == 0
    assert result['account_secrets_absent'] and result['notification_passwords_absent']
    assert (result['dialog_focus_inside'] or result.get('fresh_database_empty_accounts')) and not result['mobile_overflow']
    assert result['desktop_content_offset'] >= 248 and result['notification_tab_clickable']
    assert all(row['has_content'] and not row['overflow'] for row in result['pages'])
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
