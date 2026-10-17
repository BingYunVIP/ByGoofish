# -*- coding: utf-8 -*-
"""AI 网关客户端：地址规范化、协议顺序、降级规则、请求格式与模型列表。

用本机真实的 HTTP 服务模拟中转站的各个接口（不替换 httpx），逐条检查实际发出的请求。
真实中转站的验收见 scripts/ai_smoke.py。
"""

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.services import ai_client
from app.services.ai_client import (
    AIClient,
    AIConfigError,
    AIRequestError,
    ReasoningBudgetExhausted,
    fetch_models,
    is_default_base_url,
    normalize_base_url,
    resolve_base_url,
)

MESSAGES = [
    {'role': 'system', 'content': '你是卖家客服。'},
    {'role': 'assistant', 'content': '你好，需要什么？'},   # 开头的助手轮次：Messages API 要求先丢掉
    {'role': 'user', 'content': '有货吗'},
    {'role': 'user', 'content': '能便宜吗'},               # 连续的用户轮次要合并
]


def chat_ok(text='好的'):
    return 200, {'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}]}, {}


def responses_ok(text='好的'):
    return 200, {'status': 'completed', 'output': [
        {'type': 'reasoning', 'summary': []},
        {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}]}, {}


def messages_ok(text='好的'):
    return 200, {'type': 'message', 'stop_reason': 'end_turn', 'content': [
        {'type': 'thinking', 'thinking': ''}, {'type': 'text', 'text': text}]}, {}


def error(status, message='boom'):
    return status, {'error': {'message': message}}, {}


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(('127.0.0.1', 0), self.Handler)
        self.requests = []
        self.routes = {}
        threading.Thread(target=self.serve_forever, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_any(self):
            length = int(self.headers.get('content-length') or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            path = self.path.split('?')[0]
            record = {'method': self.command, 'path': path, 'query': self.path[len(path):],
                      'headers': {k.lower(): v for k, v in self.headers.items()}, 'body': body}
            self.server.requests.append(record)
            route = self.server.routes.get((self.command, path))
            status, payload, headers = route(record) if route else error(404, 'not found')
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('content-type', 'application/json')
            self.send_header('content-length', str(len(data)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = handle_any

    @property
    def base_url(self):
        return f'http://127.0.0.1:{self.server_address[1]}/v1'

    def paths(self):
        return [record['path'] for record in self.requests]


class AIClientTestCase(unittest.TestCase):
    def setUp(self):
        ai_client._protocol_memory.clear()
        ai_client._no_temperature.clear()
        self.server = RelayServer()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def route(self, method, path, handler):
        self.server.routes[(method, path)] = handler if callable(handler) else (lambda request: handler)

    def client(self, **kwargs):
        return AIClient(self.server.base_url, 'sk-test-key-0123456789', **kwargs)

    def complete(self, model='gpt-test', **kwargs):
        return self.client(**kwargs).complete(model, MESSAGES, max_tokens=300)


class NormalizeUrlTests(unittest.TestCase):
    def test_v1_is_appended_when_missing(self):
        cases = {
            'api.bingyun.vip': 'https://api.bingyun.vip/v1',
            'https://api.bingyun.vip': 'https://api.bingyun.vip/v1',
            'https://api.bingyun.vip/': 'https://api.bingyun.vip/v1',
            'https://openrouter.ai/api': 'https://openrouter.ai/api/v1',
            'http://localhost:11434': 'http://localhost:11434/v1',
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_base_url(raw), expected, raw)

    def test_existing_version_segments_are_kept(self):
        for url in ('https://api.bingyun.vip/v1', 'https://open.bigmodel.cn/api/paas/v4',
                    'https://generativelanguage.googleapis.com/v1beta/openai',
                    'https://dashscope.aliyuncs.com/compatible-mode/v1'):
            self.assertEqual(normalize_base_url(url + '/'), url)

    def test_pasted_endpoint_urls_are_trimmed(self):
        for suffix in ('/chat/completions', '/responses', '/messages', '/models', '/completions'):
            self.assertEqual(normalize_base_url('https://api.bingyun.vip/v1' + suffix), 'https://api.bingyun.vip/v1')
        self.assertEqual(normalize_base_url('https://x.com/v1?key=1#frag'), 'https://x.com/v1')

    def test_invalid_addresses_are_rejected(self):
        for raw in ('ftp://x.com', 'https://user:pass@x.com/v1', 'https://', 'http://[::1', 'https://x.com:99999'):
            with self.assertRaises(AIConfigError, msg=raw):
                normalize_base_url(raw)

    def test_default_and_legacy_default(self):
        self.assertEqual(resolve_base_url(''), 'https://api.bingyun.vip/v1')
        self.assertEqual(resolve_base_url('https://ai.corleom.com/v1'), 'https://api.bingyun.vip/v1')
        self.assertTrue(is_default_base_url('https://ai.corleom.com/v1'))
        self.assertTrue(is_default_base_url(''))
        self.assertFalse(is_default_base_url('https://example.com/v1'))

    def test_api_key_is_validated(self):
        for key in ('', '   ', 'sk-has space', 'sk-中文', 'sk-line\nbreak'):
            with self.assertRaises(AIConfigError, msg=repr(key)):
                AIClient('https://api.bingyun.vip/v1', key)


class ProtocolOrderTests(AIClientTestCase):
    def test_non_claude_model_uses_responses_first(self):
        self.route('POST', '/v1/responses', responses_ok('有货'))
        self.assertEqual(self.complete('gpt-6-sol', user_agent='codex-test/1.0'), '有货')
        self.assertEqual(self.server.paths(), ['/v1/responses'])
        request = self.server.requests[0]
        body = request['body']
        self.assertEqual(request['headers']['authorization'], 'Bearer sk-test-key-0123456789')
        self.assertEqual(request['headers']['user-agent'], 'codex-test/1.0')
        self.assertNotIn('x-api-key', request['headers'])
        self.assertEqual(body['instructions'], '你是卖家客服。')
        self.assertEqual([turn['role'] for turn in body['input']], ['assistant', 'user', 'user'])
        self.assertIs(body['store'], False)
        self.assertEqual(body['max_output_tokens'], 300)
        self.assertEqual(body['temperature'], 0.7)

    def test_claude_model_uses_messages_first_with_anthropic_format(self):
        self.route('POST', '/v1/messages', messages_ok('可以拍'))
        self.assertEqual(self.complete('claude-sonnet-5-5'), '可以拍')  # thinking 块被忽略
        self.assertEqual(self.server.paths(), ['/v1/messages'])
        request = self.server.requests[0]
        body = request['body']
        self.assertEqual(request['headers']['x-api-key'], 'sk-test-key-0123456789')
        self.assertEqual(request['headers']['anthropic-version'], '2023-06-01')
        self.assertEqual(body['system'], '你是卖家客服。')
        # 先丢掉开头的助手轮次，再合并连续的用户轮次
        self.assertEqual(body['messages'], [{'role': 'user', 'content': '有货吗\n能便宜吗'}])
        self.assertEqual(body['max_tokens'], 300)
        self.assertNotIn('temperature', body)  # 新版 Claude 模型不接受采样参数

    def test_model_name_match_is_case_insensitive_and_covers_prefixed_names(self):
        for model in ('Claude-3-5-Haiku', 'anthropic/claude-opus-5'):
            self.assertEqual(ai_client.preferred_protocol(model), 'messages')
        for model in ('gpt-6-sol', 'qwen-plus', 'gemini-2.5-pro', 'deepseek-chat'):
            self.assertEqual(ai_client.preferred_protocol(model), 'responses')


class FallbackTests(AIClientTestCase):
    def test_responses_failure_falls_back_to_chat_and_is_remembered(self):
        self.route('POST', '/v1/responses', error(404, 'no such endpoint'))
        self.route('POST', '/v1/chat/completions', chat_ok('降级成功'))
        self.assertEqual(self.complete('gpt-test'), '降级成功')
        self.assertEqual(self.server.paths(), ['/v1/responses', '/v1/chat/completions'])
        # 之后一段时间直接走可用协议，不再每条消息都先失败一次
        self.assertEqual(self.complete('gpt-test'), '降级成功')
        self.assertEqual(self.server.paths()[2:], ['/v1/chat/completions'])
        # 记忆过期后重新尝试首选协议；它恢复了就回到首选
        ai_client._protocol_memory.clear()
        self.route('POST', '/v1/responses', responses_ok('恢复'))
        self.assertEqual(self.complete('gpt-test'), '恢复')
        self.assertEqual(self.complete('gpt-test'), '恢复')
        self.assertEqual(self.server.paths()[3:], ['/v1/responses', '/v1/responses'])

    def test_messages_403_falls_back_to_chat(self):
        """真实中转站对这类分组返回 403：This group does not allow /v1/messages dispatch。"""
        self.route('POST', '/v1/messages', (403, {'error': {'message': 'This group does not allow /v1/messages dispatch'}}, {}))
        self.route('POST', '/v1/chat/completions', chat_ok('走了 chat'))
        self.assertEqual(self.complete('claude-opus-5-5'), '走了 chat')
        self.assertEqual(self.server.paths(), ['/v1/messages', '/v1/chat/completions'])

    def test_server_errors_and_unparseable_bodies_fall_back(self):
        for failing in (error(502, 'bad gateway'), error(400, 'unsupported'), (200, {'weird': True}, {})):
            ai_client._protocol_memory.clear()
            self.server.requests.clear()
            self.route('POST', '/v1/responses', failing)
            self.route('POST', '/v1/chat/completions', chat_ok('ok'))
            self.assertEqual(self.complete('gpt-test'), 'ok', failing)
            self.assertEqual(self.server.paths(), ['/v1/responses', '/v1/chat/completions'])

    def test_auth_failure_and_rate_limit_do_not_fall_back(self):
        for status in (401, 429):
            self.server.requests.clear()
            self.route('POST', '/v1/responses', error(status, 'nope'))
            self.route('POST', '/v1/chat/completions', chat_ok('不应被调用'))
            with self.assertRaises(AIRequestError) as ctx:
                self.complete('gpt-test')
            self.assertEqual(ctx.exception.status_code, status)
            self.assertEqual(self.server.paths(), ['/v1/responses'])

    def test_exhausted_reasoning_budget_does_not_fall_back(self):
        self.route('POST', '/v1/responses', (200, {'status': 'incomplete', 'incomplete_details': {'reason': 'max_output_tokens'},
                                                    'output': [{'type': 'reasoning'}]}, {}))
        self.route('POST', '/v1/chat/completions', chat_ok('不应被调用'))
        with self.assertRaises(ReasoningBudgetExhausted):
            self.complete('gpt-test')
        self.assertEqual(self.server.paths(), ['/v1/responses'])

    def test_refusal_does_not_fall_back(self):
        self.route('POST', '/v1/messages', (200, {'type': 'message', 'stop_reason': 'refusal', 'content': []}, {}))
        self.route('POST', '/v1/chat/completions', chat_ok('不应被调用'))
        with self.assertRaises(AIRequestError) as ctx:
            self.complete('claude-opus-5-5')
        self.assertEqual(ctx.exception.kind, 'refusal')
        self.assertEqual(self.server.paths(), ['/v1/messages'])

    def test_both_failing_reports_each_protocol(self):
        self.route('POST', '/v1/responses', error(502, 'upstream down'))
        self.route('POST', '/v1/chat/completions', error(503, 'overloaded'))
        with self.assertRaises(AIRequestError) as ctx:
            self.complete('gpt-test')
        message = str(ctx.exception)
        self.assertIn('/responses', message)
        self.assertIn('/chat/completions', message)
        self.assertIn('upstream down', message)
        self.assertEqual(ctx.exception.status_code, 503)  # 保留最后一次失败的状态码，供上层判断是否重试
        self.assertNotIn('sk-test-key', message)
        self.assertEqual(ai_client._remembered_protocol(self.server.base_url, 'gpt-test'), None)

    def test_redirects_are_not_followed(self):
        self.route('POST', '/v1/responses', (302, {}, {'location': '/elsewhere'}))
        self.route('POST', '/v1/chat/completions', chat_ok('ok'))
        self.assertEqual(self.complete('gpt-test'), 'ok')
        self.assertNotIn('/elsewhere', self.server.paths())

    def test_timeout_is_classified_and_bounded(self):
        self.route('POST', '/v1/responses', lambda request: (time.sleep(1.5), responses_ok())[1])
        self.route('POST', '/v1/chat/completions', lambda request: (time.sleep(1.5), chat_ok())[1])
        started = time.monotonic()
        with self.assertRaises(AIRequestError) as ctx:
            self.client(timeout=0.4, total_timeout=0.8).complete('gpt-test', MESSAGES, max_tokens=50)
        self.assertEqual(ctx.exception.kind, 'timeout')
        self.assertLess(time.monotonic() - started, 1.4)


class ParameterAdjustTests(AIClientTestCase):
    def test_temperature_is_dropped_when_the_model_rejects_it(self):
        def responses(request):
            if 'temperature' in request['body']:
                return error(400, "Unsupported parameter: 'temperature' is not supported with this model.")
            return responses_ok('没有温度也行')
        self.route('POST', '/v1/responses', responses)
        self.assertEqual(self.complete('gpt-test'), '没有温度也行')
        self.assertEqual(len(self.server.requests), 2)
        self.complete('gpt-test')
        self.assertNotIn('temperature', self.server.requests[-1]['body'])  # 记住了，不再带

    def test_chat_max_tokens_is_renamed_when_required(self):
        def chat(request):
            if 'max_tokens' in request['body']:
                return error(400, "Use 'max_completion_tokens' instead of 'max_tokens'")
            return chat_ok('改名成功')
        self.route('POST', '/v1/responses', error(404))
        self.route('POST', '/v1/chat/completions', chat)
        self.assertEqual(self.complete('gpt-test'), '改名成功')
        self.assertEqual(self.server.requests[-1]['body']['max_completion_tokens'], 300)


class ResponseParsingTests(AIClientTestCase):
    def test_chat_content_parts_and_reasoning_are_handled(self):
        self.route('POST', '/v1/responses', error(404))
        self.route('POST', '/v1/chat/completions', (200, {'choices': [{'message': {
            'content': [{'type': 'text', 'text': '甲'}, {'type': 'text', 'text': '乙'}],
            'reasoning_content': '这是思维链，绝不能当作回复'}, 'finish_reason': 'stop'}]}, {}))
        self.assertEqual(self.complete('gpt-test'), '甲乙')

    def test_chat_length_without_content_is_budget_exhaustion(self):
        self.route('POST', '/v1/responses', error(404))
        self.route('POST', '/v1/chat/completions', (200, {'choices': [{'message': {'content': None,
                                                                                   'reasoning_content': '思考中'},
                                                                      'finish_reason': 'length'}]}, {}))
        with self.assertRaises(ReasoningBudgetExhausted):
            self.complete('gpt-test')

    def test_messages_max_tokens_without_text_is_budget_exhaustion(self):
        self.route('POST', '/v1/messages', (200, {'type': 'message', 'stop_reason': 'max_tokens',
                                                   'content': [{'type': 'thinking', 'thinking': ''}]}, {}))
        with self.assertRaises(ReasoningBudgetExhausted):
            self.complete('claude-opus-5-5')

    def test_error_text_is_redacted(self):
        self.route('POST', '/v1/responses', error(400, 'Incorrect API key provided: sk-proj-abcdefghijklmnopqrstuv'))
        self.route('POST', '/v1/chat/completions', error(400, 'x-api-key: sk-abcdefghijklmnopqrstuvwx'))
        with self.assertRaises(AIRequestError) as ctx:
            self.complete('gpt-test')
        self.assertNotIn('sk-proj', str(ctx.exception))
        self.assertNotIn('sk-abcdefgh', str(ctx.exception))

    def test_no_user_message_is_rejected_before_sending(self):
        with self.assertRaises(AIRequestError):
            self.client().complete('gpt-test', [{'role': 'system', 'content': 'x'}], max_tokens=50)
        self.assertEqual(self.server.requests, [])


class ListModelsTests(AIClientTestCase):
    def test_openai_style_list_is_sorted_and_deduplicated(self):
        self.route('GET', '/v1/models', (200, {'object': 'list', 'data': [
            {'id': 'gpt-6-sol'}, {'id': 'Claude-x'}, {'id': 'gpt-6-sol'}, {'id': ''}, 'gpt-5']}, {}))
        self.assertEqual(self.client().list_models(), ['Claude-x', 'gpt-5', 'gpt-6-sol'])
        headers = self.server.requests[0]['headers']
        self.assertEqual(headers['authorization'], 'Bearer sk-test-key-0123456789')
        self.assertEqual(headers['x-api-key'], 'sk-test-key-0123456789')  # Anthropic 风格的 /models 也能读

    def test_anthropic_style_pagination_is_followed(self):
        def models(request):
            if 'after_id' in request['query']:
                return 200, {'data': [{'id': 'claude-b'}], 'has_more': False}, {}
            return 200, {'data': [{'id': 'claude-a'}], 'has_more': True, 'last_id': 'claude-a'}, {}
        self.route('GET', '/v1/models', models)
        self.assertEqual(self.client().list_models(), ['claude-a', 'claude-b'])
        self.assertIn('after_id=claude-a', self.server.requests[1]['query'])

    def test_gemini_style_names_and_models_key(self):
        self.route('GET', '/v1/models', (200, {'models': [{'name': 'models/gemini-2.5-pro'}]}, {}))
        self.assertEqual(self.client().list_models(), ['gemini-2.5-pro'])

    def test_missing_list_and_http_errors_are_reported(self):
        self.route('GET', '/v1/models', (200, {'unexpected': 1}, {}))
        with self.assertRaises(AIRequestError):
            self.client().list_models()
        self.route('GET', '/v1/models', error(401, 'Invalid API key'))
        with self.assertRaises(AIRequestError) as ctx:
            self.client().list_models()
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn('Invalid API key', str(ctx.exception))

    def test_fetch_models_blocks_private_targets_for_non_admins(self):
        self.route('GET', '/v1/models', (200, {'data': [{'id': 'm'}]}, {}))
        with self.assertRaises(AIConfigError):
            fetch_models(self.server.base_url, 'sk-test-key-0123456789', allow_private=False)
        self.assertEqual(self.server.requests, [])
        base_url, models = fetch_models(self.server.base_url, 'sk-test-key-0123456789', allow_private=True)
        self.assertEqual((base_url, models), (self.server.base_url, ['m']))


if __name__ == '__main__':
    unittest.main()
