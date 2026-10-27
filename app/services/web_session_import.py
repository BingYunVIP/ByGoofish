"""Extract a verified official web request as data; never execute pasted commands."""
import json
import re
import shlex
from urllib.parse import parse_qs, urlsplit


class SessionImportError(ValueError):
    pass


def parse_request(text, expected_user):
    if not isinstance(text,str) or not 1 <= len(text) <= 60000:
        raise SessionImportError('请粘贴官网消息令牌请求，内容不得超过60KB')
    try:
        parts=shlex.split(text.replace('\\\n',' '))
    except ValueError:
        raise SessionImportError('请求格式不完整，请选择“复制为cURL（bash）”后重新粘贴') from None
    if not parts or parts[0] != 'curl':
        raise SessionImportError('请使用官网网络面板的“复制为cURL（bash）”，不要粘贴响应正文')
    url='';cookie='';body='';i=1
    while i<len(parts):
        arg=parts[i]
        if arg in ('-H','--header','-b','--cookie','--data','--data-raw','--data-binary','--data-urlencode','--url'):
            i+=1
            if i>=len(parts):raise SessionImportError('请求格式不完整')
            value=parts[i]
            if arg in ('-H','--header') and value.lower().startswith('cookie:'):
                cookie=value.split(':',1)[1].strip()
            elif arg in ('-b','--cookie'):cookie=value
            elif arg=='--url':url=value
            elif arg.startswith('--data'):body=value
        elif arg.startswith('https://'):url=arg
        i+=1
    try:u=urlsplit(url)
    except ValueError:raise SessionImportError('请求地址格式不正确') from None
    if (u.scheme!='https' or u.hostname!='h5api.m.goofish.com' or u.username or u.password
            or not re.fullmatch(r'/h5/[a-zA-Z0-9_.]+/[0-9.]+/?',u.path)):
        raise SessionImportError('请选择闲鱼官网h5api.m.goofish.com接口的请求')
    if any(ord(char)<32 for char in cookie):
        raise SessionImportError('Cookie格式不正确，请重新复制完整请求')
    fields={}
    for part in cookie.split(';'):
        key,sep,value=part.strip().partition('=')
        if sep and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',key):fields[key]=value
    if not all(fields.get(k) for k in ('unb','cookie2','_m_h5_tk','_m_h5_tk_enc')):
        raise SessionImportError('请求缺少完整网页登录凭证，请复制完成消息验证后成功的请求')
    if fields['unb']!=str(expected_user):
        raise SessionImportError('请求中的闲鱼账号与当前账号不一致，未保存')
    form=parse_qs(body) if body else parse_qs(u.query)
    raw=(form.get('data') or [''])[0]
    try:data=json.loads(raw)
    except (ValueError,TypeError):data={}
    device=str(data.get('deviceId') or '') if isinstance(data,dict) else ''
    uuid=r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}'
    if device and (data.get('appKey')!='444e9908a51d1cb236a27862abc769c9'
            or not re.fullmatch(uuid+'-'+re.escape(str(expected_user)),device)):
        raise SessionImportError('消息请求的设备信息与当前账号不匹配，未保存')
    if 'idlemessage.pc.login.token' in u.path and not device:
        raise SessionImportError('消息令牌请求缺少设备信息，请复制完整请求')
    return '; '.join(f'{k}={v}' for k,v in fields.items()),device,len(fields)
