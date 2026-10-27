"""用户级通知模板；邮件支持受限 HTML，其他渠道使用纯文本正文。"""

from __future__ import annotations

import re
from datetime import datetime
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit

from app.notification_events import EVENT_DEFINITIONS, EVENT_IDS

DEFAULT_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body { margin: 0; padding: 24px; background: #f4f4f5; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #18181b; }
    .container { max-width: 640px; margin: 0 auto; background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 8px 30px rgba(15, 23, 42, 0.10); }
    .header { background: #4f46e5; color: #ffffff; padding: 28px 32px; }
    .header h1 { margin: 0; font-size: 24px; line-height: 1.25; }
    .content { padding: 32px; font-size: 15px; line-height: 1.7; }
    .message { white-space: pre-wrap; overflow-wrap: anywhere; }
    .action { margin-top: 20px; padding: 16px; border-radius: 8px; background: #eef2ff; }
    .button { display: inline-block; margin-top: 12px; padding: 11px 18px; border-radius: 8px; background: #4f46e5; color: #ffffff; text-decoration: none; font-weight: 600; }
    .muted { color: #71717a; font-size: 13px; }
    .footer { padding: 18px 32px; background: #fafafa; color: #71717a; font-size: 12px; }
    @media (max-width: 640px) { body { padding: 12px; } .header { padding: 22px; } .content { padding: 22px; } .footer { padding: 16px 22px; } }
  </style>
</head>
<body>
  <div class="container">
    <div class="header"><h1>{{event}}</h1></div>
    <div class="content">
      <p><strong>{{account}}</strong> 的通知</p>
      <p><strong>影响</strong><br>{{impact}}</p>
      <div class="action"><strong>建议操作</strong><br>{{recommended_action}}</div>
      <div class="message" style="margin-top:20px">{{message}}</div>
      <p class="muted">发生时间：{{time}}</p>
    </div>
    <div class="footer">此邮件由闲鱼超级管家自动发送，请勿直接回复。</div>
  </div>
</body>
</html>'''

HTML_EMAIL_MARKER = '<!--BYGOOFISH-EMAIL-HTML-->'
VARIABLES = {
    'message': '原始通知全文', 'account': '账号', 'event': '事件名称',
    'time': '通知时间', 'buyer': '买家昵称', 'buyer_id': '买家ID',
    'item_id': '商品ID', 'chat_id': '会话ID', 'verification_url': '验证链接',
    'impact': '事件影响', 'recommended_action': '建议操作', 'severity': '事件级别',
}
PLACEHOLDER = re.compile(r'\{\{\s*([a-z_]+)\s*\}\}')
PREFIX = 'notification_template_'
_HTML_OPEN = re.compile(r'<\s*(?:!doctype\s+html|html|head|body|div|p|table|section|h[1-6]|a|span|style|ul|ol|li|strong|br)\b', re.I)
_DANGEROUS_TAGS = {'script', 'iframe', 'object', 'embed', 'svg', 'math', 'form', 'video', 'audio', 'canvas'}
_ALLOWED_TAGS = {
    'html', 'head', 'meta', 'title', 'style', 'body', 'div', 'section', 'p',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'b', 'em', 'i', 'a',
    'span', 'br', 'hr', 'table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th',
    'ul', 'ol', 'li', 'small', 'code', 'pre',
}
_VOID_TAGS = {'meta', 'br', 'hr'}
_GLOBAL_ATTRS = {'class', 'style', 'title', 'role', 'align', 'width', 'height'}
_ATTRS_BY_TAG = {
    'html': {'lang'},
    'meta': {'charset', 'name', 'content', 'http-equiv'},
    'a': {'href'},
    'td': {'colspan', 'rowspan'},
    'th': {'colspan', 'rowspan'},
}
_UNSAFE_CSS = re.compile(r'(?:url\s*\(|expression\s*\(|@import|javascript\s*:|vbscript\s*:|data\s*:|-moz-binding|behavior\s*:)', re.I)


def is_html_template(content: str) -> bool:
    return bool(_HTML_OPEN.search(content or ''))


def _safe_css(value: str) -> str:
    if len(value) > 12_000 or _UNSAFE_CSS.search(value) or '\\' in value:
        return ''
    return value


def _safe_href(value: str) -> str:
    if re.fullmatch(r'\{\{\s*verification_url\s*\}\}', value.strip()):
        return value.strip()
    parsed = urlsplit(value.strip())
    if parsed.scheme in {'https', 'http'} and parsed.netloc:
        return value.strip()
    if parsed.scheme == 'mailto' and parsed.path:
        return value.strip()
    return ''


class _EmailTemplateSanitizer(HTMLParser):
    """Keep basic email markup while dropping executable or externally loaded content."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._drop_depth = 0
        self._style_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._drop_depth:
            if tag in _DANGEROUS_TAGS:
                self._drop_depth += 1
            return
        if tag in _DANGEROUS_TAGS:
            self._drop_depth = 1
            return
        if tag not in _ALLOWED_TAGS:
            return
        allowed = _GLOBAL_ATTRS | _ATTRS_BY_TAG.get(tag, set())
        safe_attrs = []
        for key, value in attrs:
            key = (key or '').lower()
            value = value or ''
            if key not in allowed:
                continue
            if key == 'style':
                value = _safe_css(value)
                if not value:
                    continue
            elif key == 'href':
                value = _safe_href(value)
                if not value:
                    continue
            elif key == 'http-equiv' and value.lower() not in {'content-type', 'x-ua-compatible'}:
                continue
            safe_attrs.append(f' {key}="{escape(value, quote=True)}"')
        self.parts.append(f'<{tag}{"".join(safe_attrs)}>')
        if tag == 'style':
            self._style_depth += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._drop_depth:
            if tag in _DANGEROUS_TAGS:
                self._drop_depth = max(0, self._drop_depth - 1)
            return
        if tag not in _ALLOWED_TAGS or tag in _VOID_TAGS:
            return
        if tag == 'style' and self._style_depth:
            self._style_depth -= 1
        self.parts.append(f'</{tag}>')

    def handle_data(self, data):
        if self._drop_depth:
            return
        text = _safe_css(data) if self._style_depth else data
        if text:
            self.parts.append(escape(text, quote=False))

    def handle_entityref(self, name):
        if not self._drop_depth:
            self.parts.append(f'&amp;{escape(name)};')

    def handle_charref(self, name):
        if not self._drop_depth:
            self.parts.append(f'&amp;#{escape(name)};')

    def handle_comment(self, data):
        # Remove user comments so the internal email marker cannot be spoofed.
        return

    def handle_decl(self, decl):
        if decl.strip().lower() == 'doctype html':
            self.parts.append('<!DOCTYPE html>')


class _PlainTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._links: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._skip:
            if tag in {'head', 'style', 'script'}:
                self._skip += 1
            return
        if tag in {'head', 'style', 'script'}:
            self._skip = 1
        elif tag in {'br', 'hr', 'p', 'div', 'section', 'tr', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            self.parts.append('\n')
        elif tag in {'td', 'th'}:
            self.parts.append('\t')
        elif tag == 'a':
            href = next((value for key, value in attrs if key.lower() == 'href' and value), '')
            self._links.append(href)
            if href:
                self.parts.append(' (')

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip:
            if tag in {'head', 'style', 'script'}:
                self._skip = max(0, self._skip - 1)
            return
        if tag in {'p', 'div', 'section', 'tr', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            self.parts.append('\n')
        elif tag == 'a' and self._links:
            href = self._links.pop()
            if href:
                self.parts.append(f'{href})')

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def validate_template(event_type: str, content: str) -> str:
    if event_type not in EVENT_IDS:
        raise ValueError('不支持的通知事件')
    if not isinstance(content, str) or len(content) > 8000:
        raise ValueError('通知模板不能超过8000字')
    if not content.strip():
        return DEFAULT_TEMPLATE
    for variable in PLACEHOLDER.findall(content):
        if variable not in VARIABLES:
            raise ValueError(f'不支持的模板变量：{variable}')
    remaining = PLACEHOLDER.sub('', content)
    if '{{' in remaining or '}}' in remaining:
        raise ValueError('模板变量格式应为 {{message}}')
    if not is_html_template(content):
        return content
    sanitizer = _EmailTemplateSanitizer()
    sanitizer.feed(content)
    sanitizer.close()
    safe = ''.join(sanitizer.parts).strip()
    if not safe:
        raise ValueError('HTML 模板没有可用内容')
    return safe


def _is_safe_verification_url(value: str) -> bool:
    parsed = urlsplit(value.strip())
    return parsed.scheme in {'http', 'https'} and bool(parsed.netloc)


def _append_verification_link(content: str, verification_url: str) -> str:
    safe_url = escape(verification_url, quote=True)
    link = f'<p><a class="button" href="{safe_url}">打开验证页面</a></p>'
    closing_body = re.search(r'</body\s*>', content, re.I)
    if closing_body:
        return content[:closing_body.start()] + link + content[closing_body.start():]
    return content + link


def render_template(content: str, context: dict, *, mark_email_html: bool = True) -> str:
    # Substitute once and escape values so buyer messages and provider errors remain inert.
    is_html = is_html_template(content)
    verification_url = str(context.get('verification_url') or '')
    has_verification_placeholder = bool(re.search(r'\{\{\s*verification_url\s*\}\}', content))

    if is_html and has_verification_placeholder:
        def render_verification_anchor(match):
            if not _is_safe_verification_url(verification_url):
                return ''
            opening = match.group('opening')
            safe_url = escape(verification_url, quote=True)
            opening = re.sub(
                r'href="\{\{\s*verification_url\s*\}\}"',
                f'href="{safe_url}"', opening, flags=re.I,
            )
            return f'<a{opening}>{match.group("body")}</a>'

        content = re.sub(
            r'<a\b(?P<opening>[^>]*\bhref="\{\{\s*verification_url\s*\}\}"[^>]*)>(?P<body>.*?)</a\s*>',
            render_verification_anchor, content, flags=re.I | re.S,
        )

    def substitute(match):
        value = str(context.get(match[1], '') or '')
        if is_html and match[1] == 'verification_url' and not _is_safe_verification_url(value):
            return ''
        return escape(value, quote=True) if is_html else value

    rendered = PLACEHOLDER.sub(substitute, content)
    if is_html:
        if verification_url and _is_safe_verification_url(verification_url) and not has_verification_placeholder:
            rendered = _append_verification_link(rendered, verification_url)
        return f'{HTML_EMAIL_MARKER}{rendered}' if mark_email_html else rendered
    if verification_url and verification_url not in rendered:
        rendered += f'\n验证链接: {verification_url}'
    return rendered if rendered.strip() else str(context.get('message', ''))


def notification_plain_text(message: str) -> str:
    """Return the text alternative for any non-email notification channel."""
    if not isinstance(message, str) or not message.startswith(HTML_EMAIL_MARKER):
        return message
    parser = _PlainTextExtractor()
    parser.feed(message[len(HTML_EMAIL_MARKER):])
    parser.close()
    text = ''.join(parser.parts)
    lines = [line.strip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def split_email_message(message: str) -> tuple[str, str | None]:
    if isinstance(message, str) and message.startswith(HTML_EMAIL_MARKER):
        html_body = message[len(HTML_EMAIL_MARKER):]
        return notification_plain_text(message), html_body
    return message, None


def template_context(event_type: str, message: str, account: str, **values) -> dict:
    definition = next((item for item in EVENT_DEFINITIONS if item['id'] == event_type), {})
    label = definition.get('label', event_type)
    return {**dict.fromkeys(VARIABLES, ''), 'message': message, 'account': account,
            'event': label, 'impact': definition.get('impact', ''),
            'recommended_action': definition.get('action', ''),
            'severity': definition.get('severity', 'info'),
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), **values}


def preview_template(event_type: str, content: str) -> str:
    content = validate_template(event_type, content)
    context = template_context(
        event_type, '', '示例账号', buyer='示例买家', buyer_id='示例买家ID',
        item_id='示例商品ID', chat_id='示例会话ID',
        verification_url='https://example.test/verify' if event_type in ('login_verification_required','message_verification_required') else '',
    )
    context['message'] = (f"{context['event']}\n账号: {context['account']}\n"
                          f"时间: {context['time']}\n内容: 这是用于预览的示例通知，实际发送时会替换为真实事件内容。")
    return render_template(content, context, mark_email_html=False)


def render_account_notification(db, cookie_id: str, event_type: str, message: str, **values) -> str:
    """只读取账号所属用户的模板；缺省或无效配置使用目录默认通知模板。"""
    owner = db.get_cookie_owner_user(cookie_id)
    if owner is None:
        return message
    setting = db.get_user_setting(owner, PREFIX + event_type)
    try:
        content = validate_template(event_type, setting['value'] if setting else DEFAULT_TEMPLATE)
    except (ValueError, KeyError, TypeError):
        content = validate_template(event_type, DEFAULT_TEMPLATE)
    details = db.get_cookie_details(cookie_id) or {}
    nickname = str(details.get('nickname') or '').strip()
    remark = str(details.get('remark') or '').strip()
    account = f'{nickname}（{remark}）' if nickname and remark else (nickname or remark or cookie_id)
    return render_template(content, template_context(event_type, message, account, **values))
