#!/usr/bin/env python3
"""Explicit one-time native IM acceptance between two accounts owned by one operator.

No retries of sends. The output directory is a durable stop marker, not a temporary file.
"""
import argparse
import json
import os
from pathlib import Path
import time
import uuid
import urllib.request


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--confirm-owned-accounts',action='store_true',required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--image-only',action='store_true',help='Only one existing image forward between the owned accounts')
    args=p.parse_args()
    args.output.mkdir(mode=0o700,parents=True,exist_ok=True)
    checkpoint=args.output/'real-acceptance.json'
    if checkpoint.exists():p.error('Acceptance checkpoint already exists; refusing additional sends')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('https://ipinfo.io/json',timeout=12) as r:country=json.load(r).get('country')
    if country!='CN':p.error('Mainland direct connection required')
    base='http://127.0.0.1:8080';token=''
    def request(path,body=None):
        data=json.dumps(body).encode() if body is not None else None
        headers={'Content-Type':'application/json'}
        if token:headers['Authorization']='Bearer '+token
        req=urllib.request.Request(base+path,data=data,headers=headers)
        with opener.open(req,timeout=65) as response:return json.load(response)
    login=request('/login',{'username':os.getenv('XIANYU_SMOKE_USERNAME','admin'),'password':os.getenv('XIANYU_SMOKE_PASSWORD','admin123')})
    token=login['token'];result={'country':country,'reply_attempted':False,'forward_attempted':False,'official_client_verified':False}
    def save():checkpoint.write_text(json.dumps(result,ensure_ascii=False,indent=2));checkpoint.chmod(0o600)
    try:
        accounts=request('/chat/accounts')['data']
        if len(accounts)!=2 or any(not account.get('connected') or account.get('authPaused') for account in accounts):
            raise RuntimeError('Exactly two connected owned accounts are required')
        sender=next((a for a in accounts if a.get('remark')=='大号'),accounts[0])
        peer=next(a for a in accounts if a['accountId']!=sender['accountId'])
        a=sender['accountId'];b=peer['accountId']
        conversations=request(f'/chat/conversations/{a}')['data']['conversations']
        conv=next((c for c in conversations if c['otherUserId']==peer['xianyuUserId']),None)
        if not conv:raise RuntimeError('No conversation between the two owned accounts')
        cid=conv['cid']
        models=request(f'/chat/messages/{a}/{cid}')['data']['messages']
        if args.image_only:
            images=[m for m in models if m.get('canForward') and m['type']=='image' and len(m.get('images') or [])==1]
            if not images:raise RuntimeError('No eligible existing owned-account image; no send attempted')
            source=images[-1]
            request(f'/chat/conversations/{b}')
            result['forward_attempted']=True;result['kind']='image';save()
            forwarded=request(f'/chat/forward/{a}',{'source_cid':cid,'message_id':source['messageId'],
                'target_cid':cid,'client_request_id':str(uuid.uuid4())})
            result['forward_status']=forwarded.get('data',{}).get('status');result['forward_id']=forwarded.get('data',{}).get('messageId');save()
            if result['forward_status']=='sent':
                for _ in range(5):
                    time.sleep(3)
                    messages=request(f'/chat/messages/{b}/{cid}')['data']['messages']
                    observed=next((m for m in messages if m['messageId']==result['forward_id']),None)
                    if observed and observed['type']=='image' and observed['images']:
                        result['receiver_image_observed']=True
                        import sqlite3
                        db=sqlite3.connect(Path(__file__).resolve().parents[1]/'data/xianyu_data.db')
                        db.execute("UPDATE chat_native_capabilities SET status='verified',reason='',updated_at=? WHERE kind='forward_image'",(time.time(),));db.commit();db.close();break
            save();print(json.dumps({k:v for k,v in result.items() if not k.endswith('_id')},ensure_ascii=False));return
        eligible=[m for m in models if m.get('canReference') and m['type']=='text' and len(m['text'])<=50
                  and 'http' not in m['text'] and not any(c.isdigit() for c in m['text'])]
        if not eligible:raise RuntimeError('No short, nonsensitive text source available; no send attempted')
        source=eligible[-1]
        # Initialize only the peer-side local snapshot needed for the read-only history check.
        request(f'/chat/conversations/{b}')
        body={'cid':cid,'to_user_id':peer['xianyuUserId'],'text':'【功能验收】原生引用回复（仅大小号测试）',
              'reply_to_message_id':source['messageId'],'client_request_id':str(uuid.uuid4())}
        result['reply_attempted']=True;save()
        reply=request(f'/chat/send/{a}',body)
        result['reply_status']=reply.get('data',{}).get('status');result['reply_id']=reply.get('data',{}).get('messageId');save()
        if result['reply_status']!='sent' or not result['reply_id']:raise RuntimeError('Reply not confirmed; no resend or forward attempted')
        observed=None
        for _ in range(5):
            time.sleep(3)
            messages=request(f'/chat/messages/{b}/{cid}')['data']['messages']
            observed=next((m for m in messages if m['messageId']==result['reply_id']),None)
            if observed:break
        result['receiver_native_reference']=bool(observed and observed.get('reply') and observed['reply']['messageId']==source['messageId'])
        if result['receiver_native_reference']:
            import sqlite3
            db=sqlite3.connect(Path(__file__).resolve().parents[1]/'data/xianyu_data.db')
            db.execute("UPDATE chat_native_capabilities SET status='verified',reason='',updated_at=? WHERE kind='reply_text'",(time.time(),));db.commit();db.close()
        save()
        if not result['receiver_native_reference']:raise RuntimeError('Receiver native quote not observed; no forward attempted')
        result['forward_attempted']=True;save()
        forwarded=request(f'/chat/forward/{a}',{'source_cid':cid,'message_id':result['reply_id'],
             'target_cid':cid,'client_request_id':str(uuid.uuid4())})
        result['forward_status']=forwarded.get('data',{}).get('status');result['forward_id']=forwarded.get('data',{}).get('messageId');save()
        if result['forward_status']=='sent':
            for _ in range(5):
                time.sleep(3)
                messages=request(f'/chat/messages/{b}/{cid}')['data']['messages']
                seen=next((m for m in messages if m['messageId']==result['forward_id']),None)
                if seen:
                    result['receiver_forward_observed']=True;result['forward_kept_reference']=bool(seen.get('reply'));break
        if result.get('receiver_forward_observed'):
            import sqlite3
            db=sqlite3.connect(Path(__file__).resolve().parents[1]/'data/xianyu_data.db')
            db.execute("UPDATE chat_native_capabilities SET status='verified',reason='',updated_at=? WHERE kind='forward_text'",(time.time(),));db.commit();db.close()
        save()
        print(json.dumps({k:v for k,v in result.items() if not k.endswith('_id')},ensure_ascii=False))
    except Exception as error:
        result['error_type']=type(error).__name__;save()
        if result.get('reply_attempted') and result.get('reply_status')!='sent':
            import sqlite3
            db=sqlite3.connect(Path(__file__).resolve().parents[1]/'data/xianyu_data.db')
            db.execute("UPDATE chat_native_capabilities SET status='failed',reason='闲鱼未确认原生引用，暂不可用，请查看处理记录',updated_at=? WHERE kind='reply_text'",(time.time(),));db.commit();db.close()
        print(json.dumps({k:v for k,v in result.items() if not k.endswith('_id')},ensure_ascii=False))
        raise
    finally:
        try:request('/logout',{})
        except Exception:pass


if __name__=='__main__':main()
