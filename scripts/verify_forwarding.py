#!/usr/bin/env python3
"""Explicit controlled forwarding acceptance, confined to the operator's two accounts.

Each wire operation gets a durable checkpoint before sending, never retries, and stops
on unknown outcomes. No customer content is used for test fixtures or printed.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

import httpx
from PIL import Image, ImageDraw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm-owned-accounts', action='store_true', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    checkpoint = args.output / 'acceptance.json'
    if checkpoint.exists():
        parser.error('Checkpoint exists; refusing to repeat any operations')
    result = {'operations': [], 'checks': [], 'official_app_display': 'not_independently_verified'}
    def save():
        checkpoint.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        checkpoint.chmod(0o600)
    with httpx.Client(trust_env=False, timeout=65) as client:
        country = client.get('https://ipinfo.io/json', timeout=12).json().get('country')
        if country != 'CN':
            parser.error('Mainland direct connection required')
        result['country'] = country
        base = 'http://127.0.0.1:8080'
        def api(path, body=None, **kwargs):
            response = client.request('POST' if body is not None or 'files' in kwargs else 'GET',
                                      base+path, json=body, **kwargs)
            response.raise_for_status()
            return response.json()
        login = api('/login', {'username': os.getenv('XIANYU_SMOKE_USERNAME', 'admin'),
                              'password': os.getenv('XIANYU_SMOKE_PASSWORD', 'admin123')})
        client.headers['Authorization'] = 'Bearer '+login['token']
        def send(path, kind, direction, body=None, **kwargs):
            entry = {'kind': kind, 'direction': direction, 'request_id': str(uuid.uuid4()), 'attempted': True}
            result['operations'].append(entry)
            save()  # crash/timeout cannot cause this send to be replayed
            if body is not None:
                body['client_request_id'] = entry['request_id']
            else:
                kwargs['data']['client_request_id'] = entry['request_id']
            response = api(path, body, **kwargs).get('data', {})
            entry.update(status=response.get('status'), message_id=response.get('messageId'))
            save()
            if entry['status'] != 'sent' or not entry['message_id']:
                raise RuntimeError('Send result not confirmed; no resend')
            time.sleep(3)
            return entry['message_id']
        def observed(account, cid, mid):
            for attempt in range(5):
                messages = api(f'/chat/messages/{account}/{cid}')['data']['messages']
                message = next((m for m in messages if m['messageId'] == mid), None)
                if message:
                    return message
                if attempt < 4:
                    time.sleep(3)
            raise RuntimeError('Receiver history has not confirmed the exact message id')
        try:
            accounts = api('/chat/accounts')['data']
            if len(accounts) != 2 or any(not a.get('connected') or a.get('authPaused') for a in accounts):
                raise RuntimeError('Exactly two connected owned accounts are required')
            pairs = []
            for sender in accounts:
                peer = next(a for a in accounts if a['accountId'] != sender['accountId'])
                threads = api('/chat/conversations/'+sender['accountId'])['data']['conversations']
                conv = next((c for c in threads if c['otherUserId'] == peer['xianyuUserId']), None)
                if not conv:
                    raise RuntimeError('No owned-account conversation; refusing to use another contact')
                pairs.append((sender, peer, conv['cid']))
            for index, (sender, peer, cid) in enumerate(pairs):
                a, b = sender['accountId'], peer['accountId']
                direction = f'owned-direction-{index+1}'
                text = f'【功能验收】转发文字与表情 第{index+1}组\n原样保留[微笑][握手]'
                seed = send(f'/chat/send/{a}', 'seed_text', direction,
                            {'cid':cid, 'to_user_id':peer['xianyuUserId'], 'text':text})
                assert observed(b,cid,seed)['text'] == text
                image = Image.new('RGB', (480,240), '#fff3b0')
                ImageDraw.Draw(image).text((35,90), f'ByGoofish Forward QA {index+1}', fill='#302b12')
                buf = io.BytesIO();image.save(buf, format='PNG')
                image_id = send(f'/chat/send-image/{a}', 'seed_image', direction,
                                data={'cid':cid, 'to_user_id':peer['xianyuUserId']},
                                files={'image':('forward-qa.png', buf.getvalue(), 'image/png')})
                original_image = observed(b,cid,image_id)
                assert original_image['type'] == 'image' and len(original_image['images']) == 1
                quote = send(f'/chat/send/{a}', 'seed_quote', direction,
                             {'cid':cid,'to_user_id':peer['xianyuUserId'],
                              'text':'【功能验收】转发引用消息','reply_to_message_id':seed})
                for kind, source_id in [('text',seed),('image',image_id),('quote',quote)]:
                    mid = send(f'/chat/forward/{a}', 'forward_'+kind, direction,
                               {'source_cid':cid, 'message_id':source_id, 'target_cid':cid})
                    message = observed(b,cid,mid)
                    if kind == 'text':
                        assert message['text'] == text
                    elif kind == 'quote':
                        assert message['text'] == '【功能验收】转发引用消息'
                        assert message.get('reply',{}).get('messageId') == seed
                    else:
                        assert message['type'] == 'image' and len(message['images']) == 1
                        # Same platform image URL proves forwarding used the exact source image,
                        # rather than a placeholder or the conversation's product thumbnail.
                        assert message['images'] == original_image['images']
                        result['checks'].append({'direction':direction,'image_url_hash':hashlib.sha256(message['images'][0].encode()).hexdigest()})
                    result['checks'].append({'direction':direction,'kind':kind,'receipt_and_receiver_content':True})
                    save()
            # Capability is enabled only after both directions have exact receipts and images.
            db = sqlite3.connect(Path(__file__).resolve().parents[1]/'data/xianyu_data.db')
            db.execute("UPDATE chat_native_capabilities SET status='verified',reason='',updated_at=? WHERE kind='forward_image'", (time.time(),))
            db.commit();db.close()
            result['passed'] = True;save()
        except Exception as error:
            result['error_type'] = type(error).__name__;result['passed'] = False;save()
            raise
        finally:
            try:
                api('/logout', {})
            except Exception:
                pass
    print(json.dumps({'passed': result['passed'], 'attempts': len(result['operations']),
                      'confirmed': sum(x.get('status')=='sent' for x in result['operations']),
                      'content_checks': len(result['checks'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
