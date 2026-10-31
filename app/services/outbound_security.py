"""统一的出站 URL 与重定向安全策略。"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import aiohttp

from app.services.ai_client import validate_outbound_url


def validate_external_url(url: str, *, https_only: bool = True) -> str:
    value = str(url or '').strip()
    parts = urlsplit(value)
    if parts.scheme not in {'http', 'https'} or not parts.hostname:
        raise ValueError('出站地址必须是有效的 HTTP/HTTPS URL')
    if https_only and parts.scheme != 'https':
        raise ValueError('出站地址必须使用 HTTPS')
    validate_outbound_url(value, allow_private=False, require_resolution=True)
    return value


def validate_redirect(url: str, location: str, *, https_only: bool = True) -> str:
    target = urljoin(url, location)
    return validate_external_url(target, https_only=https_only)


class PublicResolver(aiohttp.abc.AbstractResolver):
    """解析时拒绝私网、环回、链路本地和保留地址。"""
    def __init__(self):
        self._resolver = aiohttp.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        rows = await self._resolver.resolve(host, port, family)
        for row in rows:
            address = ipaddress.ip_address(row['host'].split('%', 1)[0])
            if not address.is_global:
                raise ValueError('出站目标解析到非公网地址')
        return rows

    async def close(self):
        await self._resolver.close()


async def request_json_public(method: str, url: str, *, payload=None, headers=None,
                              timeout: float = 15, max_redirects: int = 3):
    """无 Cookie、无环境代理的公网 JSON 请求；每一跳重校验。"""
    current = validate_external_url(url)
    resolver = PublicResolver()
    try:
        connector = aiohttp.TCPConnector(resolver=resolver, limit=4)
        timeout_obj = aiohttp.ClientTimeout(total=max(1, min(float(timeout), 60)))
        async with aiohttp.ClientSession(
            trust_env=False, connector=connector, cookie_jar=aiohttp.DummyCookieJar(),
            timeout=timeout_obj,
        ) as session:
            for _ in range(max_redirects + 1):
                async with session.request(method.upper(), current, json=payload,
                                           headers=headers or {}, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get('Location')
                        if not location:
                            raise ValueError('出站重定向缺少目标')
                        current = validate_redirect(current, location)
                        continue
                    body = await response.text()
                    if len(body) > 2 * 1024 * 1024:
                        raise ValueError('出站响应超过大小限制')
                    try:
                        import json
                        parsed = json.loads(body) if body else None
                    except (TypeError, ValueError):
                        parsed = None
                    return response.status, parsed
            raise ValueError('出站重定向次数超过限制')
    finally:
        await resolver.close()
