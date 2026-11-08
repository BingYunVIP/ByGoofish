"""Regression for raw list-card contamination, provenance and bounded acquisition."""
import asyncio
import json
import tempfile
import time
import unittest
from contextlib import asynccontextmanager

@asynccontextmanager
async def fake_detail_session(session):
    yield session
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
            self.db.conn.execute('UPDATE item_detail_accounts SET blocked=0')
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
        live.cookies_str='unb=fixture; _m_h5_tk=fixture_1; _m_h5_tk_enc=fixture-enc'
        self.db.update_cookie_account_info('fixture-shop',cookie_value=live.cookies_str)
        live.session=_FakeSession([{'ret':['FAIL_SYS_USER_VALIDATE::请验证'],'data':{}}])
        with patch('aiohttp.ClientSession',return_value=fake_detail_session(live.session)):
            result=await XianyuLive.get_item_info(live,'fixture-item')
        self.assertEqual(result['error_code'],'security_verification_required')
        self.assertEqual(live.session.requests,[{'itemId':'fixture-item'}])

    async def test_failed_api_response_does_not_import_challenge_cookies(self):
        from tests.test_item_sync_without_group import _FakeResponse
        from multidict import CIMultiDict
        from types import SimpleNamespace
        live=self.live();live.cookies_str='unb=fixture; _m_h5_tk=fixture_1; _m_h5_tk_enc=fixture-enc'
        self.db.update_cookie_account_info('fixture-shop',cookie_value=live.cookies_str)
        response=_FakeResponse({'ret':['FAIL_SYS_USER_VALIDATE::请验证']})
        response.headers=CIMultiDict({'set-cookie':'x5secdata=synthetic-challenge; Path=/'})
        live.session=SimpleNamespace(post=lambda *a,**kw:response)
        before=self.db.get_cookie('fixture-shop')
        with patch('aiohttp.ClientSession',return_value=fake_detail_session(live.session)):
            result=await XianyuLive.get_item_info(live,'fixture-item')
        self.assertEqual(result['error_code'],'security_verification_required')
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
        self.assertFalse(item_details.claim(self.db,'fixture-shop','next'))
        item_details.credentials_changed(self.db,'fixture-shop','cookie2=before','cookie2=after')
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
        self.assertEqual(self.item()['detail_status'],'missing')

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

    async def test_failure_classification_never_infers_validation_from_403(self):
        classify=item_details.classify
        self.assertEqual(classify(403,None)[0],'access_denied')
        self.assertEqual(classify(302,None,'https://evil.invalid/login')[0],'access_denied')
        self.assertEqual(classify(302,None,'https://login.taobao.com/login')[0],'login_expired')
        for code,category in [('FAIL_SYS_USER_VALIDATE','security_verification_required'),('FAIL_SYS_SESSION_EXPIRED','login_expired'),('FAIL_SYS_TOKEN_EXOIRED','signing_expired'),('FAIL_BIZ_ITEM_NO_PERMISSION','item_unavailable')]:
            self.assertEqual(classify(200,{'ret':[code+'::untrusted secret prose']})[0],category)

    async def test_diagnostics_are_allowlisted_bounded_and_account_isolated(self):
        identity=item_details.record_diagnostic(self.db,'fixture-shop','fixture-item',0,'access_denied',http_status=403,codes=['FAIL_SYS_ILLEGAL_ACCESS','cookie=synthetic-secret','https://evil.invalid/?sign=synthetic'])
        result=item_details.diagnostic(self.db,identity,'fixture-shop')
        self.assertEqual(result['platform_codes'],['FAIL_SYS_ILLEGAL_ACCESS'])
        self.assertNotIn('synthetic',json.dumps(result))
        self.assertIsNone(item_details.diagnostic(self.db,identity,'other-shop'))
        self.db.conn.execute('UPDATE item_detail_diagnostics SET codes=?,category=?,stage=? WHERE id=?', ('[\"cookie=secret\",\"SUCCESS\"]','cookie=secret','https://secret.invalid',identity))
        self.db.conn.commit()
        self.assertNotIn('secret',json.dumps(item_details.diagnostic(self.db,identity,'fixture-shop')))
        self.assertTrue(self.item()['detail_diagnostic_id'])

    async def test_explicit_credential_generation_persists_and_deduplicates(self):
        self.assertFalse(item_details.credentials_changed(self.db,'fixture-shop','same','same'))
        self.assertTrue(item_details.credentials_changed(self.db,'fixture-shop','cookie2=old','cookie2=new'))
        self.assertTrue(item_details.take_pending(self.db,'fixture-shop'))
        self.assertFalse(item_details.take_pending(self.db,'fixture-shop'))
        other=DBManager(self.path)
        self.assertFalse(item_details.take_pending(other,'fixture-shop'))
        self.assertEqual(item_details.generation(other,'fixture-shop'),1)
        other.close()

    async def test_old_inflight_result_cannot_overwrite_new_credentials(self):
        live=self.live()
        async def changed(_):
            item_details.credentials_changed(self.db,'fixture-shop','cookie2=old','cookie2=new')
            return 'old response'
        live._fetch_item_detail_from_browser.side_effect=changed
        self.assertEqual(await live.fetch_item_detail_from_api('fixture-item'),'')
        self.assertEqual(self.item()['item_detail'],'')
        item_details.failure(self.db,'fixture-shop','fixture-item','login_expired',expected_generation=0)
        self.assertEqual(self.item()['detail_status'],'missing')

    async def test_item_specific_denial_does_not_pause_other_products(self):
        self.db.save_item_basic_info('fixture-shop','next',item_title='next')
        item_details.failure(self.db,'fixture-shop','fixture-item','access_denied')
        self.assertTrue(item_details.claim(self.db,'fixture-shop','next'))

    async def test_legacy_probe_is_explicit_once_per_account(self):
        self.db.conn.execute("UPDATE item_info SET detail_status='blocked',detail_failure='authentication_required',detail_retry_at=? WHERE cookie_id='fixture-shop'",(time.time()+21600,))
        self.db.conn.commit()
        self.assertFalse(item_details.claim(self.db,'fixture-shop','fixture-item'))
        self.assertTrue(item_details.claim(self.db,'fixture-shop','fixture-item',explicit=True))
        self.assertFalse(item_details.claim(self.db,'fixture-shop','fixture-item',explicit=True))

    async def test_snapshot_request_rejects_challenge_cookie_pollution(self):
        from tests.test_item_sync_without_group import _FakeResponse
        from multidict import CIMultiDict
        from types import SimpleNamespace
        value='unb=fixture; _m_h5_tk=before_1; _m_h5_tk_enc=enc; cookie2=session'
        self.db.update_cookie_account_info('fixture-shop',cookie_value=value)
        live=self.live();live.cookies_str=value
        response=_FakeResponse({'ret':['FAIL_SYS_USER_VALIDATE::do-not-save-body']})
        response.status=403;response.headers=CIMultiDict({'set-cookie':'cookie2=attacker; Path=/'})
        factory=SimpleNamespace(post=lambda *args,**kwargs:response)
        with patch('aiohttp.ClientSession',return_value=fake_detail_session(factory)) as client:
            result=await XianyuLive.get_item_info(live,'fixture-item')
        self.assertEqual(result['error_code'],'security_verification_required')
        self.assertEqual(self.db.get_cookie('fixture-shop'),value)
        self.assertEqual(client.call_args.kwargs['headers']['cookie'],value)
        self.assertFalse(client.call_args.kwargs['trust_env'])
        self.assertEqual(item_details.diagnostic(self.db,result['diagnostic_id'],'fixture-shop')['http_status'],403)

    async def test_signing_expired_retries_only_with_new_safe_pair(self):
        from tests.test_item_sync_without_group import _FakeResponse
        from multidict import CIMultiDict
        from types import SimpleNamespace
        value='unb=fixture; _m_h5_tk=before_1; _m_h5_tk_enc=enc; cookie2=session'
        self.db.update_cookie_account_info('fixture-shop',cookie_value=value)
        live=self.live();live.cookies_str=value
        live.session=SimpleNamespace(headers={'cookie':value},closed=False)
        first=_FakeResponse({'ret':['FAIL_SYS_TOKEN_EXOIRED::expired']})
        first.headers=CIMultiDict([('set-cookie','_m_h5_tk=after_2; Path=/'),('set-cookie','_m_h5_tk_enc=new-enc; Path=/'),('set-cookie','cookie2=must-not-import; Path=/')])
        second=_FakeResponse({'ret':['SUCCESS::ok'],'data':{'itemDO':{'desc':'正文'}}})
        second.headers=CIMultiDict()
        sessions=[SimpleNamespace(post=lambda *a,**k:first),SimpleNamespace(post=lambda *a,**k:second)]
        with patch('aiohttp.ClientSession',side_effect=[fake_detail_session(v) for v in sessions]) as client:
            result=await XianyuLive.get_item_info(live,'fixture-item')
        self.assertEqual(client.call_count,2)
        self.assertEqual(item_details.api_description(result,'fixture-item'),'正文')
        self.assertIn('cookie2=session',self.db.get_cookie('fixture-shop'))
        self.assertEqual(item_details.generation(self.db,'fixture-shop'),0)
        self.assertEqual(live.session.headers['cookie'],self.db.get_cookie('fixture-shop'))

    async def test_concurrent_refresh_singleflight_never_duplicates_network(self):
        live=self.live()
        async def read(_):
            await asyncio.sleep(.01)
            return {'data':{'itemDO':{'desc':'并发正文'}}}
        live.get_item_info.side_effect=read
        await asyncio.gather(live.refresh_item_detail('fixture-item'),live.refresh_item_detail('fixture-item'))
        live.get_item_info.assert_awaited_once()
        self.assertEqual(self.item()['item_detail'],'并发正文')

    async def test_pending_resume_stops_after_failed_first_on_sale_item(self):
        self.db.conn.execute("UPDATE item_info SET listing_status='on_sale',last_seen_at=CURRENT_TIMESTAMP WHERE cookie_id='fixture-shop'")
        self.db.conn.commit()
        item_details.credentials_changed(self.db,'fixture-shop','cookie2=old','cookie2=new')
        live=self.live();live.get_item_info.return_value={'error_code':'security_verification_required'}
        await live._resume_pending_item_details()
        await live._resume_pending_item_details()
        live.get_item_info.assert_awaited_once()
        self.assertFalse(self.item()['detail_can_refresh'])

    async def test_list_challenge_response_does_not_save_cookies(self):
        from tests.test_item_sync_without_group import _FakeResponse
        from multidict import CIMultiDict
        from types import SimpleNamespace
        live=self.live();live.cookies_str='unb=fixture; _m_h5_tk=old_1; _m_h5_tk_enc=enc';live.cookies={'unb':'fixture'};live.myid='fixture'
        self.db.update_cookie_account_info('fixture-shop',cookie_value=live.cookies_str)
        before=self.db.get_cookie('fixture-shop')
        response=_FakeResponse({'ret':['FAIL_SYS_USER_VALIDATE::validation']})
        response.headers=CIMultiDict({'set-cookie':'cookie2=must-not-save; Path=/'})
        live.session=SimpleNamespace(post=lambda *args,**kwargs:response)
        result=await XianyuLive.get_item_list_info(live)
        self.assertIn('error',result)
        self.assertEqual(before,self.db.get_cookie('fixture-shop'))
        self.assertEqual(before,live.cookies_str)

    async def test_missing_signing_pair_stops_before_network(self):
        self.db.update_cookie_account_info('fixture-shop',cookie_value='unb=fixture; _m_h5_tk=old_1')
        live=self.live();live.cookies_str=self.db.get_cookie('fixture-shop')
        with patch('aiohttp.ClientSession') as factory:
            result=await XianyuLive.get_item_info(live,'fixture-item')
        factory.assert_not_called()
        self.assertEqual(result['error_code'],'credentials_missing')

    async def test_new_account_explicit_credentials_and_whitespace_are_safe(self):
        self.assertTrue(item_details.credentials_changed(self.db,'fixture-shop',None,'cookie2=new'))
        self.assertEqual(item_details.generation(self.db,'fixture-shop'),1)
        self.assertFalse(item_details.credentials_changed(self.db,'fixture-shop','cookie2=new','  '))
        self.assertFalse(item_details.credentials_changed(self.db,'fixture-shop','cookie2=new; unb=fixture','unb=fixture; cookie2=new'))

    async def test_malformed_redirect_is_safe_and_batch_reason_visible(self):
        self.assertEqual(item_details.classify(302,None,'https://[broken/login')[0],'invalid_response')
        self.db.save_item_basic_info('fixture-shop','next',item_title='next')
        item_details.failure(self.db,'fixture-shop','fixture-item','security_verification_required')
        item=self.db.get_item_info('fixture-shop','next')
        self.assertFalse(item['detail_can_refresh'])
        self.assertIn('安全验证',item['detail_failure_message'])
        self.assertIn('sync_web_session',item['detail_actions'])

    async def test_diagnostics_backup_scope_and_account_cleanup(self):
        from app.services.user_backup import export_user,delete_account_data
        item_details.credentials_changed(self.db,'fixture-shop',None,'cookie2=new')
        identity=item_details.record_diagnostic(self.db,'fixture-shop','fixture-item',1,'access_denied')
        exported=export_user(self.db.conn,self.owner)
        self.assertNotIn('item_detail_diagnostics',exported['data'])
        self.assertNotIn('item_detail_accounts',exported['data'])
        with self.db.conn:
            delete_account_data(self.db.conn,'fixture-shop')
        self.assertIsNone(item_details.diagnostic(self.db,identity,'fixture-shop'))
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM item_detail_accounts WHERE cookie_id=?',('fixture-shop',)).fetchone()[0],0)
        self.assertTrue(self.db.create_user('other-owner','fixture-owner@example.invalid','FixturePass123'))
        another=self.db.get_user_by_username('other-owner')['id']
        self.db.save_cookie('fixture-shop','cookie2=readded',another)
        self.assertIsNone(item_details.diagnostic(self.db,identity,'fixture-shop'))
        self.assertEqual(item_details.account(self.db,'fixture-shop')[3],0)
        self.assertNotIn('item_detail_diagnostics',export_user(self.db.conn,another)['data'])

    async def test_shared_session_headers_follow_only_successful_cookie_cas(self):
        from tests.test_item_sync_without_group import _FakeResponse
        from multidict import CIMultiDict
        from types import SimpleNamespace
        value='unb=fixture; _m_h5_tk=old_1; _m_h5_tk_enc=enc; cookie2=session'
        self.db.update_cookie_account_info('fixture-shop',cookie_value=value)
        live=self.live();live.cookies_str=value
        live.session=SimpleNamespace(headers={'cookie':value},closed=False)
        response=_FakeResponse({'ret':['SUCCESS::ok'],'data':{'itemDO':{'desc':'正文'}}})
        response.headers=CIMultiDict({'set-cookie':'_m_h5_tk=new_2; Path=/'})
        factory=SimpleNamespace(post=lambda *args,**kwargs:response)
        with patch('aiohttp.ClientSession',return_value=fake_detail_session(factory)):
            result=await XianyuLive.get_item_info(live,'fixture-item')
        self.assertNotIn('error_code',result)
        self.assertEqual(live.session.headers['cookie'],self.db.get_cookie('fixture-shop'))
        before=live.session.headers['cookie']
        with patch('aiohttp.ClientSession',return_value=fake_detail_session(factory)),patch('app.services.account_auth_client.save_cookie_delta',return_value=None):
            result=await XianyuLive.get_item_info(live,'fixture-item')
        self.assertEqual(result['error_code'],'interrupted')
        self.assertEqual(live.session.headers['cookie'],before)

    async def test_browser_diagnostic_records_real_stage_and_elapsed(self):
        live=self.live()
        async def delayed_failure(_):
            await asyncio.sleep(.01)
            raise item_details.DetailFetchError('description_not_found')
        live._fetch_item_detail_from_browser.side_effect=delayed_failure
        await live.fetch_item_detail_from_api('fixture-item')
        result=item_details.diagnostic(self.db,self.item()['detail_diagnostic_id'],'fixture-shop')
        self.assertEqual(result['stage'],'browser')
        self.assertGreaterEqual(result['elapsed_ms'],10)

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
