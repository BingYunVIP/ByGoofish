"""Local-only detail status, permission boundary and controlled operation endpoints."""
import unittest
from unittest.mock import patch, AsyncMock
from tests import test_item_details as fixtures
from app import reply_server
from app.auth import get_current_user
from app.services import item_details

class DetailRouteTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ItemDetailTests.asyncSetUp
    asyncTearDown = fixtures.ItemDetailTests.asyncTearDown
    item = fixtures.ItemDetailTests.item

    async def test_refresh_and_diagnostic_reject_other_owner_and_absent_item(self):
        reply_server.app.dependency_overrides[get_current_user] = lambda: {'user_id': self.owner+100}
        with patch.object(reply_server, '_run_on_account_loop', new_callable=AsyncMock) as run:
            for path in ('refresh-detail','detail-diagnostic'):
                req=self.client.post if path=='refresh-detail' else self.client.get
                self.assertEqual((await req('/items/fixture-shop/fixture-item/'+path)).status_code,403)
            run.assert_not_awaited()
        reply_server.app.dependency_overrides[get_current_user] = lambda: {'user_id': self.owner}
        self.assertEqual((await self.client.post('/items/fixture-shop/not-present/refresh-detail')).status_code,404)

    async def test_status_and_diagnostic_reads_make_no_platform_request(self):
        with patch.object(reply_server, '_run_on_account_loop', new_callable=AsyncMock) as run:
            for path in ('/items','/items/fixture-shop/fixture-item','/items/fixture-shop/fixture-item/detail-diagnostic'):
                result=await self.client.get(path)
                self.assertEqual(result.status_code,200)
            run.assert_not_awaited()
        self.assertIsNone((await self.client.get('/items/fixture-shop/fixture-item/detail-diagnostic')).json()['diagnostic'])

    async def test_refresh_only_dispatches_eligible_owned_item(self):
        item=dict(self.item(),detail_can_refresh=True)
        with patch.object(self.db,'get_item_info',return_value=item), patch.object(reply_server,'_run_on_account_loop',new_callable=AsyncMock) as run:
            run.return_value={'success':True,'requested':True,'item':item}
            response=await self.client.post('/items/fixture-shop/fixture-item/refresh-detail')
            self.assertEqual(response.status_code,200)
            run.assert_awaited_once()
            self.assertEqual(run.call_args.kwargs['timeout'],90)
            instance=type('Instance',(),{})()
            instance.refresh_item_detail=AsyncMock(return_value={'success':True})
            self.assertEqual(await run.call_args.args[1](instance),{'success':True})
            instance.refresh_item_detail.assert_awaited_once_with('fixture-item')
        item['detail_can_refresh']=False
        with patch.object(self.db,'get_item_info',return_value=item),patch.object(reply_server,'_run_on_account_loop',new_callable=AsyncMock) as run:
            response=await self.client.post('/items/fixture-shop/fixture-item/refresh-detail')
            self.assertFalse(response.json()['requested']);run.assert_not_awaited()

    async def test_diagnostic_cannot_bind_other_item(self):
        item=dict(self.item(),detail_diagnostic_id='fixture-diagnostic')
        with patch.object(self.db,'get_item_info',return_value=item),patch.object(item_details,'diagnostic',return_value={'item_id':'other-product'}):
            self.assertIsNone((await self.client.get('/items/fixture-shop/fixture-item/detail-diagnostic')).json()['diagnostic'])
