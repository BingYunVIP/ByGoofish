#!/usr/bin/env python3
"""真实验证邮件渠道编辑窗口里的「发送测试邮件」：真实页面、真实 SMTP，不模拟任何响应。

只向渠道已配置的接收邮箱发送一封手动测试邮件，并验证：
* 发件人显示名使用窗口里尚未保存的值，授权码留空时沿用已保存的授权码；
* 页面只发出一次测试请求，返回追踪 ID 和「已提交」提示；
* 取消编辑后渠道配置完全没有变化。
会真实发信，必须显式提供 --send（运行前应取得用户授权）。
"""

import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from playwright.sync_api import expect, sync_playwright
from utils.direct_network import direct_browser_options


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8080')
    parser.add_argument('--channel-id', type=int, required=True)
    parser.add_argument('--display-name', default='闲鱼超级管家')
    parser.add_argument('--send', action='store_true', help='明确执行真实发送；运行前应取得用户授权')
    parser.add_argument('--output', type=Path, help='保存脱敏验收结果')
    args = parser.parse_args()
    if not args.send:
        parser.error('此测试会真实发信，必须显式提供 --send')
    base = args.base_url.rstrip('/')
    if urlsplit(base).hostname not in ('localhost', '127.0.0.1', '::1'):
        parser.error('本地冒烟只接受本机回环地址')

    endpoint = '/notification-channels/email-test'
    with httpx.Client(trust_env=False, timeout=15) as client:
        client.get(base + '/health').raise_for_status()
        with sync_playwright() as playwright:
            with playwright.chromium.launch(**direct_browser_options({'headless': True})) as browser:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                posts = []
                errors = []
                page.on('pageerror', lambda error: errors.append(type(error).__name__))
                page.on('request', lambda request: posts.append(1)
                        if request.method == 'POST' and urlsplit(request.url).path == endpoint else None)
                page.goto(base, wait_until='networkidle', timeout=30000)
                page.get_by_placeholder('请输入账号').fill(os.getenv('XIANYU_SMOKE_USERNAME', 'admin'))
                page.get_by_placeholder('请输入密码').fill(os.getenv('XIANYU_SMOKE_PASSWORD', 'admin123'))
                with page.expect_response(lambda r: urlsplit(r.url).path == '/login' and r.request.method == 'POST') as login:
                    page.get_by_role('button', name='登录', exact=True).click()
                if login.value.json().get('success') is not True:
                    raise RuntimeError('本地登录未成功；未重试。请通过环境变量提供有效的管家登录信息。')
                headers = {'Authorization': 'Bearer ' + page.evaluate("localStorage.getItem('auth_token')")}
                before = client.get(f'{base}/notification-channels/{args.channel_id}', headers=headers).json()
                if before.get('type') != 'email' or not before.get('enabled'):
                    raise RuntimeError('目标渠道不是已启用的邮件渠道，未发送。')

                page.get_by_role('button', name='通知与日志', exact=True).click()
                page.get_by_title('编辑渠道').first.click()
                page.get_by_label('发件人显示名').fill(args.display_name)  # 只用于这次测试，不保存
                expect(page.get_by_label('邮箱密码或授权码')).to_have_value('')  # 密码不回传，留空沿用已保存的
                button = page.get_by_role('button', name='发送测试邮件')
                button.click()
                status = page.get_by_role('status').filter(has_text='测试邮件已提交')
                expect(status).to_be_visible(timeout=60000)
                text = status.inner_text()
                request_id = text.split('追踪 ID：')[-1].strip('）) ') if '追踪 ID：' in text else ''
                page.get_by_role('button', name='取消', exact=True).click()
                after = client.get(f'{base}/notification-channels/{args.channel_id}', headers=headers).json()
                result = {
                    'checked_at': __import__('datetime').datetime.now().astimezone().isoformat(timespec='seconds'),
                    'channel_id': args.channel_id,
                    'test_requests_sent_by_page': len(posts),
                    'request_id': request_id,
                    'submitted_message_shown': '测试邮件已提交' in text and '不是账号异常告警' in text,
                    'display_name_used_unsaved': args.display_name not in after['config'],
                    'channel_config_unchanged': before['config'] == after['config']
                                                and before['configured_fields'] == after['configured_fields'],
                    'javascript_errors': errors,
                }
                page.get_by_role('button', name='退出登录', exact=True).click()
    ok = (result['test_requests_sent_by_page'] == 1 and bool(result['request_id']) and result['submitted_message_shown']
          and result['display_name_used_unsaved'] and result['channel_config_unchanged'] and not errors)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    if not ok:
        sys.exit('邮件渠道测试验收未通过')
    print('1 real email submitted via the channel editor; configuration unchanged')


if __name__ == '__main__':
    main()
