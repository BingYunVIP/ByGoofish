import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from fastapi import HTTPException
from app.db_manager import DBManager
from app import reply_server
from utils import product_images as images
from app.services import native_capabilities as native


class ProductImageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=DBManager(str(Path(self.temp.name)/'images.db'))
        self.uid=self.db.get_user_by_username('admin')['id']
        self.db.create_user('image-other','image-other@example.invalid','FixtureImage88')
        self.other=self.db.get_user_by_username('image-other')['id']
        for account,owner in [('image-a',self.uid),('image-b',self.uid),('image-private',self.other)]:
            self.db.save_cookie(account,'unb=fixture',owner)
            self.db.save_item_basic_info(account,'shared-product',item_title=account,item_image='https://img.alicdn.com/'+account+'.jpg')
            self.db.conn.execute('INSERT INTO orders(order_id,cookie_id,item_id,order_status) VALUES(?,?,?,?)',('order-'+account,account,'shared-product','completed'))
        self.db.conn.commit()
        self.user={'user_id':self.uid,'username':'fixture'}
        self.patches=[patch('app.db_manager.db_manager',self.db),patch.object(reply_server,'db_manager',self.db)]
        for p in self.patches:p.start()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.db.close();self.temp.cleanup()

    def test_shared_url_fixtures(self):
        for original,expected in json.loads((Path(__file__).parent/'fixtures/product_image_urls.json').read_text()):
            self.assertEqual(images.normalize_image_url(original),expected)

    def test_order_list_and_details_use_account_and_item_not_global_item(self):
        result=reply_server.get_user_orders(self.user,1,20,None,None,None,None)
        self.assertEqual(len(result['data']),2)
        for order in result['data']:
            account=order['cookie_id']
            self.assertEqual(order['item_title'],account)
            self.assertEqual(order['item_image'],'https://img.alicdn.com/'+account+'.jpg')
            detail=reply_server.get_order_detail(order['order_id'],self.user)['data']
            self.assertEqual(detail['item_image'],order['item_image'])
        with self.assertRaises(HTTPException) as error:
            reply_server.get_user_orders(self.user,1,20,'image-private',None,None,None)
        self.assertEqual(error.exception.status_code,403)

    def test_url_migration_only_changes_image_and_is_idempotent(self):
        self.db.conn.execute("UPDATE item_info SET item_image='http://img.alicdn.com/legacy.jpg?x=1',listing_status='on_sale',last_seen_at='fixture',item_detail='manual prose',detail_source='manual' WHERE cookie_id='image-a'")
        self.db.conn.commit()
        before=self.db.conn.execute("SELECT * FROM item_info WHERE cookie_id='image-a'").fetchone()
        columns=[r[1] for r in self.db.conn.execute('PRAGMA table_info(item_info)')]
        images.initialize(self.db);images.initialize(self.db)
        after=self.db.conn.execute("SELECT * FROM item_info WHERE cookie_id='image-a'").fetchone()
        self.assertEqual([name for name,a,b in zip(columns,before,after) if a!=b],['item_image'])
        self.assertEqual(self.db.get_item_info('image-a','shared-product')['item_image'],'https://img.alicdn.com/legacy.jpg?x=1')

    def test_main_image_requires_identity_and_ignores_avatars_and_other_products(self):
        result={'data':{'itemDO':{'itemId':'shared-product','imageInfos':[{'url':'http://img.alicdn.com/secondary.jpg'}, {'url':'http://img.alicdn.com/main.jpg','major':True}]},'sellerDO':{'avatar':'https://img.alicdn.com/avatar.jpg'}}}
        self.assertEqual(images.main_image(result,'shared-product'),'https://img.alicdn.com/main.jpg')
        self.assertEqual(images.main_image(result,'other-product'),'')
        result['data']['itemDO'].pop('imageInfos');self.assertEqual(images.main_image(result,'shared-product'),'')
        result['data']['itemDO'].pop('itemId');self.assertEqual(images.main_image(result,'shared-product'),'')

    def test_fill_does_not_overwrite_concurrent_edit_or_other_account(self):
        response={'data':{'itemDO':{'itemId':'shared-product','picUrl':'http://img.alicdn.com/new.jpg'}}}
        images.fill_missing(self.db,'image-a','shared-product',response)
        self.assertEqual(self.db.get_item_info('image-a','shared-product')['item_image'],'https://img.alicdn.com/image-a.jpg')
        self.db.conn.execute("UPDATE item_info SET item_image='' WHERE cookie_id='image-a'");self.db.conn.commit()
        images.fill_missing(self.db,'image-a','shared-product',response)
        self.assertEqual(self.db.get_item_info('image-a','shared-product')['item_image'],'https://img.alicdn.com/new.jpg')
        self.assertEqual(self.db.get_item_info('image-b','shared-product')['item_image'],'https://img.alicdn.com/image-b.jpg')

    def test_refresh_is_explicit_one_request_and_stops_on_auth_failure(self):
        self.db.conn.execute("UPDATE item_info SET item_image='' WHERE cookie_id='image-a'");self.db.conn.commit()
        live=SimpleNamespace(get_item_info=AsyncMock(return_value={'error_code':'authentication_required'}))
        async def run(account,operation):return await operation(live)
        with patch.object(reply_server,'_run_on_account_loop',side_effect=run),patch('app.services.auth_recovery.auth_paused',return_value=False):
            result=asyncio.run(reply_server.refresh_product_image('image-a','shared-product',self.user))
            self.assertEqual(result['failure_category'],'authentication_required')
            self.assertEqual(live.get_item_info.await_count,1)
        with self.assertRaises(HTTPException):
            asyncio.run(reply_server.refresh_product_image('image-private','shared-product',self.user))

    def test_native_release_defaults_and_explicit_overrides(self):
        caps=native.capabilities(self.db)
        self.assertEqual(set(caps),set(native.KINDS))
        self.assertTrue(all(c['supported'] and c['source']=='release' and c['protocol_revision'] for c in caps.values()))
        for status in ('failed','disabled'):
            self.db.conn.execute("UPDATE chat_native_capabilities SET status=?,reason='fixture failure' WHERE kind='forward_image'",(status,));self.db.conn.commit()
            native.initialize(self.db)
            self.assertFalse(native.capabilities(self.db)['forward_image']['supported'])
            with self.assertRaises(HTTPException):native.require(self.db,self.uid,'image-b','forward_image')
        self.db.conn.execute("UPDATE chat_native_capabilities SET status='verified',protocol_revision='different-protocol' WHERE kind='reply_text'")
        self.db.conn.commit();self.assertFalse(native.capabilities(self.db)['reply_text']['supported'])
        with self.assertRaises(HTTPException):native.require(self.db,self.uid,'image-b','reply_image')

    def test_old_initialization_marker_migrates_but_failure_is_preserved(self):
        self.db.conn.execute('DROP TABLE chat_native_capabilities')
        self.db.conn.execute('CREATE TABLE chat_native_capabilities(kind TEXT PRIMARY KEY,status TEXT,reason TEXT,updated_at REAL)')
        self.db.conn.execute("INSERT INTO chat_native_capabilities VALUES('reply_text','unverified','原生能力尚未完成实际验收',0)")
        self.db.conn.execute("INSERT INTO chat_native_capabilities VALUES('forward_text','failed','real failure',0)")
        self.db.conn.commit();native.initialize(self.db);native.initialize(self.db)
        self.assertTrue(native.capabilities(self.db)['reply_text']['supported'])
        self.assertFalse(native.capabilities(self.db)['forward_text']['supported'])
