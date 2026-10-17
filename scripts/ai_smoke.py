#!/usr/bin/env python3
"""真实中转站验收：隔离服务 + 临时 SQLite + 真实 Chromium + 真实网络请求，不替换任何接口。

需要一把可用的测试密钥：--key-file 指向只含密钥的文件（权限必须是 600）。
密钥只会写入一次性数据库，不打印、不写入报告；测试结束后临时目录整体删除。
会向 --base-url（默认 api.bingyun.vip，故意不带 /v1，用来验证自动补全）发出少量真实请求：
模型列表几次、生成回复十次左右。不调用闲鱼，不发送邮件。

浏览器步骤失败时只打印步骤名和截断后的错误说明（密钥替换成 ***）：Playwright 的错误信息会带页面快照，其中可能含有刚输入的密钥。
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx

BOOTSTRAP = r'''
import os
assert os.getcwd().split('/')[-1].startswith('xianyu-ai-smoke-')
from app import cookie_manager
from app.reply_server import app
from app.db_manager import db_manager as db
for name in ('owner',):
    if not db.get_user_by_username(name):
        db.create_user(name, name + '@example.invalid', os.environ['AUDIT_PASSWORD'])
uid = db.get_user_by_username('owner')['id']
for cid in ('test-ui', 'test-owned', 'test-sys'):
    if not db.get_cookie_details(cid):
        db.save_cookie(cid, f'unb={cid}; cookie2=local-fixture', uid)


class IsolatedManager:
    """隔离服务没有启动账号任务（也就不会连接闲鱼）：只回答账号是否存在，运行状态一律是已停止。"""

    def __init__(self, ids):
        self.cookies = {cid: 'x' for cid in ids}
        self.instances = {}

    def get_cookie_status(self, cid):
        return False

    def get_task_state(self, cid):
        return 'stopped'


cookie_manager.manager = IsolatedManager(('test-ui', 'test-owned', 'test-sys'))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--frontend-dir', type=Path, required=True)
    parser.add_argument('--base-url', default='api.bingyun.vip')
    parser.add_argument('--model', default='', help='用于回复测试的模型；留空时优先 gpt-6-sol，其次列表里第一个非图像模型')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.key_file.stat().st_mode & 0o077:
        sys.exit('密钥文件权限过宽，请先 chmod 600')
    key = args.key_file.read_text().strip()
    results = []

    def check(name, condition, detail=''):
        results.append({'check': name, 'passed': bool(condition), **({'detail': detail} if detail else {})})
        print(('[OK] ' if condition else '[FAIL] ') + name + (f'  ({detail})' if detail else ''), flush=True)
        if not condition:
            raise AssertionError(name)

    with tempfile.TemporaryDirectory(prefix='xianyu-ai-smoke-') as directory:
        work = Path(directory)
        password = secrets.token_urlsafe(24)
        admin_password = secrets.token_urlsafe(24)
        (work / 'local_app.py').write_text(BOOTSTRAP)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = {**os.environ, 'PYTHONPATH': f'{work}:{ROOT}', 'DB_PATH': str(work / 'local.db'),
               'APP_LOG_PATH': str(work / 'logs' / 'application.log'),
               'AUDIT_PASSWORD': password, 'ADMIN_PASSWORD': admin_password, 'SQL_LOG_ENABLED': 'false',
               'PYTHONDONTWRITEBYTECODE': '1', 'APP_STATIC_DIR': str(args.frontend_dir.resolve())}
        process = None
        log_path = work / 'logs' / 'application.log'

        def server_log():
            time.sleep(1.2)  # 日志是异步落盘的
            return log_path.read_text(errors='replace') if log_path.exists() else ''

        try:
            with (work / 'service.log').open('ab') as output:
                process = subprocess.Popen(
                    [sys.executable, '-m', 'uvicorn', 'local_app:app', '--host', '127.0.0.1', '--port', str(port)],
                    cwd=work, env=env, stdout=output, stderr=output)
            for _ in range(150):
                if process.poll() is not None:
                    raise RuntimeError('隔离服务启动失败')
                try:
                    if httpx.get(f'http://127.0.0.1:{port}/verify', trust_env=False, timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(.2)
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=120) as client:
                def login(name, pw):
                    body = client.post('/login', json={'username': name, 'password': pw}).json()
                    assert body.get('success'), 'local login'
                    return {'Authorization': 'Bearer ' + body['token']}
                owner = login('owner', password)
                admin = login('admin', admin_password)

                def models(headers, payload):
                    time.sleep(2.2)  # 模型列表接口两次请求至少间隔 2 秒
                    return client.post('/ai-models', headers=headers, json=payload)

                # ---- 1. 读取模型列表：地址不带 /v1、密钥取自表单 ----
                started = time.monotonic()
                r = models(owner, {'base_url': args.base_url, 'api_key': key, 'scope': 'account', 'cookie_id': 'test-owned'})
                body = r.json() if r.status_code == 200 else {}
                check('real relay: model list fetched with the form key', r.status_code == 200 and body.get('count', 0) >= 1,
                      f"{body.get('count')} models in {time.monotonic() - started:.1f}s")
                resolved = body['base_url']
                check('real relay: /v1 appended automatically', resolved == f"https://{args.base_url.split('://')[-1].strip('/')}/v1")
                available = body['models']
                model = args.model or ('gpt-6-sol' if 'gpt-6-sol' in available else
                                       next(name for name in available if 'image' not in name))
                check('real relay: chosen model is in the list', model in available, model)

                # ---- 2. 账号级设置：保存后密钥不回传，地址已规范化 ----
                payload = {'ai_enabled': True, 'model_name': model, 'api_key': key, 'base_url': args.base_url,
                           'user_agent': '', 'max_bargain_rounds': 3}
                r = client.put('/ai-reply-settings/test-owned', headers=owner, json=payload)
                saved = client.get('/ai-reply-settings/test-owned', headers=owner).json()
                check('account AI settings saved with normalized address',
                      r.status_code == 200 and saved['base_url'] == resolved and saved['model_name'] == model)
                check('saved API key is never returned', saved['api_key'] == '' and saved['api_key_configured'] is True)
                r = models(owner, {'base_url': resolved, 'cookie_id': 'test-owned'})
                check('real relay: saved key is reused for the same address', r.status_code == 200 and r.json()['count'] == len(available))
                r = models(owner, {'base_url': 'https://other.example.invalid/v1', 'cookie_id': 'test-owned'})
                check('saved key is not sent to a different address', r.status_code == 400)

                # ---- 3. 非 Claude 模型：先 /responses ----
                started = time.monotonic()
                r = client.post('/ai-reply-test/test-owned', headers=owner, json={'message': '你好，这个还在吗？'})
                reply = r.json().get('reply', '') if r.status_code == 200 else ''
                check('real relay: reply generated for a non-Claude model', bool(reply.strip()),
                      f'{time.monotonic() - started:.1f}s, {len(reply)} chars')
                log = server_log()
                check('non-Claude model went through /responses first, as an SSE stream',
                      f'AI请求成功 协议=/responses 方式=流式 模型={model}' in log and '协议=/chat/completions' not in log)

                # ---- 4. Claude 模型：先 /messages，不成功再 /chat/completions ----
                before = len(log)
                client.put('/ai-reply-settings/test-owned', headers=owner, json={**payload, 'api_key': '', 'model_name': 'claude-sonnet-5-5'})
                r = client.post('/ai-reply-test/test-owned', headers=owner, json={'message': '你好'})
                detail = r.json().get('detail', '') if r.status_code != 200 else ''
                tail = server_log()[before:]
                order = [i for i in (tail.find('协议=/messages'), tail.find('协议=/chat/completions')) if i >= 0]
                check('Claude model tries /messages first, then /chat/completions',
                      r.status_code in (200, 400) and len(order) == 2 and order == sorted(order),
                      'accepted' if r.status_code == 200 else detail[:160])
                if r.status_code != 200:
                    check('failure reason names both protocols', '/messages' in detail and '/chat/completions' in detail)

                # ---- 5. 不存在的模型：两个协议都失败，原因写清楚 ----
                before = len(server_log())
                client.put('/ai-reply-settings/test-owned', headers=owner, json={**payload, 'api_key': '', 'model_name': 'no-such-model-x'})
                r = client.post('/ai-reply-test/test-owned', headers=owner, json={'message': '你好'})
                detail = r.json().get('detail', '')
                check('unknown model: both protocols tried and reasons reported',
                      r.status_code == 400 and '/responses' in detail and '/chat/completions' in detail and 'not supported' in detail.lower(),
                      detail[:160])

                # ---- 6. 密钥错误：401 不降级 ----
                bad_key = 'sk-' + 'wrong' * 8
                client.put('/ai-reply-settings/test-owned', headers=owner, json={**payload, 'api_key': bad_key})
                r = client.post('/ai-reply-test/test-owned', headers=owner, json={'message': '你好'})
                detail = r.json().get('detail', '')
                check('wrong key: HTTP 401 reported and no fallback attempt',
                      r.status_code == 400 and '401' in detail and '/chat/completions' not in detail, detail[:120])
                check('keys never appear in the server log', key not in server_log() and bad_key not in server_log())

                # ---- 7. 真实浏览器（普通用户）：地址补全、获取模型、选择、保存、回复测试 ----
                # 此时还没有配置系统默认密钥，test-ui 账号的密钥输入框是空的。
                from contextlib import contextmanager
                from playwright.sync_api import sync_playwright, expect
                from utils.direct_network import direct_browser_options
                step_name = ''

                def step(name):
                    nonlocal step_name
                    step_name = name

                @contextmanager
                def browser_page():
                    errors = []
                    with sync_playwright() as pw:
                        with pw.chromium.launch(**direct_browser_options({'headless': True})) as browser:
                            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                            page.on('pageerror', lambda e: errors.append(type(e).__name__))
                            try:
                                yield page, errors
                            except Exception as exc:  # noqa: BLE001 - 失败说明里把密钥换成 ***，并截断页面快照
                                detail = ' '.join(str(exc).replace(key, '***').split())[:500]
                                print(f'[FAIL] browser step "{step_name}": {type(exc).__name__}: {detail}', flush=True)
                                results.append({'check': f'browser step {step_name}', 'passed': False, 'detail': type(exc).__name__})
                                raise AssertionError(f'browser step {step_name}') from None

                def sign_in(page, name, pw_value, first_button):
                    page.goto(f'http://127.0.0.1:{port}/', wait_until='networkidle')
                    page.get_by_placeholder('请输入账号').fill(name)
                    page.get_by_placeholder('请输入密码').fill(pw_value)
                    page.get_by_role('button', name='登录', exact=True).click()
                    page.get_by_role('button', name=first_button, exact=True).click()

                with browser_page() as (page, errors):
                    step('login and open the AI page')
                    sign_in(page, 'owner', password, 'AI 回复')
                    page.get_by_label('当前账号').wait_for()
                    page.get_by_label('当前账号').select_option('test-ui')
                    url_box = page.get_by_label('接口地址')
                    expect(url_box).to_have_value('https://api.bingyun.vip/v1')
                    step('fill address without /v1')
                    url_box.fill(args.base_url)
                    url_box.blur()
                    check('UI: address gets /v1 on blur', url_box.input_value() == resolved)
                    step('fill key and fetch models')
                    page.get_by_placeholder('请输入 API Key').fill(key)
                    page.get_by_role('button', name='获取模型').click()
                    listbox = page.get_by_role('listbox', name='可用模型')
                    listbox.wait_for()
                    options = listbox.get_by_role('option')
                    check('UI: model list shown with every model from the relay', options.count() == len(available),
                          f'{options.count()} options')
                    step('pick the model')
                    listbox.get_by_role('option', name=model, exact=True).click()
                    check('UI: clicking a model fills the field', page.get_by_label('模型名称').input_value() == model)
                    step('save settings')
                    page.get_by_role('checkbox').first.check()
                    page.get_by_role('button', name='保存配置').click()
                    expect(page.get_by_text('人工智能回复配置已保存')).to_be_visible()
                    persisted = client.get('/ai-reply-settings/test-ui', headers=owner).json()
                    check('UI: settings persisted (normalized address, model, key kept server-side)',
                          persisted['base_url'] == resolved and persisted['model_name'] == model
                          and persisted['api_key'] == '' and persisted['api_key_configured'] is True and persisted['ai_enabled'])
                    step('reply test in the browser')
                    started = time.monotonic()
                    page.get_by_role('button', name='测试回复').click()
                    reply_box = page.locator('div.border-yellow-400')
                    reply_box.wait_for(timeout=90000)
                    check('UI: reply test shows a real model reply', bool(reply_box.inner_text().strip()),
                          f'{time.monotonic() - started:.1f}s')
                    step('model fetch reuses the saved key')
                    page.reload(wait_until='networkidle')
                    page.get_by_label('当前账号').select_option('test-ui')
                    page.wait_for_timeout(2300)
                    page.get_by_role('button', name='获取模型').click()
                    page.get_by_role('listbox', name='可用模型').wait_for()
                    check('UI: models can be fetched again with the key left blank', True)
                    check('no JavaScript errors (user session)', not errors)

                # ---- 7b. 系统级默认配置：账号没有自己的密钥和地址时使用系统默认 ----
                for name, value in (('ai_api_url', args.base_url), ('ai_api_key', key), ('ai_model', model)):
                    check(f'system default saved: {name}', client.put(f'/system-settings/{name}', headers=admin, json={'value': value}).status_code == 200)
                system = client.get('/system-settings', headers=admin).json()
                system = system.get('data', system)
                check('system AI key masked, address normalized',
                      system.get('ai_api_key') == '' and system.get('ai_api_key_configured') is True and system.get('ai_api_url') == resolved)
                r = client.put('/ai-reply-settings/test-sys', headers=owner,
                               json={'ai_enabled': True, 'model_name': 'qwen-plus', 'api_key': '', 'base_url': '', 'user_agent': ''})
                effective = client.get('/ai-reply-settings/test-sys', headers=owner).json()
                check('account without its own config inherits the system default',
                      r.status_code == 200 and effective['base_url'] == resolved and effective['model_name'] == model)
                started = time.monotonic()
                r = client.post('/ai-reply-test/test-sys', headers=owner, json={'message': '包邮吗？'})
                check('real relay: reply generated from the system default configuration',
                      r.status_code == 200 and bool(r.json().get('reply', '').strip()), f'{time.monotonic() - started:.1f}s')
                r = models(admin, {'base_url': resolved, 'scope': 'system'})
                check('real relay: system scope reuses the saved system key', r.status_code == 200 and r.json()['count'] == len(available))

                # ---- 8. 真实浏览器（管理员）：系统默认 AI 配置 ----
                with browser_page() as (page, errors):
                    step('admin login and system settings')
                    sign_in(page, 'admin', admin_password, '系统设置')
                    page.get_by_role('tab', name='默认 AI 配置', exact=True).click()
                    expect(page.get_by_label('API 地址')).to_have_value(resolved)
                    expect(page.get_by_placeholder('已配置，留空保留原密钥')).to_be_visible()
                    page.wait_for_timeout(2300)
                    page.get_by_role('button', name='获取模型').click()
                    sys_list = page.get_by_role('listbox', name='可用模型')
                    sys_list.wait_for()
                    check('UI: system default model list loads with the saved key',
                          sys_list.get_by_role('option').count() == len(available))
                    check('no JavaScript errors (admin session)', not errors)
        finally:
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
    print(f'{len(results)} real-relay checks passed; temporary service/data removed')


if __name__ == '__main__':
    main()
