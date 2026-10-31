"""AI 网关客户端：接口地址规范化、协议选择与降级、模型列表。

协议顺序（用户指定）：

* 模型名含 ``claude`` → 先 ``/messages``（Anthropic Messages），不成功再 ``/chat/completions``；
* 其他模型 → 先 ``/responses``（OpenAI Responses），不成功再 ``/chat/completions``。

降级规则：鉴权失败（401）和限流（429）换接口结果不会变，只会加倍请求，不降级；
「推理预算耗尽」「安全拒答」是模型行为，换接口无济于事，同样不降级；
其余失败（400/403/404/5xx、超时、连接错误、响应无法解析）都降级到 ``/chat/completions``。
降级成功后在内存里记住该（地址, 模型）一段时间，后续直接走可用协议，
避免每条买家消息都先失败一次。

每个协议都优先用 SSE 流式请求（``stream: true``），边收边拼；服务端忽略 stream 直接返回 JSON 时照常处理。
流式不被支持或中途中断时，对同一协议改用一次非流式请求，并记住 30 分钟；
只有流结束事件齐全才使用结果，半截回复绝不外发。

所有请求直连：不读取系统代理环境变量，也不跟随重定向（避免把密钥带到别的主机）。
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

import httpx
from loguru import logger

from utils.log_sanitizer import redact_sensitive_text

DEFAULT_AI_BASE_URL = 'https://api.bingyun.vip/v1'
# 旧版内置的作者中转站地址：等同「未配置」，统一视为默认地址。
LEGACY_DEFAULT_BASE_URLS = frozenset({'https://ai.corleom.com/v1'})
DEFAULT_USER_AGENT = 'codex_cli_rs/0.0.0 (Hermes Agent)'
ANTHROPIC_VERSION = '2023-06-01'

PROTOCOL_MESSAGES = 'messages'
PROTOCOL_RESPONSES = 'responses'
PROTOCOL_CHAT = 'chat/completions'

REQUEST_TIMEOUT = 30.0        # 单次请求
TOTAL_TIMEOUT = 50.0          # 一次生成（含降级）的总时限
MIN_FALLBACK_SECONDS = 5.0    # 剩余时间不足时不再发起降级请求（可由 AIClient 参数覆盖）
PROTOCOL_MEMORY_SECONDS = 30 * 60
MAX_MODEL_PAGES = 5

_ENDPOINT_SUFFIXES = ('/chat/completions', '/completions', '/responses', '/messages', '/models')
# v1、v4、v1beta、v1alpha2 这类版本段；路径里已经有就不再追加 /v1。
_VERSION_SEGMENT = re.compile(r'^v\d+(?:alpha|beta)?\d*$', re.IGNORECASE)


class AIConfigError(ValueError):
    """可以直接展示给用户的配置错误（地址格式、不允许的目标等）。"""


class AIRequestError(RuntimeError):
    """一次 AI 接口调用失败。status_code 为 None 表示没有拿到 HTTP 响应。

    kind: http | timeout | network | format | refusal | stream（流式响应中断或流式不被支持）
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None,
                 protocol: str = '', kind: str = 'http') -> None:
        super().__init__(message)
        self.status_code = status_code
        self.protocol = protocol
        self.kind = kind


class ReasoningBudgetExhausted(RuntimeError):
    """推理模型把 max_tokens 全花在思维链上，没给正文留下额度。

    单独立一个类型，是为了让调用方把它和限流、网络抖动区分开：
    这类失败换时间重试没用，得加大预算再试。
    """


# ---------------------------------------------------------------------------
# 地址规范化与出站校验
# ---------------------------------------------------------------------------

def normalize_base_url(raw: Optional[str]) -> str:
    """把用户填写的接口地址整理成「服务根地址」。

    * 没有写协议时补 ``https://``；
    * 粘贴了完整接口地址（``…/chat/completions``、``…/responses`` 等）时去掉接口部分；
    * 路径里没有版本段（v1、v4、v1beta…）时自动追加 ``/v1``；
    * 去掉查询参数、片段和末尾斜杠；
    * 留空返回空串，由调用方决定是否使用默认地址。
    """
    text = (raw or '').strip()
    if not text:
        return ''
    if '://' not in text:
        text = 'https://' + text
    try:
        parts = urlsplit(text)
        hostname = parts.hostname
        parts.port  # noqa: B018 - 触发端口合法性校验
    except ValueError:
        raise AIConfigError('接口地址格式不正确，请填写 https://域名/v1 这样的地址') from None
    if parts.scheme.lower() not in ('http', 'https') or not hostname:
        raise AIConfigError('接口地址格式不正确，请填写 https://域名/v1 这样的地址')
    if parts.username or parts.password:
        raise AIConfigError('接口地址不能包含账号或密码，请把密钥填到 API Key')
    path = parts.path.rstrip('/')
    for suffix in _ENDPOINT_SUFFIXES:
        if path.lower().endswith(suffix):
            path = path[:-len(suffix)].rstrip('/')
            break
    if not any(_VERSION_SEGMENT.match(segment) for segment in path.split('/') if segment):
        path += '/v1'
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, '', ''))


def resolve_base_url(raw: Optional[str]) -> str:
    """规范化后的地址；留空或仍是旧默认值时使用当前默认地址。"""
    normalized = normalize_base_url(raw)
    if not normalized or normalized in LEGACY_DEFAULT_BASE_URLS:
        return DEFAULT_AI_BASE_URL
    return normalized


def is_default_base_url(raw: Optional[str]) -> bool:
    """是否等同于「未配置」：空、当前默认地址或旧版内置默认地址。"""
    try:
        return resolve_base_url(raw) == DEFAULT_AI_BASE_URL
    except AIConfigError:
        return False


def validate_outbound_host(host: str, port: int, *, allow_private: bool = False,
                           require_resolution: bool = True) -> Optional[bool]:
    """出站目标检查：防止把服务器当跳板访问内网或云厂商元数据服务。

    * 链路本地（含 169.254.169.254 元数据地址）、组播、未指定地址一律拒绝；
    * 本机和内网地址只允许管理员使用。
    返回目标是否为本机/内网地址；域名暂时解析不了且 require_resolution=False 时返回 None
    （保存配置不应因临时断网失败）。这是尽力而为的检查：域名在检查之后再改指向其他地址的情况无法完全杜绝。
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        if not require_resolution:
            return None
        raise AIConfigError('无法解析服务器域名，请检查拼写和网络') from None
    addresses = []
    for info in infos:
        address = ipaddress.ip_address(str(info[4][0]).split('%', 1)[0])
        if address.version == 6 and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        addresses.append(address)
    if not addresses:
        raise AIConfigError('无法解析服务器域名，请检查拼写和网络')
    if any(a.is_link_local or a.is_multicast or a.is_unspecified for a in addresses):
        raise AIConfigError('目标地址不能是链路本地或保留地址')
    private = any(a.is_private or a.is_loopback for a in addresses)
    if private and not allow_private:
        raise AIConfigError('目标地址指向本机或内网，仅管理员可以使用')
    return private


def validate_outbound_url(url: str, *, allow_private: bool = False, require_resolution: bool = True) -> None:
    """校验 AI 接口地址：主机规则同 validate_outbound_host，另外公网地址必须使用 https，避免密钥明文传输。"""
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    private = validate_outbound_host(parts.hostname or '', port, allow_private=allow_private,
                                     require_resolution=require_resolution)
    if parts.scheme == 'http' and private is False:
        raise AIConfigError('公网接口必须使用 https，避免 API Key 明文传输')


# ---------------------------------------------------------------------------
# 协议选择与记忆
# ---------------------------------------------------------------------------

_memory_lock = threading.Lock()
_protocol_memory: Dict[Tuple[str, str], Tuple[str, float]] = {}
_no_temperature: set = set()


def preferred_protocol(model: str) -> str:
    return PROTOCOL_MESSAGES if 'claude' in (model or '').lower() else PROTOCOL_RESPONSES


def _remembered_protocol(base_url: str, model: str) -> Optional[str]:
    key = (base_url, model)
    with _memory_lock:
        entry = _protocol_memory.get(key)
        if entry and entry[1] > time.monotonic():
            return entry[0]
        _protocol_memory.pop(key, None)
    return None


def _remember_protocol(base_url: str, model: str, protocol: str) -> None:
    key = (base_url, model)
    now = time.monotonic()
    with _memory_lock:
        if protocol == preferred_protocol(model):
            _protocol_memory.pop(key, None)
            return
        entry = _protocol_memory.get(key)
        if entry and entry[0] == protocol and entry[1] > now:
            return  # 不续期：到期后会重新尝试首选协议，它恢复了就不用一直降级
        _protocol_memory[key] = (protocol, now + PROTOCOL_MEMORY_SECONDS)


def _forget_protocol(base_url: str, model: str) -> None:
    with _memory_lock:
        _protocol_memory.pop((base_url, model), None)


# 只认单词 stream / streaming / 流式；网关错误里常见的 upstream 不是这个意思
_STREAM_WORD = re.compile(r'\bstream(?:s|ing|ed)?\b|流式')

_plain_memory: Dict[Tuple[str, str, str], float] = {}


def _stream_disabled(base_url: str, model: str, protocol: str) -> bool:
    """最近这个（地址, 模型, 协议）的流式请求失败、非流式成功过：一段时间内直接用非流式。"""
    key = (base_url, model, protocol)
    with _memory_lock:
        expires = _plain_memory.get(key)
        if expires and expires > time.monotonic():
            return True
        _plain_memory.pop(key, None)
    return False


def _disable_stream(base_url: str, model: str, protocol: str) -> None:
    with _memory_lock:
        _plain_memory.setdefault((base_url, model, protocol), time.monotonic() + PROTOCOL_MEMORY_SECONDS)


def _stream_unsupported(exc: 'AIRequestError') -> bool:
    """这次流式失败是不是「流式本身的问题」：是的话同一协议再用非流式试一次。"""
    if exc.kind == 'stream':
        return True
    return exc.status_code in (400, 406, 415, 422) and bool(_STREAM_WORD.search(str(exc).lower()))


def plan_protocols(base_url: str, model: str) -> List[str]:
    """本次调用依次尝试的协议。最近降级成功过就先用降级协议，失败再回到首选协议。"""
    preferred = preferred_protocol(model)
    if _remembered_protocol(base_url, model) == PROTOCOL_CHAT:
        return [PROTOCOL_CHAT, preferred]
    return [preferred, PROTOCOL_CHAT]


def _should_fallback(exc: Exception) -> bool:
    if isinstance(exc, ReasoningBudgetExhausted):
        return False
    if isinstance(exc, AIRequestError):
        return exc.kind != 'refusal' and exc.status_code not in (401, 429)
    return False


# ---------------------------------------------------------------------------
# 请求体与响应解析
# ---------------------------------------------------------------------------

def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return '' if content is None else str(content)


def split_messages(messages: Sequence[Dict[str, Any]]) -> Tuple[str, List[Dict[str, str]]]:
    """拆出系统提示与对话轮次；空内容和未知角色直接忽略。"""
    system_parts: List[str] = []
    turns: List[Dict[str, str]] = []
    for message in messages:
        role = message.get('role')
        content = _text_of(message.get('content')).strip()
        if not content:
            continue
        if role == 'system':
            system_parts.append(content)
        elif role in ('user', 'assistant'):
            turns.append({'role': role, 'content': content})
    return '\n\n'.join(system_parts), turns


def _anthropic_turns(turns: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Messages API 要求从用户轮次开始，且新模型不接受以助手轮次结尾（预填充已移除）。"""
    merged: List[Dict[str, str]] = []
    for turn in turns:
        if merged and merged[-1]['role'] == turn['role']:
            merged[-1] = {'role': turn['role'], 'content': merged[-1]['content'] + '\n' + turn['content']}
        else:
            merged.append(dict(turn))
    while merged and merged[0]['role'] != 'user':
        merged.pop(0)
    while merged and merged[-1]['role'] != 'user':
        merged.pop()
    return merged


def build_body(protocol: str, model: str, messages: Sequence[Dict[str, Any]], *,
               max_tokens: int, temperature: Optional[float]) -> Dict[str, Any]:
    system, turns = split_messages(messages)
    if protocol == PROTOCOL_MESSAGES:
        turns = _anthropic_turns(turns)
    if not any(turn['role'] == 'user' for turn in turns):
        raise AIRequestError('没有可发送给模型的用户消息', protocol=protocol, kind='format')
    if protocol == PROTOCOL_MESSAGES:
        # 新版 Claude 模型不再接受 temperature 等采样参数，一律不传。
        body: Dict[str, Any] = {'model': model, 'max_tokens': int(max_tokens), 'messages': turns}
        if system:
            body['system'] = system
        return body
    if protocol == PROTOCOL_RESPONSES:
        body = {'model': model, 'input': turns, 'max_output_tokens': max(16, int(max_tokens)), 'store': False}
        if system:
            body['instructions'] = system
    else:
        body = {'model': model, 'max_tokens': int(max_tokens),
                'messages': ([{'role': 'system', 'content': system}] if system else []) + turns}
    if temperature is not None:
        body['temperature'] = temperature
    return body


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def extract_chat_text(response: Any, protocol: str = PROTOCOL_CHAT) -> str:
    """从 /chat/completions 响应里取正文（兼容字典和带属性的对象）。

    不能直接 ``choices[0].message.content.strip()``：content 为 None 的情况很常见。
    已知会返回空正文的场景：回复被 max_tokens 截断、命中内容过滤、推理模型把预算花在思维链上。

    绝对不能拿 reasoning_content 当正文兜底：它装的是思维链而非答案。曾经这里有一句
    「content 为空就取 reasoning_content」，结果原始思维链被当成回复发给了买家，
    连议价底价都一并泄露。答案永远只在 content 里。
    """
    choices = _field(response, 'choices')
    if not choices:
        raise AIRequestError('AI 返回内容为空（choices 为空）', protocol=protocol, kind='format')
    choice = choices[0]
    message = _field(choice, 'message')
    content = _field(message, 'content') if message else None
    if isinstance(content, list):
        content = ''.join(_text_of(_field(part, 'text')) for part in content)
    if not content:
        finish_reason = _field(choice, 'finish_reason')
        if finish_reason == 'length':
            raise ReasoningBudgetExhausted('AI 回复被 max_tokens 截断且未返回正文（推理模型把预算用在思维链上）')
        if finish_reason == 'content_filter':
            raise AIRequestError('AI 回复被服务端内容过滤拦截', protocol=protocol, kind='refusal')
        raise AIRequestError(f'AI 返回内容为空（finish_reason={finish_reason}）', protocol=protocol, kind='format')
    return str(content).strip()


def _extract_responses_text(data: Dict[str, Any]) -> str:
    texts: List[str] = []
    refused = False
    # reasoning 等输出项不是正文；有 phase 的模型会先输出 commentary（过程说明），只有 final_answer 才是给买家的回复
    messages = [item for item in data.get('output') or [] if isinstance(item, dict) and item.get('type') == 'message']
    if any(item.get('phase') == 'final_answer' for item in messages):
        messages = [item for item in messages if item.get('phase') == 'final_answer']
    else:
        messages = [item for item in messages if item.get('phase') != 'commentary']
    for item in messages:
        for part in item.get('content') or []:
            if not isinstance(part, dict):
                continue
            if part.get('type') in ('output_text', 'text') and part.get('text'):
                texts.append(str(part['text']))
            elif part.get('type') == 'refusal':
                refused = True
    text = ''.join(texts).strip() or _text_of(data.get('output_text')).strip()
    if text:
        return text
    if refused:
        raise AIRequestError('AI 拒绝回复（安全策略）', protocol=PROTOCOL_RESPONSES, kind='refusal')
    details = data.get('incomplete_details')
    reason = details.get('reason') if isinstance(details, dict) else None
    if reason == 'content_filter':
        raise AIRequestError('AI 回复被服务端内容过滤拦截', protocol=PROTOCOL_RESPONSES, kind='refusal')
    if data.get('status') == 'incomplete' and reason in (None, 'max_output_tokens'):
        raise ReasoningBudgetExhausted('AI 回复被 max_output_tokens 截断且未返回正文（推理模型把预算用在思维链上）')
    error = data.get('error')
    if error:
        raise AIRequestError(_error_message(error) or 'AI 返回错误', protocol=PROTOCOL_RESPONSES, kind='format')
    raise AIRequestError(f"AI 返回内容为空（status={data.get('status')}）",
                         protocol=PROTOCOL_RESPONSES, kind='format')


def _extract_messages_text(data: Dict[str, Any]) -> str:
    if data.get('type') == 'error' or data.get('error'):
        raise AIRequestError(_error_message(data.get('error')) or 'AI 返回错误',
                             protocol=PROTOCOL_MESSAGES, kind='format')
    blocks = data.get('content') or []
    # 开启思考的模型会返回 thinking 块，只取 text 块。
    text = ''.join(_text_of(block.get('text')) for block in blocks
                   if isinstance(block, dict) and block.get('type') == 'text').strip()
    if text:
        return text
    stop_reason = data.get('stop_reason')
    if stop_reason == 'max_tokens':
        raise ReasoningBudgetExhausted('AI 回复被 max_tokens 截断且未返回正文（推理模型把预算用在思维链上）')
    if stop_reason == 'refusal':
        raise AIRequestError('AI 拒绝回复（安全策略）', protocol=PROTOCOL_MESSAGES, kind='refusal')
    raise AIRequestError(f'AI 返回内容为空（stop_reason={stop_reason}）', protocol=PROTOCOL_MESSAGES, kind='format')


def extract_text(protocol: str, data: Dict[str, Any]) -> str:
    if protocol == PROTOCOL_MESSAGES:
        return _extract_messages_text(data)
    if protocol == PROTOCOL_RESPONSES:
        return _extract_responses_text(data)
    return extract_chat_text(data, protocol)


def _error_message(error: Any) -> str:
    if isinstance(error, dict):
        error = error.get('message') or error.get('detail') or error.get('code') or ''
    return str(error or '').strip()


def describe_http_error(response: httpx.Response) -> str:
    """面向用户的错误说明：状态码 + 接口返回的错误信息（脱敏并截断）。"""
    message = ''
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        message = _error_message(body.get('error')) or _error_message(body.get('message')) \
            or _error_message(body.get('detail'))
    if not message:
        message = (response.text or '').strip().replace('\n', ' ')
    message = redact_sensitive_text(message)[:200]
    return f'接口返回 HTTP {response.status_code}' + (f'：{message}' if message else '')


def _clean_api_key(api_key: Optional[str]) -> str:
    key = (api_key or '').strip()
    if not key:
        raise AIConfigError('请先填写 API Key')
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) > 126 for ch in key):
        raise AIConfigError('API Key 含有非法字符，请重新复制')
    return key


# ---------------------------------------------------------------------------
# SSE 流式响应
# ---------------------------------------------------------------------------

_DONE = object()


def _stream_error(message: str, protocol: str) -> 'AIRequestError':
    message = redact_sensitive_text(message or 'AI 返回错误')[:200]
    return AIRequestError(message, protocol=protocol, kind='stream' if _STREAM_WORD.search(message.lower()) else 'http')


def _json_body(response: httpx.Response, protocol: str) -> Dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        raise AIRequestError('响应不是有效的 JSON', status_code=response.status_code,
                             protocol=protocol, kind='format') from None
    if not isinstance(data, dict):
        raise AIRequestError('响应格式不正确', status_code=response.status_code, protocol=protocol, kind='format')
    return data


def _sse_events(lines: Iterator[str], deadline: float, protocol: str) -> Iterator[Tuple[Optional[str], str]]:
    """把 SSE 文本行切成（事件名, data）。每行都检查总时限，慢吞吞地滴灌也会在时限到时中止。"""
    event: Optional[str] = None
    data: List[str] = []
    for raw in lines:
        if time.monotonic() > deadline:
            raise AIRequestError('请求超时', protocol=protocol, kind='timeout')
        line = raw.rstrip('\r')
        if not line:
            if data:
                yield event, '\n'.join(data)
            event, data = None, []
        elif line.startswith(':'):
            continue  # 注释行，常用作保活心跳
        else:
            field, _, value = line.partition(':')
            value = value[1:] if value.startswith(' ') else value
            if field == 'event':
                event = value
            elif field == 'data':
                data.append(value)
    if data:
        yield event, '\n'.join(data)


def _stream_objects(lines: Iterator[str], deadline: float, protocol: str) -> Iterator[Tuple[Optional[str], Any]]:
    for event, data in _sse_events(lines, deadline, protocol):
        if data.strip() == '[DONE]':
            yield event, _DONE
            return
        try:
            obj = json.loads(data)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield event, obj


def _incomplete(protocol: str) -> 'AIRequestError':
    return AIRequestError('AI 流式响应中断，没有收到结束事件', protocol=protocol, kind='stream')


def _collect_chat(stream: Iterator[Tuple[Optional[str], Any]]) -> Dict[str, Any]:
    parts: List[str] = []
    finish = None
    terminal = False
    for _, obj in stream:
        if obj is _DONE:
            terminal = True
            break
        if obj.get('error'):
            raise _stream_error(_error_message(obj['error']), PROTOCOL_CHAT)
        for choice in obj.get('choices') or []:
            content = (choice.get('delta') or {}).get('content')  # reasoning_content 是思维链，永远不取
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.extend(_text_of(_field(part, 'text')) for part in content)
            finish = choice.get('finish_reason') or finish
    if not (terminal or finish):
        raise _incomplete(PROTOCOL_CHAT)
    return {'choices': [{'message': {'content': ''.join(parts)}, 'finish_reason': finish}]}


def _collect_responses(stream: Iterator[Tuple[Optional[str], Any]]) -> Dict[str, Any]:
    items: Dict[int, Dict[str, Any]] = {}

    def slot(index: Any) -> Dict[str, Any]:
        return items.setdefault(index if isinstance(index, int) else 0,
                                {'type': None, 'phase': None, 'text': [], 'done_text': None, 'refusal': False})

    final: Optional[Dict[str, Any]] = None
    for event, obj in stream:
        kind = obj.get('type') or event or ''
        if kind in ('response.output_item.added', 'response.output_item.done'):
            item = obj.get('item') if isinstance(obj.get('item'), dict) else {}
            entry = slot(obj.get('output_index'))
            entry['type'] = item.get('type') or entry['type']
            entry['phase'] = item.get('phase') or entry['phase']
            if kind.endswith('.done') and isinstance(item.get('content'), list):
                texts = [_text_of(part.get('text')) for part in item['content']
                         if isinstance(part, dict) and part.get('type') in ('output_text', 'text')]
                if texts:
                    entry['done_text'] = ''.join(texts)
        elif kind == 'response.output_text.delta':
            slot(obj.get('output_index'))['text'].append(_text_of(obj.get('delta')))
        elif kind == 'response.output_text.done':
            slot(obj.get('output_index'))['done_text'] = _text_of(obj.get('text'))
        elif kind.startswith('response.refusal.'):
            slot(obj.get('output_index'))['refusal'] = True
        elif kind in ('response.completed', 'response.incomplete', 'response.failed'):
            final = obj.get('response') if isinstance(obj.get('response'), dict) else {}
            break
        elif kind == 'error':
            raise _stream_error(_error_message(obj.get('error') or obj), PROTOCOL_RESPONSES)
    if final is None:
        raise _incomplete(PROTOCOL_RESPONSES)
    output = []
    for index in sorted(items):
        entry = items[index]
        if entry['type'] not in (None, 'message'):
            continue  # reasoning 等不是正文
        text = entry['done_text'] if entry['done_text'] is not None else ''.join(entry['text'])
        content = [{'type': 'refusal'}] if entry['refusal'] and not text.strip() else [{'type': 'output_text', 'text': text}]
        output.append({'type': 'message', 'role': 'assistant', 'phase': entry['phase'], 'content': content})
    response: Dict[str, Any] = {key: final[key] for key in ('status', 'incomplete_details', 'error') if final.get(key)}
    response['output'] = output
    if not any(_text_of(part.get('text')).strip() for item in output for part in item['content']) and final.get('output'):
        response['output'] = final['output']  # 个别中转站的流里没有文本事件，但最终 response 带完整输出
    return response


def _collect_messages(stream: Iterator[Tuple[Optional[str], Any]]) -> Dict[str, Any]:
    blocks: Dict[int, Dict[str, Any]] = {}
    stop = None
    terminal = False
    for event, obj in stream:
        kind = obj.get('type') or event or ''
        if kind == 'content_block_start':
            block = obj.get('content_block') if isinstance(obj.get('content_block'), dict) else {}
            blocks[obj.get('index', 0)] = {'type': block.get('type'), 'text': [_text_of(block.get('text'))]}
        elif kind == 'content_block_delta':
            delta = obj.get('delta') if isinstance(obj.get('delta'), dict) else {}
            if delta.get('type') == 'text_delta':  # thinking_delta 等不是正文
                blocks.setdefault(obj.get('index', 0), {'type': 'text', 'text': []})['text'].append(_text_of(delta.get('text')))
        elif kind == 'message_delta':
            stop = (obj.get('delta') or {}).get('stop_reason') or stop
        elif kind == 'message_stop':
            terminal = True
            break
        elif kind == 'error':
            raise _stream_error(_error_message(obj.get('error') or obj), PROTOCOL_MESSAGES)
    if not (terminal or stop):
        raise _incomplete(PROTOCOL_MESSAGES)
    content = [{'type': 'text', 'text': ''.join(block['text'])} for _, block in sorted(blocks.items())
               if block['type'] == 'text']
    return {'type': 'message', 'stop_reason': stop, 'content': content}


def collect_stream(protocol: str, lines: Iterator[str], deadline: float) -> Dict[str, Any]:
    """读完一条 SSE 流，整理成与非流式响应相同的结构，后面复用同一套正文提取规则。"""
    stream = _stream_objects(lines, deadline, protocol)
    if protocol == PROTOCOL_MESSAGES:
        return _collect_messages(stream)
    if protocol == PROTOCOL_RESPONSES:
        return _collect_responses(stream)
    return _collect_chat(stream)


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class AIClient:
    """面向一个中转站（地址 + 密钥）的无状态客户端；每次调用独立建立连接。"""

    def __init__(self, base_url: Optional[str], api_key: Optional[str], *, user_agent: str = '',
                 timeout: float = REQUEST_TIMEOUT, total_timeout: float = TOTAL_TIMEOUT,
                 min_fallback: float = MIN_FALLBACK_SECONDS,
                 allow_private: bool = True) -> None:
        self.base_url = resolve_base_url(base_url)
        self.api_key = _clean_api_key(api_key)
        agent = (user_agent or '').encode('ascii', 'ignore').decode().strip()
        self.user_agent = agent or DEFAULT_USER_AGENT
        self.timeout = float(timeout)
        self.total_timeout = float(total_timeout)
        self.min_fallback = float(min_fallback)
        self.allow_private = bool(allow_private)

    # -- 内部 ------------------------------------------------------------

    def _headers(self, *, anthropic: bool = False) -> Dict[str, str]:
        headers = {'Authorization': f'Bearer {self.api_key}', 'User-Agent': self.user_agent,
                   'Accept': 'application/json', 'Content-Type': 'application/json'}
        if anthropic:
            headers['x-api-key'] = self.api_key
            headers['anthropic-version'] = ANTHROPIC_VERSION
        return headers

    def _request(self, method: str, protocol: str, url: str, *, timeout: float, anthropic: bool,
                 json_body: Optional[Dict[str, Any]] = None,
                 params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            with httpx.Client(trust_env=False, follow_redirects=False,
                              timeout=httpx.Timeout(timeout, connect=min(10.0, timeout))) as client:
                response = client.request(method, url, headers=self._headers(anthropic=anthropic),
                                          json=json_body, params=params)
        except httpx.TimeoutException:
            raise AIRequestError('请求超时', protocol=protocol, kind='timeout') from None
        except httpx.HTTPError as exc:
            raise AIRequestError(f'网络错误（{type(exc).__name__}）', protocol=protocol, kind='network') from None
        if response.status_code >= 300:
            raise AIRequestError(describe_http_error(response), status_code=response.status_code, protocol=protocol)
        return _json_body(response, protocol)

    def _stream_request(self, protocol: str, url: str, body: Dict[str, Any], *, timeout: float,
                        deadline: float) -> Dict[str, Any]:
        """SSE 流式请求。服务端忽略 stream、直接返回 JSON 时，按普通响应处理。"""
        headers = {**self._headers(anthropic=protocol == PROTOCOL_MESSAGES), 'Accept': 'text/event-stream'}
        streaming = False
        try:
            with httpx.Client(trust_env=False, follow_redirects=False,
                              timeout=httpx.Timeout(timeout, connect=min(10.0, timeout))) as client:
                with client.stream('POST', url, headers=headers, json={**body, 'stream': True}) as response:
                    if response.status_code >= 300:
                        response.read()
                        raise AIRequestError(describe_http_error(response), status_code=response.status_code,
                                             protocol=protocol)
                    if 'text/event-stream' not in response.headers.get('content-type', '').lower():
                        response.read()
                        return _json_body(response, protocol)
                    streaming = True
                    return collect_stream(protocol, response.iter_lines(), deadline)
        except httpx.TimeoutException:
            raise AIRequestError('请求超时', protocol=protocol, kind='timeout') from None
        except httpx.HTTPError as exc:
            # 已经开始收流再出网络错误属于流式中断；连接阶段的错误是普通网络故障
            raise AIRequestError(f'网络错误（{type(exc).__name__}）', protocol=protocol,
                                 kind='stream' if streaming else 'network') from None

    def _call(self, protocol: str, model: str, messages: Sequence[Dict[str, Any]], *,
              max_tokens: int, temperature: float, deadline: float) -> str:
        use_temperature = (self.base_url, model) not in _no_temperature
        body = build_body(protocol, model, messages, max_tokens=max_tokens,
                          temperature=temperature if use_temperature else None)
        url = f'{self.base_url}/{protocol}'
        modes = ['plain'] if _stream_disabled(self.base_url, model, protocol) else ['stream', 'plain']
        for position, mode in enumerate(modes):
            label = '流式' if mode == 'stream' else '非流式'
            adjusted = False
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AIRequestError('请求超时', protocol=protocol, kind='timeout')
                started = time.monotonic()
                timeout = min(self.timeout, remaining)
                try:
                    if mode == 'stream':
                        data = self._stream_request(protocol, url, body, timeout=timeout, deadline=deadline)
                    else:
                        data = self._request('POST', protocol, url, timeout=timeout,
                                             anthropic=protocol == PROTOCOL_MESSAGES, json_body=body)
                    text = extract_text(protocol, data)
                except AIRequestError as exc:
                    if not adjusted and exc.status_code == 400 and self._adjust_body(protocol, body, str(exc), model):
                        adjusted = True  # 个别模型不接受 temperature / max_tokens，去掉或改名后重试一次
                        continue
                    if mode == 'stream' and position + 1 < len(modes) and _stream_unsupported(exc):
                        logger.warning(f'AI流式请求不成功，改用非流式 协议=/{protocol} 模型={model} 原因={exc}')
                        break
                    logger.warning(f'AI请求失败 协议=/{protocol} 方式={label} 模型={model} 原因={exc} '
                                   f'耗时={time.monotonic() - started:.1f}s')
                    raise
                if mode == 'plain' and len(modes) == 2:
                    _disable_stream(self.base_url, model, protocol)  # 流式不行、非流式行：记住一段时间
                logger.info(f'AI请求成功 协议=/{protocol} 方式={label} 模型={model} 耗时={time.monotonic() - started:.1f}s')
                return text
        raise AIRequestError('AI 请求失败', protocol=protocol)

    def _adjust_body(self, protocol: str, body: Dict[str, Any], message: str, model: str) -> bool:
        lowered = message.lower()
        if 'temperature' in body and 'temperature' in lowered:
            body.pop('temperature')
            _no_temperature.add((self.base_url, model))
            return True
        if protocol == PROTOCOL_CHAT and 'max_tokens' in body and 'max_completion_tokens' in lowered:
            body['max_completion_tokens'] = body.pop('max_tokens')
            return True
        return False

    # -- 对外 ------------------------------------------------------------

    def complete(self, model: str, messages: Sequence[Dict[str, Any]], *,
                 max_tokens: int, temperature: float = 0.7) -> str:
        """生成一条回复；按协议顺序尝试，返回正文。"""
        model = (model or '').strip()
        if not model:
            raise AIConfigError('模型名称不能为空')
        validate_outbound_url(self.base_url, allow_private=self.allow_private)
        order = plan_protocols(self.base_url, model)
        deadline = time.monotonic() + self.total_timeout
        failures: List[Tuple[str, Exception]] = []
        for index, protocol in enumerate(order):
            remaining = deadline - time.monotonic()
            if index and remaining < self.min_fallback:
                break
            try:
                text = self._call(protocol, model, messages, max_tokens=max_tokens,
                                  temperature=temperature, deadline=deadline)
            except (AIRequestError, ReasoningBudgetExhausted) as exc:
                failures.append((protocol, exc))
                if not _should_fallback(exc):
                    raise _combine_failures(failures) from None
                if index + 1 < len(order):
                    logger.warning(f'AI协议 /{protocol} 不成功，降级到 /{order[index + 1]}（模型={model}）')
                continue
            _remember_protocol(self.base_url, model, protocol)
            return text
        _forget_protocol(self.base_url, model)
        raise _combine_failures(failures)

    def list_models(self) -> List[str]:
        """读取中转站当前密钥可用的模型 ID（按名称排序）。"""
        url = f'{self.base_url}/models'
        ids: List[str] = []
        params: Optional[Dict[str, Any]] = None
        for _ in range(MAX_MODEL_PAGES):
            data = self._request('GET', 'models', url, timeout=min(self.timeout, 20.0),
                                 anthropic=True, params=params)
            items = data.get('data')
            if not isinstance(items, list):
                items = data.get('models')
            if not isinstance(items, list):
                raise AIRequestError('接口没有返回模型列表', protocol='models', kind='format')
            for item in items:
                name = item if isinstance(item, str) else (
                    _text_of(item.get('id') or item.get('name') or item.get('model')) if isinstance(item, dict) else '')
                name = name.strip()
                if name.startswith('models/'):
                    name = name[len('models/'):]
                if name and name not in ids:
                    ids.append(name)
            last_id = data.get('last_id')
            if not (data.get('has_more') and last_id):
                break
            params = {'after_id': last_id}
        return sorted(ids, key=str.lower)


def _combine_failures(failures: List[Tuple[str, Exception]]) -> Exception:
    """多个协议都失败时保留最后一个异常的类型和状态码，并把各协议的失败原因写进说明。"""
    last = failures[-1][1]
    if len(failures) == 1:
        return last
    summary = '；'.join(f'/{protocol}：{exc}' for protocol, exc in failures)
    if isinstance(last, AIRequestError):
        return AIRequestError(summary, status_code=last.status_code, protocol=last.protocol, kind=last.kind)
    return last


def fetch_models(base_url: Optional[str], api_key: Optional[str], *, user_agent: str = '',
                 allow_private: bool = False) -> Tuple[str, List[str]]:
    """校验目标地址后读取模型列表，返回（规范化后的地址, 模型 ID 列表）。"""
    client = AIClient(base_url, api_key, user_agent=user_agent)
    validate_outbound_url(client.base_url, allow_private=allow_private)
    return client.base_url, client.list_models()
