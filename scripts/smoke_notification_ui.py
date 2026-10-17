#!/usr/bin/env python3
"""真实验证关闭的事件不发、已订阅事件仅发一封 QQ 邮件；不模拟响应。"""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
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
    parser.add_argument('--rule-id', type=int, required=True)
    parser.add_argument('--blocked-event', default='login_success')
    parser.add_argument('--allowed-event', default='cookie_invalid')
    parser.add_argument('--send', action='store_true', help='明确执行真实发送；运行前应取得用户授权')
    parser.add_argument('--output', type=Path, help='保存脱敏验收结果')
    args = parser.parse_args()
    if not args.send:
        parser.error('此测试会真实发信，必须显式提供 --send')
    base = args.base_url.rstrip('/')
    if urlsplit(base).hostname not in ('localhost', '127.0.0.1', '::1'):
        parser.error('本地冒烟只接受本机回环地址')
    if args.rule_id <= 0:
        parser.error('--rule-id 必须为正整数')

    with httpx.Client(trust_env=False, timeout=10) as client:
        health = client.get(base + '/health')
        health.raise_for_status()
        assert health.json()['status'] == 'healthy'

        with sync_playwright() as playwright:
            with playwright.chromium.launch(**direct_browser_options({'headless': True})) as browser:
                page = browser.new_page()
                # 只观察真实提示事件；不拦截、修改或模拟任何请求。
                page.add_init_script("""
                    window.__notificationSmokeFeedback = [];
                    window.addEventListener('app:feedback', e =>
                        window.__notificationSmokeFeedback.push(e.detail));
                """)
                errors = []
                sends = []
                endpoint = f'/message-notifications/rule/{args.rule_id}/test'
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: sends.append(1)
                        if request.method == 'POST' and urlsplit(request.url).path == endpoint else None)
                page.goto(base, wait_until='networkidle', timeout=30000)
                username = os.getenv('XIANYU_SMOKE_USERNAME', os.getenv('ADMIN_USERNAME', 'admin'))
                password = os.getenv('XIANYU_SMOKE_PASSWORD', os.getenv('ADMIN_PASSWORD', 'admin123'))
                page.get_by_placeholder('请输入账号').fill(username)
                page.get_by_placeholder('请输入密码').fill(password)
                with page.expect_response(lambda response: urlsplit(response.url).path == '/login'
                                          and response.request.method == 'POST') as login:
                    page.get_by_role('button', name='登录', exact=True).click()
                if login.value.json().get('success') is not True:
                    raise RuntimeError('本地登录未成功；未重试。请通过环境变量提供有效的管家登录信息。')

                token = page.evaluate("localStorage.getItem('auth_token')")
                headers = {'Authorization': f'Bearer {token}'}
                response = client.get(base + '/message-notifications', headers=headers)
                response.raise_for_status()
                rules = [dict(rule, cookie_id=cookie_id) for cookie_id, group in response.json().items() for rule in group]
                target = next((rule for rule in rules if int(rule['id']) == args.rule_id), None)
                if not target or target.get('channel_type') != 'email':
                    raise RuntimeError('目标规则不是当前用户可访问的邮件规则，未发送。')
                subscribed = target.get('event_types')
                if subscribed is None or args.blocked_event in subscribed or args.allowed_event not in subscribed:
                    raise RuntimeError('真实规则不满足禁用 / 订阅验收条件；不会自动改用户设置。')
                if not target.get('enabled') or not target.get('channel_enabled'):
                    raise RuntimeError('规则或渠道未启用，不自动修改用户设置。')
                before_rules = rules
                before_accounts = client.get(base + '/cookies/details', headers=headers).json()
                before_state = next(item['runtime_state'] for item in before_accounts if item['id'] == target['cookie_id'])
                response = client.get(base + '/notification-channels', headers=headers)
                response.raise_for_status()
                channel = next(item for item in response.json() if item['id'] == target['channel_id'])
                config = json.loads(channel['config'])
                if str(config.get('smtp_server', '')).lower() != 'smtp.qq.com':
                    raise RuntimeError('目标渠道不是已配置的 QQ SMTP，未发送。')
                if not str(config.get('recipient_email', '')).lower().endswith('@qq.com'):
                    raise RuntimeError('当前收件地址不是 QQ 邮箱，未发送。')

                page.get_by_role('button', name='账号管理', exact=True).click()
                expect(page.get_by_text('监听状态显示后台任务的最近检查结果。', exact=False)).to_be_visible()
                if before_state == 'running':
                    expect(page.locator('article').filter(has_text=target['cookie_id']).get_by_text('监听中', exact=True)).to_be_visible()
                page.get_by_role('button', name='通知与日志', exact=True).click()
                page.get_by_role('tab', name='账号通知', exact=True).click()
                expect(page.get_by_text('手动测试只检查通知能否投递', exact=False)).to_be_visible()
                rule_label = target.get('name') or target['channel_name']
                label = f"测试订阅投递{rule_label}"
                button = page.get_by_role('button', name=label, exact=True)
                expect(button).to_be_visible()

                # 真实调用校验接口：旧页面的空请求和未确认的渠道测试均不得发信。
                rejected = client.post(base + endpoint, headers=headers)
                assert rejected.status_code == 422, '旧的无模式请求仍可发信'
                rejected = client.post(base + endpoint, headers=headers, json={'mode': 'channel'})
                assert rejected.status_code == 400, '未确认渠道测试仍可发信'

                # 实际点击渠道测试再取消；不进行第二次 SMTP 提交。
                page.get_by_role('button', name=f'渠道测试{rule_label}', exact=True).click()
                expect(page.get_by_text('确认发送渠道测试', exact=True)).to_be_visible()
                page.get_by_role('button', name='取消', exact=True).last.click()
                assert len(sends) == 0, '取消确认仍发送了测试请求'

                select = page.get_by_role('combobox', name=f'测试订阅项{rule_label}', exact=True)
                select.select_option(args.blocked_event)
                page.evaluate('window.__notificationSmokeFeedback = []')
                with page.expect_response(lambda r: urlsplit(r.url).path == endpoint) as blocked:
                    button.click()
                blocked_result = blocked.value.json()
                assert blocked.value.status == 200 and blocked_result.get('sent') is False
                assert blocked_result.get('skipped') is True
                expect(page.locator('span[role="status"]:visible')).to_contain_text('未发送任何通知')
                page.wait_for_function('window.__notificationSmokeFeedback.length > 0')
                assert page.evaluate('window.__notificationSmokeFeedback.at(-1).type') == 'info'
                print(json.dumps({'blocked_event': args.blocked_event, 'sent': False,
                                  'request_id': blocked_result['request_id']}, ensure_ascii=False), flush=True)

                select.select_option(args.allowed_event)
                page.evaluate('window.__notificationSmokeFeedback = []')
                with page.expect_response(lambda r: urlsplit(r.url).path == endpoint, timeout=45000) as sent:
                    button.click()
                response = sent.value
                if response.status != 200:
                    raise RuntimeError(f'真实通知测试返回 HTTP {response.status}，未自动重试。')
                result = response.json()
                assert result.get('success') is True, '真实发送未成功'
                assert result.get('sent') is True and result.get('skipped') is False
                assert result['mode'] == 'event' and result['event_type'] == args.allowed_event
                message = result.get('message')
                assert isinstance(message, str) and message.strip(), '后端成功响应缺少 message'
                assert '测试邮件已提交' in message
                assert '手动测试，不是账号异常告警' in message
                assert re.fullmatch(r'[a-f0-9]{12}', result['request_id'])
                assert datetime.fromisoformat(result['sent_at']).tzinfo is not None
                assert isinstance(result['duration_ms'], int) and result['duration_ms'] >= 0
                state = page.locator('span[role="status"]:visible').filter(has_text=message)
                expect(state).to_be_visible()
                page.wait_for_function('window.__notificationSmokeFeedback.length > 0')
                feedback = page.evaluate('window.__notificationSmokeFeedback.at(-1)')
                assert feedback['type'] == 'success'
                assert message in feedback['message']
                assert result['request_id'] in feedback['message']
                assert 'undefined' not in feedback['message']
                assert 'Invalid Date' not in state.inner_text()
                assert len(sends) == 2, '应为一次拦截检查和一次实际发送'
                assert not errors, '浏览器出现脚本错误'
                # 重复请求被真实服务限流，禁止再次发信；不绕过、不等待重试。
                limited = client.post(base + endpoint, headers=headers,
                                      json={'mode': 'event', 'event_type': args.allowed_event})
                assert limited.status_code == 429 and int(limited.headers['Retry-After']) > 0
                after = client.get(base + '/message-notifications', headers=headers).json()
                assert [dict(rule, cookie_id=cookie_id) for cookie_id, group in after.items() for rule in group] == before_rules
                after_accounts = client.get(base + '/cookies/details', headers=headers).json()
                after_state = next(item['runtime_state'] for item in after_accounts if item['id'] == target['cookie_id'])
                assert before_state == after_state, '测试前后监听状态变化，需要独立核查账号连接'
                # 只输出脱敏回执，不输出账号、地址、SMTP 授权码、Cookie 或本地 token。
                record = {
                    'real_send_requests': 1, 'blocked_event_requests': 1,
                    'blocked_request_id': blocked_result['request_id'],
                    'runtime_state_before': before_state, 'runtime_state_after': after_state,
                    'repeat_request_status': limited.status_code, 'settings_unchanged': True,
                    'http_status': response.status,
                    'success': result['success'], 'message': message,
                    'request_id': result['request_id'], 'sent_at': result['sent_at'],
                    'duration_ms': result['duration_ms'], 'ui_feedback': feedback['message'],
                    'page_error_count': len(errors),
                }
                if args.output:
                    args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
                print(json.dumps(record, ensure_ascii=False))
                client.post(base + '/logout', headers=headers)


if __name__ == '__main__':
    main()
