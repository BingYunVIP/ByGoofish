"""Official emoji directory with local raster assets; no third-party substitutions."""
import asyncio
import hashlib
import io
import json
import time
from pathlib import Path
from PIL import Image
from app.services.reply_images import download, is_platform_image_url


def initialize(db):
    with db.lock:
        db.conn.execute('''CREATE TABLE IF NOT EXISTS chat_emoji_cache (
            cookie_id TEXT PRIMARY KEY REFERENCES cookies(id) ON DELETE CASCADE,
            items TEXT NOT NULL DEFAULT '[]', updated_at REAL NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0)''')
        db.conn.commit()


def cached(db, account):
    with db.lock:
        row = db.conn.execute('SELECT items,updated_at,retry_at FROM chat_emoji_cache WHERE cookie_id=?',(account,)).fetchone()
    return (json.loads(row[0]),row[1],row[2]) if row else ([],0,0)


async def refresh(instance, db):
    from app.services.auto_reply import available
    items,updated,retry = cached(db,instance.cookie_id)
    now=time.time()
    if now < max(updated+86400,retry) or not available(instance,db):
        return {'items':items,'stale':now>=updated+86400}
    lock=getattr(instance,'_emoji_refresh_lock',None)
    if lock is None:lock=instance._emoji_refresh_lock=asyncio.Lock()
    async with lock:
        items,updated,retry=cached(db,instance.cookie_id)
        if time.time()<max(updated+86400,retry):return {'items':items,'stale':time.time()>=updated+86400}
        try:
            from utils.xianyu_seller_api import XianyuSellerAPI
            original=instance.cookies_str
            async with XianyuSellerAPI(instance.cookie_id,original) as api:
                api.ORIGIN='https://www.goofish.com'
                data=await api._call('mtop.taobao.idlemessage.face.emoji.load',{})
                if instance.cookies_str==original and api.cookies_str!=original:
                    from utils.xianyu_utils import trans_cookies
                    instance.cookies_str=api.cookies_str
                    instance.cookies=trans_cookies(api.cookies_str)
            records=(data.get('data') or {}).get('items')
            if not isinstance(records,list) or not records:raise ValueError('official emoji directory missing')
            semaphore=asyncio.Semaphore(4)
            root=Path('static/chat-emojis');root.mkdir(parents=True,exist_ok=True)
            async def asset(record):
                if not isinstance(record,dict):return None
                alias=str(record.get('iconAlias') or '')
                url=str(record.get('iconUrl') or '')
                if url.startswith('//'):url='https:'+url
                if not (alias.startswith('[') and alias.endswith(']') and 2<len(alias)<40 and is_platform_image_url(url)):return None
                name=str(record.get('iconName') or alias[1:-1])[:50]
                key=hashlib.sha256(url.encode()).hexdigest()
                path=root/(key+'.png')
                async with semaphore:
                    if not path.is_file():
                        try:
                            raw=await download(url)
                            if len(raw)>1024*1024:return {'alias':alias,'name':name,'url':''}
                            def convert():
                                with Image.open(io.BytesIO(raw)) as image:
                                    if image.width*image.height>1024*1024:raise ValueError('emoji too large')
                                    image.convert('RGBA').save(path,'PNG')
                            await asyncio.to_thread(convert)
                        except Exception:return {'alias':alias,'name':name,'url':''}
                return {'alias':alias,'name':name,'url':'/static/chat-emojis/'+path.name}
            tasks=[asyncio.create_task(asset(r)) for r in records[:300]]
            done,pending=await asyncio.wait(tasks,timeout=35)
            for task in pending:task.cancel()
            await asyncio.gather(*pending,return_exceptions=True)
            results=[task.result() for task in done if not task.cancelled() and task.exception() is None]
            unique={r['alias']:r for r in results if r}
            # Timed-out assets keep their official name rather than a substituted emoji.
            for record in records[:300]:
                alias=str(record.get('iconAlias') or '') if isinstance(record,dict) else ''
                if alias.startswith('[') and alias.endswith(']') and len(alias)<40 and alias not in unique:
                    unique[alias]={'alias':alias,'name':str(record.get('iconName') or alias[1:-1])[:50],'url':''}
            if not unique:raise ValueError('no valid official emojis')
            items=list(unique.values())
            with db.lock:
                db.conn.execute('INSERT OR REPLACE INTO chat_emoji_cache VALUES(?,?,?,0)',(instance.cookie_id,json.dumps(items,ensure_ascii=False),now));db.conn.commit()
            return {'items':items,'stale':False}
        except Exception:
            with db.lock:
                db.conn.execute('INSERT OR REPLACE INTO chat_emoji_cache VALUES(?,?,?,?)',(instance.cookie_id,json.dumps(items,ensure_ascii=False),updated,now+21600));db.conn.commit()
            return {'items':items,'stale':True}
