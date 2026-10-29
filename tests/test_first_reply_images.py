"""External image upload, URL boundaries, cache, five-second first reply and once semantics."""
import asyncio
import io
import json
from pathlib import Path
import socket
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from aiohttp import web
from PIL import Image
from app.db_manager import DBManager
from app.services import reply_images as images, auto_reply as replies
import tests.test_auto_reply_flow as flow_fixtures


def picture(format='WEBP',size=(803,1920),mode='RGB'):
    output=io.BytesIO();Image.new(mode,size).save(output,format=format);return output.getvalue()


class ReplyImageFormatTests(unittest.TestCase):
    def test_https_image_extension_is_not_platform_cdn(self):
        for url in ('https://image.example.test/a.webp','https://alicdn.com.evil.test/a.jpg',
                    'https://evil-alicdn.com/a.png','https://img.alicdn.com@evil.test/a.jpg','https://goofish.com/a.jpg'):
            self.assertFalse(images.is_platform_image_url(url))
        self.assertTrue(images.is_platform_image_url('https://img.alicdn.com/a.png'))

    def test_private_literal_credentials_and_metadata_urls_rejected(self):
        for url in ('http://127.0.0.1/a','http://[::1]/a','http://169.254.169.254/a',
                    'https://localhost/a','file:///tmp/a','https://user:secret@image.example/a'):
            with self.assertRaises(images.ReplyImageError):images.validate_public_url(url)

    def test_webp_and_transparency_become_bounded_jpeg(self):
        for fmt,mode in (('WEBP','RGB'),('PNG','RGBA'),('GIF','P')):
            raw,width,height=images.jpeg_image(picture(fmt,(2000,3000),mode))
            with Image.open(io.BytesIO(raw)) as image:
                self.assertEqual(image.format,'JPEG');self.assertEqual(image.mode,'RGB')
                self.assertEqual(image.size,(width,height));self.assertLessEqual(max(image.size),1920)

    def test_html_corrupt_and_oversize_data_are_not_images(self):
        for raw in (b'<html>login required</html>',b'not an image',b'x'*(images.MAX_BYTES+1)):
            with self.assertRaises(images.ReplyImageError):images.jpeg_image(raw)


class ReplyImageTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=DBManager(str(Path(self.tmp.name)/'image.db'))
        uid=self.db.get_user_by_username('admin')['id']
        for account in ('image-fixture-a','image-fixture-b'):self.db.save_cookie(account,'unb=fixture',uid)
        self.live=SimpleNamespace(cookie_id='image-fixture-a',cookies_str='unb=fixture')
        self.cleanups=[]

    async def asyncTearDown(self):
        for cleanup in self.cleanups:await cleanup()
        self.db.close();self.tmp.cleanup()

    async def test_external_webp_uploaded_as_jpeg_once_and_account_cache_isolated(self):
        paths=[]
        async def upload(path):
            paths.append(path)
            self.assertTrue(Path(path).is_file())
            with Image.open(path) as image:self.assertEqual(image.format,'JPEG')
            return 'https://img.alicdn.com/image-fixture.jpg'
        with patch.object(images,'download',AsyncMock(return_value=picture())) as download,\
                patch('utils.image_uploader.ImageUploader.upload_image',AsyncMock(side_effect=upload)) as uploader:
            a=await images.prepare(self.live,'https://image.example.test/source.webp',self.db)
            b=await images.prepare(self.live,'https://image.example.test/source.webp',self.db)
            self.assertEqual(a,b);self.assertEqual(a[1:],(803,1920));uploader.assert_awaited_once();download.assert_awaited_once()
            self.live.cookie_id='image-fixture-b'
            await images.prepare(self.live,'https://image.example.test/source.webp',self.db)
            self.assertEqual(uploader.await_count,2)
        self.assertTrue(all(not Path(path).exists() for path in paths))

    async def test_expired_cache_reuploads_and_source_url_is_never_overwritten(self):
        self.db.save_default_reply(self.live.cookie_id,True,'fixture',True,'https://image.example.test/original.webp')
        with patch.object(images,'download',AsyncMock(return_value=picture())) as download,\
                patch('utils.image_uploader.ImageUploader.upload_image',AsyncMock(return_value='https://img.alicdn.com/cached.jpg')):
            await images.prepare(self.live,'https://image.example.test/original.webp',self.db)
            self.db.conn.execute('UPDATE auto_reply_image_cache SET updated_at=0');self.db.conn.commit()
            await images.prepare(self.live,'https://image.example.test/original.webp',self.db)
            self.assertEqual(download.await_count,2)
        self.assertEqual(self.db.get_default_reply(self.live.cookie_id)['reply_image_url'],'https://image.example.test/original.webp')

    async def test_failed_upload_is_not_cached_and_temporary_file_is_removed(self):
        paths=[]
        async def failed(path):paths.append(path);return None
        with patch.object(images,'download',AsyncMock(return_value=picture())),\
                patch('utils.image_uploader.ImageUploader.upload_image',AsyncMock(side_effect=failed)):
            with self.assertRaisesRegex(images.ReplyImageError,'image_upload_failed'):
                await images.prepare(self.live,'https://image.example.test/source.webp',self.db)
        self.assertEqual(self.db.conn.execute('SELECT count(*) FROM auto_reply_image_cache').fetchone()[0],0)
        self.assertTrue(all(not Path(path).exists() for path in paths))

    async def test_concurrent_same_source_uploads_once(self):
        with patch.object(images,'download',AsyncMock(return_value=picture())),\
                patch('utils.image_uploader.ImageUploader.upload_image',AsyncMock(return_value='https://img.alicdn.com/shared.jpg')) as upload:
            await asyncio.gather(*[images.prepare(self.live,'https://image.example.test/source.webp',self.db) for _ in range(4)])
            upload.assert_awaited_once()

    async def fixture_server(self,handler):
        app=web.Application();app.router.add_get('/{tail:.*}',handler)
        runner=web.AppRunner(app);await runner.setup()
        server=web.TCPSite(runner,'127.0.0.1',0);await server.start()
        self.cleanups.append(runner.cleanup)
        port=server._server.sockets[0].getsockname()[1]
        async def fixture_resolve(*args,**kwargs):
            return [{'hostname':'image.fixture.test','host':'127.0.0.1','port':port,'family':socket.AF_INET,'proto':0,'flags':0}]
        return f'http://image.fixture.test:{port}',fixture_resolve

    async def test_real_stream_download_bound_and_redirect_target_validation(self):
        raw=picture()
        async def handler(request):
            if request.path=='/private':return web.Response(status=302,headers={'Location':'http://127.0.0.1/secret'})
            if request.path=='/large':return web.Response(body=b'x'*(images.MAX_BYTES+1))
            return web.Response(body=raw,content_type='image/webp')
        base,resolver=await self.fixture_server(handler)
        with patch.object(images.PublicResolver,'resolve',side_effect=resolver):
            self.assertEqual(await images.download(base+'/image'),raw)
            with self.assertRaisesRegex(images.ReplyImageError,'image_url_invalid'):await images.download(base+'/private')
            with self.assertRaisesRegex(images.ReplyImageError,'image_too_large'):await images.download(base+'/large')

    async def test_resolved_private_address_cannot_be_connected(self):
        resolver=images.PublicResolver()
        with patch.object(resolver.resolver,'resolve',AsyncMock(return_value=[{'host':'127.0.0.1'}])):
            with self.assertRaises(images.ReplyImageError):await resolver.resolve('public-name.test')
        await resolver.close()

    async def test_one_time_upgrade_recognizes_confirmed_text_but_clear_survives_restart(self):
        self.db.save_default_reply(self.live.cookie_id,True,'fixture',True,'')
        self.db.add_auto_reply_log(cookie_id=self.live.cookie_id,chat_id='chat',reply_strategy='default',text_send_status='success',image_send_status='failed')
        self.db.conn.execute('DROP TABLE auto_reply_image_cache');self.db.conn.commit()
        reopened=DBManager(self.db.db_path)
        try:
            self.assertTrue(reopened.has_default_reply_record(self.live.cookie_id,'chat'))
            reopened.clear_default_reply_records(self.live.cookie_id)
        finally:reopened.close()
        reopened=DBManager(self.db.db_path)
        try:self.assertFalse(reopened.has_default_reply_record(self.live.cookie_id,'chat'))
        finally:reopened.close()


class FirstReplyTimingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = flow_fixtures.AutoReplyFlow.asyncSetUp
    asyncTearDown = flow_fixtures.AutoReplyFlow.asyncTearDown
    instance = flow_fixtures.AutoReplyFlow.instance
    drain = flow_fixtures.AutoReplyFlow.drain
    last = flow_fixtures.AutoReplyFlow.last
    # Reuse the DB/IM fixtures, without inheriting the full base test suite below.
    async def test_first_reply_waits_five_seconds_text_precedes_image_and_next_message_is_skipped(self):
        from scripts.notification_scenarios import ACCOUNT
        self.db.save_default_reply(ACCOUNT,True,'首次文案',True,'https://img.alicdn.com/fixture.png')
        text_sent=asyncio.Event();image_release=asyncio.Event();started=time.monotonic();times=[]
        async def send_text(*args):
            times.append(time.monotonic());text_sent.set()
            return {'code':200,'body':{'messageId':'first-text'}}
        async def image_size(*args):await image_release.wait();return 100,100
        self.live.send_im_text.side_effect=send_text;self.live._get_image_size_from_url.side_effect=image_size
        await replies.accept(self.live,self.db,self.msg)
        await asyncio.wait_for(text_sent.wait(),timeout=7)
        self.assertGreaterEqual(times[0]-started,4.95)
        self.live.send_im_image.assert_not_awaited()
        await replies.accept(self.live,self.db,{**self.msg,'messageId':'second','time':self.msg['time']+10})
        image_release.set();await self.drain()
        self.live.send_im_text.assert_awaited_once();self.live.send_im_image.assert_awaited_once()
        self.assertEqual(self.last()['decision_reason'],'reply_once')
