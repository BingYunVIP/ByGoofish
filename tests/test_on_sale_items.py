"""End-to-end listing evidence, safe pagination, historical records and account isolation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

import httpx
from XianyuAutoAsync import XianyuLive
from app import reply_server
from app.auth import get_current_user
from app.db_manager import DBManager
from tests.test_item_sync_without_group import _FakeSession,make_card


class OnSaleItemsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.temp.name)/'items.db')
        self.db=DBManager(self.path)
        self.owner=self.db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        self.db.save_cookie('sale-fixture','unb=fixture',self.owner)
        self.patches=[patch('app.db_manager.db_manager',self.db),patch.object(reply_server,'db_manager',self.db)]
        for p in self.patches:p.start()
        reply_server.app.dependency_overrides[get_current_user]=lambda:{'user_id':self.owner,'username':'fixture'}
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=reply_server.app),base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        reply_server.app.dependency_overrides.pop(get_current_user,None)
        for p in reversed(self.patches):p.stop()
        self.db.close();self.temp.cleanup()

    def save(self,item='live',account='sale-fixture'):
        return self.db.save_item_info(account,item,{'title':item,'item_detail':'fixture detail'})

    def state(self,item='live',account='sale-fixture'):
        return self.db.conn.execute('SELECT listing_status,last_seen_at FROM item_info WHERE cookie_id=? AND item_id=?',(account,item)).fetchone()

    def live(self,pages,count=1):
        live=XianyuLive.__new__(XianyuLive)
        live.cookie_id=live.myid='sale-fixture'
        live.cookies_str='unb=fixture; _m_h5_tk=fixture_1'
        live.session=_FakeSession([{'ret':['SUCCESS::调用成功'],'data':{'itemGroupList':[
            {'groupName':'在售','groupId':1,'itemNumber':count},
            {'groupName':'已售出','groupId':2,'itemNumber':4}]}}]+pages)
        live._fetch_missing_item_details=AsyncMock(return_value=0)
        return live

    @staticmethod
    def page(ids,next_page=False):
        return {'ret':['SUCCESS::调用成功'],'data':{'cardList':[make_card(i) for i in ids], 'nextPage':next_page,'totalCount':0}}

    async def test_manual_order_detail_and_raw_inserts_are_unknown(self):
        self.save()
        self.db.save_item_basic_info('sale-fixture','order-item',item_title='order cached')
        self.db.batch_save_item_basic_info([{'cookie_id':'sale-fixture','item_id':'batch','item_title':'batch'}])
        self.db.update_item_title_only('sale-fixture','title-only','title')
        self.db.conn.execute("INSERT INTO item_info(cookie_id,item_id,item_title,listing_status) VALUES('sale-fixture','raw','raw','on_sale')")
        self.db.conn.commit()
        for item in ('live','order-item','batch','title-only','raw'):
            self.assertEqual(self.state(item),('unknown',None))
        self.assertEqual(self.db.get_items_by_cookie('sale-fixture',listing_status='on_sale'),[])

    async def test_one_confirmed_and_four_history_items_count_as_one(self):
        for item in ['live']+[f'history-{i}' for i in range(4)]:self.save(item)
        self.db.batch_save_item_basic_info([{'cookie_id':'sale-fixture','item_id':'live','item_title':'live'}],confirmed_on_sale=True)
        result=await self.client.get('/items',params={'listing_status':'on_sale'})
        self.assertEqual([i['item_id'] for i in result.json()['items']],['live'])
        history=await self.client.get('/items')
        self.assertEqual(len(history.json()['items']),5)
        detail=await self.client.get('/items/sale-fixture/history-0')
        self.assertEqual(detail.status_code,200)

    async def test_detail_updates_preserve_existing_confirmed_or_off_shelf_status(self):
        for item in ('live','removed'):self.save(item)
        self.db.reconcile_item_listing_status('sale-fixture',['live'])
        before=self.state()
        self.db.save_item_info('sale-fixture','live',{'title':'changed','description':'order data'})
        self.db.update_item_title_only('sale-fixture','live','new title')
        self.db.batch_save_item_basic_info([{'cookie_id':'sale-fixture','item_id':'live','item_title':'basic'}])
        self.db.save_item_info('sale-fixture','removed','new cached detail')
        self.assertEqual(self.state(),before)
        self.assertEqual(self.state('removed')[0],'off_shelf')

    async def test_full_scoped_sync_calibrates_and_preserves_history_configuration(self):
        for item in ('live','removed'):self.save(item)
        self.db.conn.execute("INSERT INTO item_delivery_configs(user_id,cookie_id,item_id,enabled,is_multi_spec) VALUES(?,'sale-fixture','removed',1,0)",(self.owner,))
        self.db.conn.commit()
        self.db.save_cookie('other-fixture','unb=fixture',self.owner)
        self.save('foreign','other-fixture')
        result=await self.live([self.page(['live'])]).get_all_items()
        self.assertTrue(result['success'])
        self.assertEqual(self.state()[0],'on_sale')
        self.assertEqual(self.state('removed')[0],'off_shelf')
        self.assertEqual(self.state('foreign','other-fixture')[0],'unknown')
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM item_delivery_configs').fetchone()[0],1)
        self.assertEqual(len(self.db.get_items_by_cookie('sale-fixture')),2)

    async def test_later_page_failure_does_not_mark_missing_items_off_shelf(self):
        for item in ('live','old'):self.save(item)
        self.db.reconcile_item_listing_status('sale-fixture',['old'])
        live=self.live([self.page(['live'],True),{'ret':['FAIL_BIZ_ERROR'],'data':{}}])
        result=await live.get_all_items()
        self.assertFalse(result['success'])
        self.assertEqual(self.state('old')[0],'on_sale')

    async def test_truncation_keeps_old_items_but_exact_final_page_calibrates(self):
        for item in ('live','old'):self.save(item)
        self.db.reconcile_item_listing_status('sale-fixture',['old'])
        result=await self.live([self.page(['live'],True)]).get_all_items(max_pages=1)
        self.assertTrue(result['success'])
        self.assertEqual(self.state('old')[0],'on_sale')
        result=await self.live([self.page(['live'],False)]).get_all_items(max_pages=1)
        self.assertTrue(result['success'])
        self.assertEqual(self.state('old')[0],'off_shelf')

    async def test_empty_on_sale_group_calibrates_zero_without_deleting(self):
        self.save();self.db.reconcile_item_listing_status('sale-fixture',['live'])
        result=await self.live([self.page([])],count=0).get_all_items()
        self.assertTrue(result['success']);self.assertTrue(result['confirmed_empty'])
        self.assertEqual(self.state()[0],'off_shelf')
        self.assertEqual(len(self.db.get_items_by_cookie('sale-fixture')),1)

    async def test_unknown_group_cannot_write_or_calibrate(self):
        self.save();self.db.reconcile_item_listing_status('sale-fixture',['live'])
        live=self.live([])
        live.session=_FakeSession([{'ret':['SUCCESS::调用成功'],'data':{'cardList':[make_card('unscoped')]}}])
        result=await live.get_all_items()
        self.assertFalse(result['success'])
        self.assertEqual(self.state()[0],'on_sale')
        self.assertEqual(len(self.db.get_items_by_cookie('sale-fixture')),1)

    async def test_migration_repairs_default_on_sale_but_does_not_invent_evidence(self):
        self.save('confirmed');self.db.reconcile_item_listing_status('sale-fixture',['confirmed'])
        self.db.conn.execute('DROP TRIGGER item_listing_evidence_insert')
        self.db.conn.execute('DROP TRIGGER item_listing_evidence_update')
        self.db.conn.execute("INSERT INTO item_info(cookie_id,item_id,item_title,listing_status) VALUES('sale-fixture','stale','stale','on_sale')")
        self.db.conn.commit()
        cursor=self.db.conn.cursor();self.db._migrate_item_listing_status(cursor);self.db.conn.commit()
        self.assertEqual(self.state('stale'),('unknown',None))
        self.assertEqual(self.state('confirmed')[0],'on_sale')
        self.db._migrate_item_listing_status(cursor)
        self.db.conn.commit()
        self.assertEqual(self.state('stale'),('unknown',None))

    async def test_empty_page_claiming_more_is_not_confirmed_empty(self):
        self.save();self.db.reconcile_item_listing_status('sale-fixture',['live'])
        live=self.live([self.page([],True)],count=0)
        result=await live.get_all_items()
        self.assertFalse(result['success'])
        self.assertEqual(self.state()[0],'on_sale')
        self.assertEqual(len(live.session.requests),2)

    async def test_repeated_last_page_is_not_used_as_a_complete_snapshot(self):
        for item in ('live','old'):self.save(item)
        self.db.reconcile_item_listing_status('sale-fixture',['old'])
        result=await self.live([self.page(['live'],True),self.page(['live'],False)],count=2).get_all_items()
        self.assertFalse(result['success'])
        self.assertEqual(self.state('old')[0],'on_sale')

    async def test_explicit_response_scope_conflict_is_rejected(self):
        page=self.page(['sold']);page['data']['groupName']='已售出'
        self.save();self.db.reconcile_item_listing_status('sale-fixture',['live'])
        result=await self.live([page]).get_all_items()
        self.assertFalse(result['success'])
        self.assertIsNone(self.state('sold'))
        self.assertEqual(self.state()[0],'on_sale')

    async def test_failed_commit_cannot_claim_saved_items_or_calibrate(self):
        self.save('old');self.db.reconcile_item_listing_status('sale-fixture',['old'])
        self.db.conn.execute('PRAGMA foreign_keys=ON')
        self.db.conn.execute('CREATE TABLE commit_guard (account TEXT REFERENCES cookies(id) DEFERRABLE INITIALLY DEFERRED)')
        self.db.conn.execute("CREATE TRIGGER fail_item_commit AFTER INSERT ON item_info BEGIN INSERT INTO commit_guard VALUES('missing-fixture'); END")
        self.db.conn.commit()
        result=await self.live([self.page(['new'])]).get_all_items()
        self.assertFalse(result['success'])
        self.assertIsNone(self.state('new'))
        self.assertEqual(self.state('old')[0],'on_sale')

    async def test_filtered_routes_validate_filter_and_enforce_account_owner(self):
        self.db.conn.execute("INSERT INTO users(username,email,password_hash) VALUES('other','other@example.test','fixture')")
        other=self.db.conn.execute("SELECT id FROM users WHERE username='other'").fetchone()[0]
        self.db.conn.commit();self.db.save_cookie('foreign-fixture','unb=fixture',other)
        self.save('foreign','foreign-fixture');self.db.reconcile_item_listing_status('foreign-fixture',['foreign'])
        self.save();self.db.reconcile_item_listing_status('sale-fixture',['live'])
        rows=(await self.client.get('/items?listing_status=on_sale')).json()['items']
        self.assertEqual([x['item_id'] for x in rows],['live'])
        self.assertEqual((await self.client.get('/items/cookie/foreign-fixture?listing_status=on_sale')).status_code,403)
        self.assertEqual((await self.client.get('/items?listing_status=invalid')).status_code,400)
        self.assertEqual((await self.client.get('/items/cookie/sale-fixture?listing_status=invalid')).status_code,400)
