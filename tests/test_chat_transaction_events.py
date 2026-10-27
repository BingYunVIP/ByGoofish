"""Transaction card evidence, participant names and display-only order association."""
import json
from datetime import datetime, timezone
from unittest import TestCase
from app.chat_events import resolve_direction, transaction_event, valid_nickname
from app.xianyu_im import parse_conversation, parse_message
from app.services import chat_workspace as store
from app.db_manager import DBManager
from test_chat_workspace import Fixture


def stamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)


def model(text, *, sender='fixture-buyer', receiver='fixture-shop', at='2026-10-26 11:18:00', decoded=None, title=None):
    return {'message': {'messageId': 'fixture-message', 'createAt': stamp(at),
            'extension': {'senderUserId': sender + '@goofish', 'receiver': receiver,
                          'reminderTitle': title or text, 'reminderContent': text},
            'content': {'custom': {'data': json.dumps(decoded or {'contentType': 1, 'text': {'text': text}}), 'summary': text}}}}


class EventParserTests(TestCase):
    def test_inventory_and_unknown_messages(self):
        phrases = {
            '[我已拍下，待付款]': 'order_pending_payment',
            '[我已付款，等待你发货]': 'order_paid_pending_ship',
            '[你已发货]': 'seller_shipped', '[卖家已发货]': 'seller_shipped',
            '[买家确认收货，交易成功]': 'buyer_received',
            '[你已确认收货，交易成功]': 'buyer_received',
            '快给Ta一个评价吧～': 'review_requested', '我完成了评价': 'review_completed',
            '[我发起了退款申请]': 'refund_requested',
            '[退款成功，钱款已原路退返]': 'refund_completed',
            '已求买家送我闲鱼小红花': 'flower_sent_confirmation',
            '可以送Ta闲鱼小红花吗～': 'flower_requested',
            '你人真不错，送你闲鱼小红花': 'flower_received',
        }
        for text, kind in phrases.items():
            with self.subTest(text=text):
                parsed = parse_message(model(text), 'fixture-shop')
                self.assertEqual(parsed['event_type'], kind)
                self.assertEqual(parsed['senderName'], '')
                self.assertFalse(valid_nickname(text))
        for text in ('[流泪]', '[图片]', '为什么你已发货还没收到？', '评价很好，谢谢', '[新的未知状态]'):
            self.assertIsNone(transaction_event(text), text)

    def test_buyer_and_seller_views_and_recipient_perspective(self):
        cases = [
            ('[我已付款，等待你发货]', 'fixture-buyer', 'fixture-shop', 'inbound', 'outbound'),
            ('[你已发货]', 'fixture-buyer', 'fixture-shop', 'outbound', 'inbound'),
            ('[卖家已发货]', 'fixture-shop', 'fixture-buyer', 'outbound', 'inbound'),
            ('[你已确认收货，交易成功]', 'fixture-shop', 'fixture-buyer', 'inbound', 'outbound'),
            ('已求买家送我闲鱼小红花', 'fixture-shop', 'fixture-shop', 'outbound', 'inbound'),
            ('[我完成了评价]', 'fixture-shop', 'fixture-shop', 'outbound', 'inbound'),
        ]
        for text, sender, receiver, seller_view, buyer_view in cases:
            with self.subTest(text=text):
                raw = model(text, sender=sender, receiver=receiver)
                seller_message = parse_message(raw, 'fixture-shop')
                buyer_message = parse_message(raw, 'fixture-buyer')
                resolve_direction(seller_message, 'fixture-shop', 'fixture-buyer', 'fixture-shop', 'fixture-buyer')
                resolve_direction(buyer_message, 'fixture-buyer', 'fixture-shop', 'fixture-shop', 'fixture-buyer')
                self.assertEqual(seller_message['event_direction'], seller_view)
                self.assertEqual(buyer_message['event_direction'], buyer_view)
                self.assertEqual(seller_message['senderId'], sender)
        unknown = parse_message(model('[你已发货]', sender='0', receiver=''), 'fixture-shop')
        self.assertEqual(unknown['event_direction'], 'unknown')
        reminder = parse_message(model('快给ta一个评价吧～'), 'fixture-shop')
        self.assertEqual(reminder['event_from'], '闲鱼')

    def test_nested_jump_order_ids_take_precedence_over_item_id(self):
        oid = '9000000000000000001'
        for key, url in (
            ('targetUrl', 'fleamarket://order_detail?id=' + oid),
            ('jumpUrl', 'https://www.goofish.com/order-detail?orderId=' + oid),
            ('targetUrl', 'fleamarket://order?bizOrderId=' + oid),
        ):
            decoded = {'itemId': 'fixture-item', 'dxCard': {'item': {'main': {'exContent': {
                'title': '快给ta一个评价吧～', 'button': {'intent': {'page': {key: url}}}
            }}}}}
            parsed = parse_message(model('快给ta一个评价吧～', decoded=decoded), 'fixture-shop')
            self.assertEqual(parsed['event_order_id'], oid)
            self.assertEqual(parsed['card'], {'kind': 'order', 'orderId': oid})
        item_url = {'title': '商品', 'targetUrl': 'https://www.goofish.com/item?id=' + oid}
        self.assertEqual(parse_message(model('[卡片消息]', decoded=item_url), 'fixture-shop')['event_order_id'], '')

    def test_self_confirmation_card_does_not_turn_into_its_buyer_facing_request(self):
        decoded = {'dxCard': {'item': {'main': {'exContent': {'title': '可以送我闲鱼小红花吗～'}}}}}
        raw = model('已求买家送我闲鱼小红花', sender='fixture-shop', receiver='fixture-shop', decoded=decoded)
        parsed = parse_message(raw, 'fixture-shop')
        self.assertEqual(parsed['event_type'], 'flower_sent_confirmation')
        self.assertEqual(parsed['text'], '已求买家送我闲鱼小红花')
        self.assertEqual(parsed['event_direction'], 'unknown')

    def test_event_titles_never_override_real_sender_nick(self):
        raw = {'singleChatConversation': {'cid': 'fixture-chat@goofish', 'pairFirst': 'fixture-shop@goofish', 'pairSecond': 'fixture-buyer@goofish'},
               'lastMessage': model('快给ta一个评价吧～'), 'modifyTime': 1}
        self.assertEqual(parse_conversation(raw, 'fixture-shop')['otherUserName'], '')
        raw['lastMessage']['message']['extension']['senderNick'] = '真实昵称'
        self.assertEqual(parse_conversation(raw, 'fixture-shop')['otherUserName'], '真实昵称')
        raw['lastMessage'] = model('你好', title='旧标题')
        raw['lastMessage']['message']['extension']['senderNick'] = '明确昵称'
        self.assertEqual(parse_conversation(raw, 'fixture-shop')['otherUserName'], '明确昵称')



class EventAssociationTests(Fixture, TestCase):
    def setUp(self):
        self.setup_db()

    def tearDown(self):
        self.close_db()

    def order(self, oid, created='2026-10-26 11:16:25', **changes):
        fields = dict(cookie_id='fixture-shop', buyer_id='fixture-buyer', item_id='fixture-item',
                      chat_id='fixture-chat', order_status='completed', buyer_nick='有效昵称', created_at=created)
        fields.update(changes)
        self.db.insert_or_update_order(oid, **fields)

    def resolve(self, text='快给ta一个评价吧～', at='2026-10-26 11:18:00', oid='', **kwargs):
        message = parse_message(model(text, at=at, **kwargs), 'fixture-shop')
        if oid: message['event_order_id'] = oid
        store.associate_transaction_events(self.db, 'fixture-shop', 'fixture-chat', [message], self.uid, 'fixture-shop')
        return message

    def test_old_event_keeps_old_order_and_future_order_is_excluded_using_utc(self):
        self.order('old', created='2026-10-20 12:00:00')
        self.order('latest')
        self.order('future', created='2026-10-26 11:20:00')
        self.assertEqual(self.resolve(at='2026-10-20 12:03:00')['event_order_id'], 'old')
        self.assertEqual(self.resolve()['event_order_id'], 'latest')
        self.assertEqual(self.resolve(at='2026-10-26 11:16:24')['event_order_id'], 'old')

    def test_new_same_item_order_without_chat_id_is_not_hidden_by_an_old_chat_match(self):
        self.order('old', created='2026-10-20 12:00:00')
        self.order('latest', chat_id='')
        self.assertEqual(self.resolve()['event_order_id'], 'latest')

    def test_explicit_platform_id_wins_over_current_status_and_newer_orders(self):
        self.order('old', created='2026-10-20 12:00:00', order_status='cancelled')
        self.order('new')
        result = self.resolve(oid='old')
        self.assertEqual(result['event_order_id'], 'old')
        self.assertEqual(result['event_order_match'], 'platform_card')

    def test_wrong_status_item_chat_and_account_are_not_auto_associated(self):
        self.order('wrong-status', order_status='processing')
        self.order('wrong-item', item_id='another-item')
        self.order('wrong-chat', chat_id='another-chat')
        self.order('wrong-account', cookie_id='fixture-shop-b')
        self.assertEqual(self.resolve()['event_order_id'], '')
        self.assertEqual(self.resolve(oid='wrong-account')['event_order_id'], '')
        before = self.db.conn.total_changes
        self.resolve()
        self.assertEqual(self.db.conn.total_changes, before)

    def test_equal_creation_times_remain_candidates_and_missing_time_does_not_guess(self):
        self.order('a'); self.order('b')
        result = self.resolve()
        self.assertEqual(result['event_order_id'], '')
        self.assertEqual(set(result['event_order_candidates']), {'a', 'b'})
        message = parse_message(model('快给ta一个评价吧～'), 'fixture-shop');message['time'] = 0
        store.associate_transaction_events(self.db, 'fixture-shop', 'fixture-chat', [message])
        self.assertEqual(message['event_order_id'], '')

    def test_interaction_receipt_identifies_delayed_event_for_older_order(self):
        self.order('old', created='2026-10-20 12:00:00'); self.order('new')
        event_time = stamp('2026-10-26 11:18:00') / 1000
        self.db.conn.execute('INSERT INTO buyer_interaction_attempts VALUES(?,?,?,?,?,?)',
                             ('fixture-shop','old','thanks','sent',event_time-1,event_time-1));self.db.conn.commit()
        result = self.resolve('亲，感谢支持！有任何问题随时找我~', sender='fixture-shop', receiver='fixture-buyer')
        self.assertEqual(result['event_order_id'], 'old')
        self.assertEqual(result['event_order_match'], 'interaction_receipt')

    def test_business_direction_corrects_stale_payment_sender_and_unknown_actor_is_neutral(self):
        self.order('order')
        result = self.resolve('[你已发货]')
        self.assertEqual(result['event_direction'], 'outbound')
        self.assertEqual(result['event_from'], '我方')
        self.assertEqual(result['event_to'], '买家')
        self.assertEqual(result['senderId'], 'fixture-buyer')
        confirmed = parse_message(model('已求买家送我闲鱼小红花', sender='fixture-shop', receiver='fixture-shop'), 'fixture-shop')
        store.associate_transaction_events(self.db, 'fixture-shop', 'fixture-chat', [confirmed], self.uid, 'fixture-shop')
        self.assertEqual(confirmed['event_direction'], 'outbound')
        message = parse_message(model('[我完成了评价]', sender='stranger'), 'fixture-shop')
        store.associate_transaction_events(self.db, 'fixture-shop', 'fixture-chat', [message])
        self.assertEqual(message['event_direction'], 'unknown')

    def test_cross_user_cannot_use_cached_order_context(self):
        self.order('order')
        message = parse_message(model('快给ta一个评价吧～'), 'fixture-shop')
        store.associate_transaction_events(self.db, 'fixture-shop', 'fixture-chat', [message], self.uid+1)
        self.assertEqual(message['event_order_id'], '')

    def test_buyer_view_can_associate_order_stored_on_owned_seller_account(self):
        store.observe_conversations(self.db, self.uid, 'fixture-shop-b', [{
            'cid': 'buyer-view-chat', 'otherUserId': 'fixture-shop', 'otherUserName': '卖家昵称',
            'itemId': 'fixture-item', 'lastMessageTime': 0, 'lastMessageSummary': '',
            'unreadCount': 0,
        }])
        self.db.insert_or_update_order('seller-owned-order', cookie_id='fixture-shop',
            buyer_id='fixture-shop-b', buyer_nick='本账号买家昵称', item_id='fixture-item',
            chat_id='buyer-view-chat', order_status='completed', created_at='2026-10-26 11:16:25')
        message = parse_message(model('[买家确认收货，交易成功]', sender='fixture-shop-b',
            receiver='fixture-shop', at='2026-10-26 11:17:00'), 'fixture-shop-b')
        store.associate_transaction_events(self.db, 'fixture-shop-b', 'buyer-view-chat',
            [message], self.uid, 'fixture-shop-b')
        self.assertEqual(message['event_order_id'], 'seller-owned-order')
        self.assertEqual(message['event_direction'], 'outbound')
        self.assertEqual(message['event_from'], '我方')
        self.assertEqual(message['event_to'], '卖家')

    def test_legacy_names_repaired_on_restart_without_touching_notes_orders_or_valid_names(self):
        self.order('old', created='2026-10-20 12:00:00', buyer_nick='旧订单昵称')
        self.order('new', buyer_nick='新订单昵称')
        snapshot = {**self.conversation, 'otherUserName': '你人真不错，送你闲鱼小红花'}
        self.db.conn.execute('UPDATE chat_workspace_state SET snapshot=?', (json.dumps(snapshot),))
        store.update_state(self.db, self.uid, 'fixture-shop', 'fixture-chat', {'note':'保留备注','pinned':True})
        store.finish_buyer_profile_fetch(self.db,self.uid,'fixture-shop','fixture-buyer','缓存昵称')
        self.db.conn.execute("UPDATE chat_buyer_profiles SET nickname='我完成了评价'");self.db.conn.commit()
        self.db.close();self.db=DBManager(self.path)
        state = store.state(self.db,self.uid,'fixture-shop','fixture-chat')
        self.assertEqual(state['snapshot']['otherUserName'], '')
        self.assertEqual(state['note'], '保留备注');self.assertTrue(state['pinned'])
        self.assertEqual(store.buyer_profile(self.db,self.uid,'fixture-shop','fixture-buyer')['nickname'], '')
        incoming = {**self.conversation, 'otherUserName':'快给ta一个评价吧～'}
        store.observe_conversations(self.db,self.uid,'fixture-shop',[incoming])
        self.assertEqual(incoming['otherUserName'],'新订单昵称')
        self.assertEqual(self.db.get_order_by_id('old')['buyer_nick'],'旧订单昵称')
        incoming['otherUserName']='最新明确昵称'
        store.observe_conversations(self.db,self.uid,'fixture-shop',[incoming])
        store.repair_buyer_nickname(self.db,self.uid,'fixture-shop','fixture-chat','fixture-buyer','较早查询结果')
        self.assertEqual(store.state(self.db,self.uid,'fixture-shop','fixture-chat')['snapshot']['otherUserName'],'最新明确昵称')
