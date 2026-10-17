"""用本地 HTTP / WebSocket 服务验证坏代理不会影响闲鱼链路，不访问外网。"""

import asyncio
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import aiohttp
import httpx
import websockets

from utils.direct_network import direct_browser_options, direct_environment
from utils.qr_login import QRLoginManager


POISONED_PROXY = {
    key: 'socks5://127.0.0.1:9'
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')
}
POISONED_PROXY.update(NO_PROXY='', no_proxy='')


class LoginHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        self.server.paths.append((self.command, path))
        payload = {}
        status = 200
        if path == '/login':
            payload = 'window.viewData = {"loginFormData":{"appName":"xianyu"}};'
        elif path == '/generate':
            payload = {'content': {'success': True, 'data': {
                't': 'test-time', 'ck': 'test-key', 'codeContent': 'https://qr.invalid/test',
            }}}
        elif path == '/query':
            payload = {'content': {'data': {'qrCodeStatus': 'NEW'}}}
        elif path == '/denied':
            status = 403
            payload = 'private-upstream-response'
        elif path == '/invalid-json':
            payload = 'private-upstream-response'
        body = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Set-Cookie', 'm_h5_tk=test_123; Path=/')
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.do_GET()


class DirectEnvironmentTests(unittest.TestCase):
    def test_child_environment_preserves_runtime_without_changing_parent(self):
        source = {**POISONED_PROXY, 'DISPLAY': ':99', 'PATH': '/test/bin',
                  'PLAYWRIGHT_BROWSERS_PATH': '/test/browsers', 'NODE_USE_ENV_PROXY': '1'}
        clean = direct_environment(source)
        self.assertNotIn('ALL_PROXY', clean)
        self.assertNotIn('NODE_USE_ENV_PROXY', clean)
        self.assertEqual(clean['NO_PROXY'], '*')
        self.assertEqual(clean['DISPLAY'], ':99')
        self.assertEqual(clean['PLAYWRIGHT_BROWSERS_PATH'], '/test/browsers')
        self.assertIn('ALL_PROXY', source)

    def test_browser_overrides_proxy_and_pac_without_mutating_options(self):
        original = {'proxy': {'server': 'http://127.0.0.1:9'},
                    'env': POISONED_PROXY.copy(), 'headless': True,
                    'args': ['--lang=zh-CN', '--proxy-server=socks5://127.0.0.1:9',
                             '--proxy-pac-url=http://127.0.0.1:9/pac', '--proxy-auto-detect']}
        result = direct_browser_options(original)
        self.assertEqual(result['args'], ['--lang=zh-CN', '--no-proxy-server'])
        self.assertNotIn('proxy', result)
        self.assertNotIn('https_proxy', result['env'])
        self.assertTrue(result['headless'])
        self.assertIn('proxy', original)
        self.assertEqual(len(original['args']), 4)


class DirectLoginTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), LoginHandler)
        cls.server.paths = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        cls.thread.start()
        cls.base_url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.server.paths.clear()
        self.manager = QRLoginManager()
        self.manager.api_h5_tk = self.base_url + '/token'
        self.manager.api_mini_login = self.base_url + '/login'
        self.manager.api_generate_qr = self.base_url + '/generate'
        self.manager.api_scan_status = self.base_url + '/query'

    async def test_generation_and_polling_ignore_socks_environment(self):
        with patch.dict(os.environ, POISONED_PROXY), patch.object(
            self.manager, '_monitor_qr_status', new_callable=AsyncMock
        ):
            result = await self.manager.generate_qr_code()
            self.assertTrue(result['success'], result.get('message'))
            image = base64.b64decode(result['qr_code_url'].split(',', 1)[1])
            self.assertTrue(image.startswith(b'\x89PNG\r\n\x1a\n'))
            session = self.manager.sessions[result['session_id']]
            response = await self.manager._poll_qrcode_status(session)
            self.assertEqual(response.json()['content']['data']['qrCodeStatus'], 'NEW')
            self.assertEqual(session.params['ck'], 'test-key')
        self.assertEqual(self.server.paths, [
            ('GET', '/token'), ('POST', '/token'), ('GET', '/login'),
            ('GET', '/generate'), ('POST', '/query'),
        ])

    async def test_http_error_is_actionable_without_response_or_url_secrets(self):
        self.manager.api_h5_tk = self.base_url + '/denied?secret=hidden'
        result = await self.manager.generate_qr_code()
        self.assertFalse(result['success'])
        self.assertIn('HTTP 403', result['message'])
        self.assertNotIn('hidden', result['message'])
        self.assertNotIn('private-upstream-response', result['message'])
        self.assertEqual(self.manager.sessions, {})

    async def test_non_json_qr_response_does_not_leak_body(self):
        self.manager.api_generate_qr = self.base_url + '/invalid-json'
        result = await self.manager.generate_qr_code()
        self.assertFalse(result['success'])
        self.assertNotIn('private-upstream-response', result['message'])
        self.assertEqual(self.manager.sessions, {})

    async def test_connection_failure_recommends_domestic_direct_network(self):
        with patch.object(self.manager, '_get_mh5tk', side_effect=httpx.ConnectError('hidden')):
            result = await self.manager.generate_qr_code()
        self.assertIn('国内网络', result['message'])
        self.assertNotIn('hidden', result['message'])
        self.assertNotIn('使用代理', result['message'])

    async def test_aiohttp_default_is_direct_under_poisoned_environment(self):
        with patch.dict(os.environ, POISONED_PROXY):
            async with aiohttp.ClientSession() as session:
                self.assertFalse(session.trust_env)
                async with session.get(self.base_url + '/token') as response:
                    self.assertEqual(response.status, 200)

    async def test_pinned_websocket_client_is_direct_under_poisoned_environment(self):
        async def echo(socket, _path):
            await socket.send(await socket.recv())

        with patch.dict(os.environ, POISONED_PROXY):
            async with websockets.serve(echo, '127.0.0.1', 0) as server:
                port = server.sockets[0].getsockname()[1]
                async with websockets.connect(f'ws://127.0.0.1:{port}', open_timeout=2) as client:
                    await client.send('direct')
                    self.assertEqual(await client.recv(), 'direct')

    async def test_cookie_refresh_requests_session_is_direct(self):
        from utils.refresh_util import XianyuApis

        def request():
            with XianyuApis().session as session:
                self.assertFalse(session.trust_env)
                return session.get(self.base_url + '/token', timeout=2).status_code

        with patch.dict(os.environ, POISONED_PROXY):
            self.assertEqual(await asyncio.to_thread(request), 200)


if __name__ == '__main__':
    unittest.main()
