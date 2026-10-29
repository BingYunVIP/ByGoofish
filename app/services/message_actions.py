"""Native IM reply/forward operations. References are resolved on the account loop."""
import base64
import json
from utils.xianyu_utils import generate_uuid
from fastapi import HTTPException
from app.xianyu_im import parse_message, _as_dict, _load_content


def full(value):
    return str(value).split('@')[0] + '@goofish'


def custom_content(text=None, image=None):
    decoded = {'contentType':1,'text':{'text':text}} if text is not None else {'contentType':2,'image':{'pics':[image]}}
    return {'contentType':101,'custom':{'type':1,'data':base64.b64encode(json.dumps(decoded,ensure_ascii=False).encode()).decode()}}


def native_display(content):
    """Decode the reply's SimpleContent wrapper without retaining arbitrary extension data."""
    content = _as_dict(content)
    simple = _as_dict(content.get('simpleContent')) or content
    custom = _as_dict(simple.get('custom'))
    if custom:
        return parse_message({'message':{'content':{'custom':custom},'extension':{'senderUserId':'display'}}},'')
    text = _as_dict(simple.get('text'))
    if text:
        return {'text':str(text.get('content') or text.get('text') or ''),'images':[],'type':'text'}
    photo = _as_dict(simple.get('photo'))
    if photo:
        url = photo.get('url')
        return {'text':'','images':[str(url)] if url else [],'type':'image'}
    return None


def reference_view(content):
    outer = _as_dict(content)
    if outer.get('contentType') != 10:
        return None
    reply = _as_dict(outer.get('replyContent'))
    reference = _as_dict(reply.get('referenceMessage'))
    view = native_display(reference.get('referenceContent')) or {'text':'[不支持的引用内容]','images':[],'type':'system'}
    return {'messageId':str(reference.get('messageId') or ''),'senderId':str(reference.get('sender') or '').split('@')[0],
            'senderName':str(_as_dict(reference.get('extension')).get('senderNick') or ''),
            'time':int(reference.get('createAt') or 0),**{k:view[k] for k in ('text','images','type')}}


async def resolve_source(instance, cid, message_id, recipient, *, allow_quote=False):
    """A scoped history query is the proof of membership; no client content is accepted."""
    from app.services.auth_recovery import auth_paused
    from utils.risk_control import registry
    if auth_paused(instance.cookie_id) or registry.get(instance.cookie_id).is_blocked:
        raise HTTPException(409,'账号认证暂停，请先处理账号状态',headers={'X-Chat-Send-Status':'not_sent'})
    cursor = None
    for _ in range(5):
        raw = await instance.get_im_messages(cid, cursor, 50)
        payload = raw.get('data') if isinstance(raw.get('data'),dict) else raw
        if not isinstance(payload,dict) or payload.get('reason') or payload.get('code'):
            raise HTTPException(409,'闲鱼消息读取受限，请稍后重试',headers={'X-Chat-Send-Status':'not_sent'})
        rows = payload.get('userMessageModels') or []
        for row in rows:
            message = _as_dict(row.get('message')) if isinstance(row,dict) else {}
            if str(message.get('messageId') or '') != message_id:
                continue
            parsed = parse_message(row, instance.myid)
            raw_cid = str(message.get('cid') or '').split('@')[0]
            if raw_cid and raw_cid != str(cid).split('@')[0]:
                raise HTTPException(403,'原消息不属于此会话')
            if not parsed or parsed['senderId'] not in (str(instance.myid),str(recipient)):
                raise HTTPException(403,'原消息参与者不匹配')
            if parsed['type'] not in ('text','image') or parsed.get('event_type') or (parsed.get('reply') and not allow_quote):
                raise HTTPException(400,'此消息类型暂不支持引用或转发')
            # Reconstruct only supported content; do not forward cards, private extension or actions.
            content = _as_dict(message.get('content'))
            if content.get('contentType') not in ((1,2,101,10) if allow_quote else (1,2,101)):
                raise HTTPException(400,'尚未验证此消息格式的原生操作')
            if len(parsed.get('images') or []) > 1:
                raise HTTPException(400,'暂不支持多图消息操作')
            return message, parsed
        next_cursor = payload.get('nextCursor')
        if not payload.get('hasMore') or next_cursor is None or next_cursor == cursor:
            break
        cursor = next_cursor
    raise HTTPException(404,'未找到原消息；请加载该消息后重试',headers={'X-Chat-Send-Status':'not_sent'})


def source_content(message, parsed):
    content = _as_dict(message.get('content'))
    if content.get('contentType') == 10:
        reference = parsed.get('reply') or {}
        original = custom_content(text=reference.get('text') or '[图片]')
        if reference.get('images'):
            original = custom_content(image={'url':reference['images'][0],'type':0,'width':800,'height':600})
        ref_cid = _as_dict(_as_dict(content.get('replyContent')).get('referenceMessage')).get('cid') or message.get('cid')
        pointer = {'cid':full(ref_cid)} if ref_cid else {}
        return {'contentType':10,'replyContent':{'referenceMessage':{**pointer,'sender':full(reference.get('senderId') or ''),
            'messageId':reference.get('messageId') or '', 'createAt':reference.get('time') or 0,
            'referenceContent':{'contentType':101,'simpleContent':original}, 'extension':{}},
            'replyContent':{'contentType':1,'simpleContent':{'text':{'content':parsed['text']}}}}}
    if content.get('contentType') == 101:
        # Use normalized content, not arbitrary card structures from a custom payload.
        if parsed['type']=='text':return custom_content(text=parsed['text'])
        decoded=_load_content(_as_dict(content.get('custom')).get('data')) or {}
        pics=_as_dict(decoded.get('image')).get('pics') or [{}]
        original=pics[0] if isinstance(pics[0],dict) else {}
        return custom_content(image={'url':parsed['images'][0],'type':0,'width':int(original.get('width') or 800),'height':int(original.get('height') or 600)})
    if parsed['type']=='text':
        return {'contentType':1,'text':{'content':parsed['text']}}
    return content


async def send_reply(instance, cid, recipient, source, text=None, image=None, *, db=None, request_id=""):
    message, parsed = source
    # Verified official App sample: normal custom text + extension.replyMessageId.
    # The generic SDK contentType=10 was not accepted by the Goofish service.
    if image is not None:
        raise HTTPException(400,'尚未验证原生图片回复；请取消引用后发送图片',headers={'X-Chat-Send-Status':'not_sent'})
    content = custom_content(text=text)
    decoded={'atUsers':[],'contentType':1,'text':{'text':str(text or '')}}
    content['custom']['data']=base64.b64encode(json.dumps(decoded,ensure_ascii=False).encode()).decode()
    reply_id=str(message['messageId'])
    extension={'replyMessageId':reply_id,'extJson':json.dumps({'replyMessageId':reply_id},separators=(',',':'))}
    response = await instance._send_im_request('/r/MessageSend/sendByReceiverScope',[
        {'uuid':generate_uuid(),'cid':full(cid),'conversationType':1,'content':content,
         'redPointPolicy':0,'extension':extension,'ctx':{'appVersion':'1.0','platform':'web'},'mtags':{},'msgReadStatusSetting':1},
        {'actualReceivers':[full(recipient),full(instance.myid)]}])
    from app.services.native_capabilities import record
    record(db,instance.cookie_id,request_id,response)
    return response


async def forward(instance, target_cid, recipient, source, *, db=None, client_request_id=""):
    message, parsed = source
    request_id = generate_uuid()
    result = await instance._send_im_request('/r/MessageForward/batchForward',[
        {'toCid':full(target_cid),'conversationType':1,'forwardMessages':[
            {'uuid':request_id,'messageId':str(message['messageId']),'content':source_content(message,parsed),'extension':({'replyMessageId':parsed['reply']['messageId'],'extJson':json.dumps({'replyMessageId':parsed['reply']['messageId']})} if parsed.get('reply') else {})}],
         'redPointPolicy':0,'msgReadStatusSetting':1,'ctx':{'appVersion':'1.0','platform':'web'}},
        {'actualReceivers':[full(recipient),full(instance.myid)]}])
    from app.services.native_capabilities import record
    record(db,instance.cookie_id,client_request_id,result)
    body = result.get('body') or {}
    receipt = (body.get('sendResults') or {}).get(request_id)
    if str(result.get('code')) != '200' or not isinstance(receipt,dict) or not receipt.get('messageId'):
        raise TimeoutError('未取得转发消息的成功回执，请核查历史，勿重复发送')
    return {'code':200,'body':receipt}
