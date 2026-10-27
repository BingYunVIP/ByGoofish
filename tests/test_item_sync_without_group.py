"""Only a reliably identified on-sale group may supply inventory-list records."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from XianyuAutoAsync import XianyuLive

COOKIES = 'unb=123456; _m_h5_tk=abc_1700000000000; _m_h5_tk_enc=x; cookie2=y'


def make_card(item_id, title='商品', price='9.9'):
    """构造一个与真实响应同构的最小 cardList 元素。"""
    return {
        'cardType': 1000,
        'cardData': {
            'auctionType': 'b',
            'categoryId': 50023914,
            'itemId': item_id,
            'title': title,
            'priceInfo': {'price': price},
            'detailParams': {'itemId': item_id, 'title': title},
        },
    }


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status = 200
        self.headers = {}
        self.cookies = {}

    async def json(self, *args, **kwargs):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    """按调用顺序回放预设响应，并记下每次请求的 data。"""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.requests = []

    async def close(self):
        return None

    def post(self, url, params=None, data=None, **kwargs):
        self.requests.append(json.loads(data['data']) if data else None)
        payload = self._payloads.pop(0) if self._payloads else {'ret': ['SUCCESS::调用成功'], 'data': {}}
        return _FakeResponse(payload)


def build_live(payloads):
    live = XianyuLive(cookies_str=COOKIES, cookie_id='acc', user_id=1)
    live.session = _FakeSession(payloads)

    # 入库会顺带用浏览器逐个抓商品详情。这里只验证同步的判定逻辑，
    # 打桩掉它，免得测试真去开 Chromium（实测会拖到 30 秒以上）。
    live.save_items_list_to_db = AsyncMock(side_effect=lambda items: len(items))
    return live


def run(coro):
    return asyncio.run(coro)


class ScopedItemSyncTests(unittest.TestCase):
    def test_unscoped_responses_are_not_imported_or_treated_as_empty(self):
        for groups in (None, [], [{'groupName':'综合','groupId':7,'itemNumber':8}],
                       [{'groupName':'已售出','groupId':8,'itemNumber':8}],
                       [{'groupName':'在售','itemNumber':8}]):
            for cards in ([], [make_card('unverified')]):
                with self.subTest(groups=groups,cards=bool(cards)):
                    payload={'ret':['SUCCESS::调用成功'],'data':{'itemGroupList':groups,'cardList':cards,'totalCount':0}}
                    live=build_live([payload]);session=live.session
                    try:
                        result=run(live.get_item_list_info())
                        self.assertFalse(result['success'])
                        self.assertEqual(result['error_code'],'on_sale_group_unavailable')
                        self.assertEqual(result['listing_scope'],'unconfirmed')
                        live.save_items_list_to_db.assert_not_awaited()
                        self.assertEqual(len(session.requests),1)
                    finally:run(live.close_session())

    def test_requests_only_on_sale_group_when_sold_and_comprehensive_exist(self):
        discovery={'ret':['SUCCESS::调用成功'],'data':{'itemGroupList':[
            {'groupName':'综合','groupId':10,'itemNumber':5},
            {'groupName':'已售出','groupId':30,'itemNumber':4},
            {'groupName':'在售','groupId':20,'itemNumber':1}],
            'cardList':[make_card('unscoped')]}}
        page={'ret':['SUCCESS::调用成功'],'data':{'cardList':[make_card('live')],'totalCount':0,'nextPage':False}}
        live=build_live([discovery,page]);session=live.session
        try:
            result=run(live.get_item_list_info())
            self.assertTrue(result['success'])
            self.assertEqual([x['id'] for x in result['items']],['live'])
            self.assertEqual(result['items'][0]['listing_status'],'on_sale')
            self.assertEqual(session.requests[1]['groupId'],'20')
            self.assertEqual(session.requests[1]['groupName'],'在售')
            self.assertTrue(result['count_reconciled'])
            self.assertFalse(result['confirmed_empty'])
        finally:run(live.close_session())

    def test_on_sale_empty_is_confirmed_even_when_sold_group_has_items(self):
        live=build_live([
            {'ret':['SUCCESS::调用成功'],'data':{'itemGroupList':[
                {'groupName':'在售','groupId':1,'itemNumber':0},
                {'groupName':'已售出','groupId':2,'itemNumber':9}]}},
            {'ret':['SUCCESS::调用成功'],'data':{'cardList':[],'totalCount':0,'nextPage':False}},
        ])
        try:
            result=run(live.get_item_list_info())
            self.assertTrue(result['success'])
            self.assertTrue(result['confirmed_empty'])
            self.assertEqual(result['items'],[])
            live.save_items_list_to_db.assert_not_awaited()
        finally:run(live.close_session())

    def test_known_sold_or_off_shelf_cards_are_excluded_not_product_title(self):
        cards=[make_card('live','卖掉了的同款商品'),make_card('sold'),make_card('removed'),make_card('labelled')]
        cards[1]['cardData']['isSold']=True
        cards[2]['cardData']['itemStatus']='OFF_SHELF'
        cards[3]['cardData']['itemLabelDataVO']={'tagList':[{'data':{'content':'卖掉了'}}]}
        result=XianyuLive._extract_items_from_response({'cardList':cards})
        self.assertEqual([x['id'] for x in result],['live'])
        self.assertIsNone(result[0]['item_status'])

    def test_non_sale_evidence_wins_for_duplicate_cards(self):
        live=make_card('same');sold=make_card('same');sold['cardData']['isSold']=True
        for cards in ([live,sold],[sold,live]):
            self.assertEqual(XianyuLive._extract_items_from_response({'cardList':cards}),[])

    def test_mixed_page_only_saves_confirmed_eligible_cards(self):
        sold=make_card('sold');sold['cardData']['itemStatusText']='已售出'
        live=build_live([
            {'ret':['SUCCESS::调用成功'],'data':{'itemGroupList':[{'groupName':'在售','groupId':1,'itemNumber':1}]}},
            {'ret':['SUCCESS::调用成功'],'data':{'cardList':[make_card('live'),sold],'nextPage':False,'totalCount':1}},
        ])
        try:
            result=run(live.get_item_list_info())
            self.assertTrue(result['success'])
            self.assertEqual([x['id'] for x in live.save_items_list_to_db.await_args.args[0]],['live'])
        finally:run(live.close_session())


if __name__=='__main__':unittest.main()
