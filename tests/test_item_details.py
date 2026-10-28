"""Regression for raw list-card contamination, provenance and bounded acquisition."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from app.db_manager import DBManager
from app.services import item_details, auth_recovery
from app import reply_server
from app.auth import get_current_user
from XianyuAutoAsync import XianyuLive

CARD = {'title': '示例商品', 'price': '1.02', 'detail_params': {'itemId': 'fixture-item'},
        'track_params': {'example': 'fixture'}, 'item_label_data': {'label': '热销'}, 'card_type': 1003}


class ItemDetailTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'details.db')
        self.db = DBManager(self.path)
        self.owner = self.db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        for account in ('fixture-shop', 'other-shop'):
            self.db.save_cookie(account,'fixture=value',self.owner)
            self.db.save_item_basic_info(account,'fixture-item',item_title='示例商品')
        self.patches = [patch('app.db_manager.db_manager', self.db),patch.object(reply_server,'db_manager',self.db)]
        for p in self.patches: p.start()
        reply_server.app.dependency_overrides[get_current_user] = lambda: {'user_id': self.owner}
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=reply_server.app),base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        reply_server.app.dependency_overrides.pop(get_current_user,None)
        for p in reversed(self.patches): p.stop()
        self.db.close()
        self.temp.cleanup()

    def item(self,account='fixture-shop'):
        return self.db.get_item_info(account,'fixture-item')

    def live(self):
        live=XianyuLive.__new__(XianyuLive)
        live.cookie_id='fixture-shop'
        live._fetch_item_detail_from_browser=AsyncMock(return_value='真实说明\n第二行')
        live.get_item_info=AsyncMock(return_value={'ret':['SUCCESS::调用成功'],'data':{'itemDO':{}}})
        return live

    async def test_migration_repairs_only_known_card_shape_and_is_idempotent(self):
        self.db.conn.execute("UPDATE item_info SET item_detail=?,listing_status='on_sale',last_seen_at=CURRENT_TIMESTAMP WHERE cookie_id='fixture-shop'",(json.dumps(CARD),))
        self.db.conn.execute("UPDATE item_info SET item_detail=? WHERE cookie_id='other-shop'",('{"my_custom":"用户填写的JSON"}',))
        self.db.conn.commit()
        before=self.db.conn.execute("SELECT listing_status,last_seen_at FROM item_info WHERE cookie_id='fixture-shop'").fetchone()
        item_details.initialize(self.db)
        first=self.item()
        self.assertEqual(first['item_detail'],'')
        self.assertEqual(first['detail_status'],'missing')
        self.assertEqual(self.item('other-shop')['detail_source'],'manual')
        raw=self.db.conn.execute("SELECT platform_metadata FROM item_info WHERE cookie_id='fixture-shop'").fetchone()[0]
        self.assertEqual(json.loads(raw),CARD)
        item_details.initialize(self.db)
        self.assertEqual(first,self.item())
        self.assertEqual(before,self.db.conn.execute("SELECT listing_status,last_seen_at FROM item_info WHERE cookie_id='fixture-shop'").fetchone())

    async def test_migration_uses_reliable_description(self):
        self.db.conn.execute("UPDATE item_info SET item_detail=?,item_description='真实说明'",(json.dumps(CARD),))
        self.db.conn.commit();item_details.initialize(self.db)
        self.assertEqual(self.item()['item_detail'],'真实说明')
        self.assertEqual(self.item()['detail_source'],'platform')

    async def test_basic_batch_and_object_savers_never_serialize_object_to_prose(self):
        self.db.save_item_info('fixture-shop','fixture-item',CARD)
        self.assertEqual(self.item()['item_detail'],'')
        self.db.save_item_basic_info('fixture-shop','fixture-item',item_detail=json.dumps(CARD))
        self.db.batch_save_item_basic_info([{'cookie_id':'fixture-shop','item_id':'fixture-item','item_title':'商品','item_detail':json.dumps(CARD)}])
        self.assertEqual(self.item()['item_detail'],'')
        self.assertEqual(self.item()['detail_source'],'unknown')

    async def test_metadata_not_returned_by_any_existing_reader_or_api(self):
        self.db.save_item_info('fixture-shop','fixture-item',CARD)
        for rows in ([self.item()],self.db.get_all_items(),self.db.get_items_by_cookie('fixture-shop')):
            for row in rows:
                self.assertNotIn('platform_metadata',row)
                self.assertNotIn('item_detail_parsed',row)
        response=await self.client.get('/items/fixture-shop/fixture-item')
        self.assertEqual(response.status_code,200)
        self.assertNotIn('track_params',response.text)

    async def test_manual_edit_and_manual_clear_survive_every_automatic_writer(self):
        for text in ('人工说明',''):
            response=await self.client.put('/items/fixture-shop/fixture-item',json={'item_detail':text})
            self.assertEqual(response.status_code,200)
            self.db.update_item_detail('fixture-shop','fixture-item','平台正文',source='platform')
            self.db.save_item_basic_info('fixture-shop','fixture-item',item_detail='补录')
            self.db.batch_save_item_basic_info([{'cookie_id':'fixture-shop','item_id':'fixture-item','item_title':'标题','item_detail':'自动同步'}])
            self.db.save_item_info('fixture-shop','fixture-item',{'title':'标题','description':'平台描述'})
            self.db.update_item_title_only('fixture-shop','fixture-item','新标题')
            self.assertEqual(self.item()['item_detail'],text)
            self.assertEqual(self.item()['detail_source'],'manual')
            self.assertFalse(item_details.claim(self.db,'fixture-shop','fixture-item'))

    async def test_manual_create_without_prose_defaults_to_automatic_acquisition(self):
        response=await self.client.post('/items',json={'cookie_id':'fixture-shop','item_id':'manual','title':'人工商品','detail':''})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['item']['detail_source'],'unknown')

    async def test_official_api_prose_is_automatic_first_choice(self):
        live=self.live()
        live.get_item_info.return_value={'ret':['SUCCESS::调用成功'],'data':{'itemDO':{'itemId':'fixture-item','desc':'官方正文\n保留换行'},'trackParams':{'label':'不能写入'}}}
        self.assertEqual(await live.fetch_item_detail_from_api('fixture-item'),'官方正文\n保留换行')
        self.assertEqual(self.item()['item_detail'],'官方正文\n保留换行')
        live._fetch_item_detail_from_browser.assert_not_awaited()

    async def test_api_challenge_or_rate_limit_does_not_fall_back_or_retry(self):
        for category in ('authentication_required','rate_limited','http_error','credentials_missing'):
            self.db.conn.execute("UPDATE item_info SET detail_status='missing',detail_retry_at=NULL")
            self.db.conn.commit()
            live=self.live();live.get_item_info.return_value={'error_code':category}
            self.assertEqual(await live.fetch_item_detail_from_api('fixture-item'),'')
            live.get_item_info.assert_awaited_once()
            live._fetch_item_detail_from_browser.assert_not_awaited()
            self.assertEqual(self.item()['detail_failure'],category)

    async def test_api_identity_mismatch_cannot_fill_wrong_product(self):
        live=self.live();live.get_item_info.return_value={'data':{'itemDO':{'itemId':'another','desc':'别的商品'}}}
        self.assertEqual(await live.fetch_item_detail_from_api('fixture-item'),'')
        self.assertEqual(self.item()['detail_failure'],'item_identity_mismatch')
        live._fetch_item_detail_from_browser.assert_not_awaited()

    async def test_official_detail_request_is_single_read_no_challenge_retry(self):
        from tests.test_item_sync_without_group import _FakeSession
        live=self.live()
        live.cookies_str='unb=fixture; _m_h5_tk=fixture_1'
        live.session=_FakeSession([{'ret':['FAIL_SYS_USER_VALIDATE::请验证'],'data':{}}])
        result=await XianyuLive.get_item_info(live,'fixture-item')
        self.assertEqual(result['error_code'],'authentication_required')
        self.assertEqual(live.session.requests,[{'itemId':'fixture-item'}])

    async def test_failed_api_response_does_not_import_challenge_cookies(self):
        from tests.test_item_sync_without_group import _FakeResponse
        from multidict import CIMultiDict
        from types import SimpleNamespace
        live=self.live();live.cookies_str='unb=fixture; _m_h5_tk=fixture_1'
        response=_FakeResponse({'ret':['FAIL_SYS_USER_VALIDATE::请验证']})
        response.headers=CIMultiDict({'set-cookie':'x5secdata=synthetic-challenge; Path=/'})
        live.session=SimpleNamespace(post=lambda *a,**kw:response)
        before=self.db.get_cookie('fixture-shop')
        result=await XianyuLive.get_item_info(live,'fixture-item')
        self.assertEqual(result['error_code'],'authentication_required')
        self.assertEqual(self.db.get_cookie('fixture-shop'),before)

    async def test_acquisition_preserves_newlines_and_persists_result(self):
        live=self.live()
        self.assertEqual(await live.fetch_item_detail_from_api('fixture-item'),'真实说明\n第二行')
        self.assertEqual(self.item()['detail_source'],'platform')
        self.assertEqual(self.item()['detail_status'],'ready')
        await live.fetch_item_detail_from_api('fixture-item')
        live._fetch_item_detail_from_browser.assert_awaited_once()

    async def test_failure_is_six_hour_cooldown_persisted_across_connections(self):
        live=self.live();live._fetch_item_detail_from_browser.side_effect=item_details.DetailFetchError('description_not_found')
        await live.fetch_item_detail_from_api('fixture-item')
        self.assertEqual(self.item()['detail_status'],'failed')
        self.assertGreaterEqual(self.item()['detail_retry_at'],time.time()+21598)
        item_details.initialize(self.db)
        await live.fetch_item_detail_from_api('fixture-item')
        live._fetch_item_detail_from_browser.assert_awaited_once()
        other=DBManager(self.path)
        self.assertFalse(item_details.claim(other,'fixture-shop','fixture-item'));other.close()

    async def test_timeout_and_network_error_are_bounded_failures(self):
        for error,category in ((TimeoutError(),'timeout'),(RuntimeError('net::ERR_CONNECTION_RESET'),'network_error')):
            self.db.conn.execute("UPDATE item_info SET detail_retry_at=NULL,detail_status='missing'")
            self.db.conn.commit()
            live=self.live();live._fetch_item_detail_from_browser.side_effect=error
            self.assertEqual(await live.fetch_item_detail_from_api('fixture-item'),'')
            self.assertEqual(self.item()['detail_failure'],category)
            self.assertEqual(self.item()['item_detail'],'')

    async def test_manual_edit_during_fetch_wins(self):
        live=self.live()
        async def fetch(_):
            self.db.update_item_detail('fixture-shop','fixture-item','刚刚人工保存')
            return '平台正文'
        live._fetch_item_detail_from_browser.side_effect=fetch
        self.assertEqual(await live.fetch_item_detail_from_api('fixture-item'),'')
        self.assertEqual(self.item()['item_detail'],'刚刚人工保存')

    async def test_cancelled_acquisition_records_failure_and_propagates_cancel(self):
        live=self.live();live._fetch_item_detail_from_browser.side_effect=asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError): await live.fetch_item_detail_from_api('fixture-item')
        self.assertEqual(self.item()['detail_failure'],'interrupted')

    async def test_account_isolation_and_concurrent_claim(self):
        self.assertTrue(item_details.claim(self.db,'fixture-shop','fixture-item'))
        self.assertFalse(item_details.claim(self.db,'fixture-shop','fixture-item'))
        self.assertTrue(item_details.claim(self.db,'other-shop','fixture-item'))

    async def test_authentication_blocks_whole_batch_until_controlled_recovery(self):
        self.db.save_item_basic_info('fixture-shop','next',item_title='下一件')
        live=self.live();live.get_item_info.return_value={'error_code':'authentication_required'}
        result=await live._fetch_missing_item_details([{'item_id':'fixture-item'},{'item_id':'next'}])
        self.assertEqual(result,0)
        live.get_item_info.assert_awaited_once()
        live._fetch_item_detail_from_browser.assert_not_awaited()
        self.assertEqual(self.item()['detail_status'],'blocked')
        self.assertFalse(item_details.claim(self.db,'fixture-shop','next'))
        auth_recovery.set_state(self.db,'fixture-shop','checking')
        auth_recovery.set_state(self.db,'fixture-shop','recovered')
        self.assertTrue(item_details.claim(self.db,'fixture-shop','next'))

    async def test_browser_restriction_does_not_block_official_api_and_never_reopens_browser(self):
        live=self.live();live._fetch_item_detail_from_browser.side_effect=item_details.DetailFetchError('authentication_required')
        await live.fetch_item_detail_from_api('fixture-item')
        self.assertEqual(self.item()['detail_failure'],'browser_authentication_required')
        self.assertEqual(self.item()['detail_status'],'failed')
        self.assertEqual(self.item()['detail_browser_blocked'],1)
        self.db.conn.execute("UPDATE item_info SET detail_retry_at=NULL")
        self.db.conn.commit()
        live.get_item_info.return_value={'data':{'itemDO':{'desc':'官网接口正文'}}}
        self.assertEqual(await live.fetch_item_detail_from_api('fixture-item'),'官网接口正文')
        live._fetch_item_detail_from_browser.assert_awaited_once()

    async def test_paused_account_never_opens_browser(self):
        auth_recovery.set_state(self.db,'fixture-shop','required','message_verification_required')
        live=self.live();await live.fetch_item_detail_from_api('fixture-item')
        live._fetch_item_detail_from_browser.assert_not_awaited()
        self.assertEqual(self.item()['detail_status'],'blocked')

    async def test_card_sync_saves_metadata_and_queues_missing_prose(self):
        live=self.live();live._fetch_missing_item_details=AsyncMock(return_value=0)
        result=await live.save_items_list_to_db([dict(CARD,id='fixture-item')])
        self.assertEqual(result,1)
        self.assertEqual(self.item()['item_detail'],'')
        live._fetch_missing_item_details.assert_awaited_once()
        self.db.update_item_detail('fixture-shop','fixture-item','')
        live._fetch_missing_item_details.reset_mock()
        await live.save_items_list_to_db([dict(CARD,id='fixture-item')])
        live._fetch_missing_item_details.assert_not_awaited()

    async def test_cross_owner_detail_access_denied(self):
        reply_server.app.dependency_overrides[get_current_user]=lambda:{'user_id':self.owner+100}
        for method in ('get','put'):
            response=await getattr(self.client,method)('/items/fixture-shop/fixture-item',**({'json':{'item_detail':'wrong'}} if method=='put' else {}))
            self.assertEqual(response.status_code,403)


class DescriptionPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        from utils.direct_network import direct_browser_options
        self.p=await async_playwright().start()
        self.browser=await self.p.chromium.launch(**direct_browser_options({'headless':True,'channel':'chromium','args':['--no-sandbox']}))
        self.page=await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close();await self.p.stop()

    async def test_description_module_hash_changes_and_newlines(self):
        await self.page.set_content('<div class="desc--NewHash">真实商品说明<br>第二行</div><p>热销第1名</p>')
        self.assertEqual(await item_details.extract_page_description(self.page),'真实商品说明\n第二行')

    async def test_hidden_challenge_does_not_block_real_description(self):
        await self.page.set_content('<div id="nocaptcha" style="display:none"></div><div class="x desc--NewHash">真实正文</div>')
        self.assertEqual(await item_details.extract_page_description(self.page),'真实正文')

    async def test_login_template_does_not_override_product_prose_or_count_as_visible_restriction(self):
        await self.page.set_content('<p>请先登录</p><div class="desc--x">公开商品说明</div>')
        self.assertEqual(await item_details.extract_page_description(self.page),'公开商品说明')
        await self.page.set_content('<p style="display:none">请先登录</p><h1>仅标题</h1>')
        with self.assertRaises(item_details.DetailFetchError) as error:
            await item_details.extract_page_description(self.page)
        self.assertEqual(error.exception.category,'description_not_found')

    async def test_login_error_challenge_and_unrelated_page_never_become_prose(self):
        for html,category in [('<div id="nocaptcha">Oops</div>','authentication_required'),
                              ('<div class="desc--x">请先登录</div>','authentication_required'),
                              ('<p>非法访问</p>','authentication_required'),
                              ('<div class="desc--x" style="display:none">隐藏内容</div><p>热销第1名</p>','description_not_found'),
                              ('<h1>商品标题</h1><span>¥1.02</span><p>3人想要</p>','description_not_found')]:
            await self.page.set_content(html)
            with self.assertRaises(item_details.DetailFetchError) as error:
                await item_details.extract_page_description(self.page)
            self.assertEqual(error.exception.category,category)
