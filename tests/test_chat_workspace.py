"""Workspace persistence, ownership, transport receipts and zero-repeat regression."""

import asyncio
import io
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from app.db_manager import DBManager
from app.services import chat_workspace as store
from app.routers.chat_workspace import create_chat_workspace_router, send_once, buyer_identity
from XianyuAutoAsync import AutoReplyPauseManager, XianyuLive


class Fixture:
    def setup_db(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "workspace.db")
        self.db = DBManager(self.path)
        self.uid = self.db.conn.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()[0]
        self.db.save_cookie("fixture-shop", "fixture=value", self.uid)
        self.db.save_cookie("fixture-shop-b", "fixture=value", self.uid)
        self.conversation = {
            "cid": "fixture-chat",
            "otherUserId": "fixture-buyer",
            "otherUserName": "示例昵称",
            "itemId": "fixture-item",
            "lastMessageTime": 10000,
            "lastMessageSummary": "你好",
            "unreadCount": 2,
            "lastMessageSenderId": "fixture-buyer",
            "lastMessageType": "text",
        }
        store.observe_conversations(
            self.db, self.uid, "fixture-shop", [dict(self.conversation)]
        )
        self.instance = SimpleNamespace(
            cookies_str="fixture=value",
            send_im_text=AsyncMock(
                return_value={"code": 200, "body": {"messageId": "fixture-sent"}}
            ),
            send_im_image=AsyncMock(
                return_value={"code": 200, "body": {"messageId": "fixture-image"}}
            ),
        )

        async def run(cid, op):
            return await op(self.instance)

        self.run = run
        self.patches = [
            patch("XianyuAutoAsync.pause_manager.paused_chats", {}),
            patch("app.db_manager.db_manager", self.db),
            patch("app.services.auth_recovery.auth_paused", return_value=False),
            patch(
                "utils.risk_control.registry.get",
                return_value=SimpleNamespace(is_blocked=False),
            ),
        ]
        for p in self.patches:
            p.start()

    def close_db(self):
        for p in reversed(self.patches):
            p.stop()
        self.db.close()
        self.temp.cleanup()


class StateTests(Fixture, TestCase):
    def setUp(self):
        self.setup_db()

    def tearDown(self):
        self.close_db()

    def test_notes_pin_and_read_survive_restart_without_leaking_accounts(self):
        store.update_state(
            self.db,
            self.uid,
            "fixture-shop",
            "fixture-chat",
            {
                "pinned": True,
                "note": "测试备注",
                "viewed_message_time": 10000,
                "viewed_message_summary": "你好",
                "viewed_unread_count": 2,
            },
        )
        self.db.close()
        self.db = DBManager(self.path)
        s = store.state(self.db, self.uid, "fixture-shop", "fixture-chat")
        self.assertTrue(s["pinned"])
        self.assertEqual(s["note"], "测试备注")
        self.assertEqual(s["viewed_message_time"], 10000)
        self.assertIsNone(
            store.state(self.db, self.uid, "fixture-shop-b", "fixture-chat")
        )
        self.assertIsNone(
            store.state(self.db, self.uid + 1, "fixture-shop", "fixture-chat")
        )

    def test_only_actual_participant_messages_change_pending_reply(self):
        self.assertTrue(
            store.state(self.db, self.uid, "fixture-shop", "fixture-chat")[
                "needs_reply"
            ]
        )
        store.observe_messages(
            self.db,
            self.uid,
            "fixture-shop",
            "fixture-chat",
            [
                {"type": "text", "senderId": "0", "isSelf": False, "time": 90000},
                {
                    "type": "system",
                    "senderId": "fixture-buyer",
                    "isSelf": False,
                    "time": 99999,
                },
            ],
        )
        store.observe_messages(
            self.db,
            self.uid,
            "fixture-shop",
            "fixture-chat",
            [
                {
                    "type": "text",
                    "senderId": "fixture-shop",
                    "isSelf": True,
                    "time": 11000,
                }
            ],
        )
        self.assertFalse(
            store.state(self.db, self.uid, "fixture-shop", "fixture-chat")[
                "needs_reply"
            ]
        )
        store.observe_messages(
            self.db,
            self.uid,
            "fixture-shop",
            "fixture-chat",
            [
                {
                    "type": "image",
                    "senderId": "fixture-buyer",
                    "isSelf": False,
                    "time": 12000,
                }
            ],
        )
        self.assertTrue(
            store.state(self.db, self.uid, "fixture-shop", "fixture-chat")[
                "needs_reply"
            ]
        )

    def test_read_watermark_does_not_move_backwards(self):
        store.update_state(
            self.db,
            self.uid,
            "fixture-shop",
            "fixture-chat",
            {"viewed_message_time": 500},
        )
        store.update_state(
            self.db,
            self.uid,
            "fixture-shop",
            "fixture-chat",
            {"viewed_message_time": 100},
        )
        self.assertEqual(
            store.state(self.db, self.uid, "fixture-shop", "fixture-chat")[
                "viewed_message_time"
            ],
            500,
        )

    def test_handover_survives_manager_restart_and_resume_is_account_scoped(self):
        one = AutoReplyPauseManager()
        one.pause_chat("fixture-chat", "fixture-shop")
        two = AutoReplyPauseManager()
        self.assertTrue(two.is_chat_paused("fixture-chat", "fixture-shop"))
        self.assertFalse(two.is_chat_paused("fixture-chat", "fixture-shop-b"))
        two.resume_chat("fixture-chat", "fixture-shop")
        self.assertFalse(two.is_chat_paused("fixture-chat", "fixture-shop"))

    def test_expiry_and_zero_duration_respected(self):
        store.set_handover(self.db, "fixture-shop", "fixture-chat", time.time() - 1)
        self.assertFalse(store.handover(self.db, "fixture-shop", "fixture-chat"))
        self.db.update_cookie_pause_duration("fixture-shop", 0)
        AutoReplyPauseManager().pause_chat("fixture-chat", "fixture-shop")
        self.assertFalse(store.handover(self.db, "fixture-shop", "fixture-chat"))

    def test_idempotency_does_not_store_body_and_rejects_different_payload(self):
        self.assertTrue(
            store.claim_send(
                self.db,
                self.uid,
                "fixture-shop",
                "fixture-chat",
                "fixture-buyer",
                "request-fixture",
                "text",
                b"private payload",
            )[0]
        )
        self.assertFalse(
            store.claim_send(
                self.db,
                self.uid,
                "fixture-shop",
                "fixture-chat",
                "fixture-buyer",
                "request-fixture",
                "text",
                b"private payload",
            )[0]
        )
        with self.assertRaises(ValueError):
            store.claim_send(
                self.db,
                self.uid,
                "fixture-shop",
                "fixture-chat",
                "fixture-buyer",
                "request-fixture",
                "text",
                b"different",
            )
        self.assertNotIn(
            "private payload",
            str(self.db.conn.execute("SELECT * FROM chat_send_attempts").fetchall()),
        )

    def test_stale_interrupted_send_returns_unknown_after_restart(self):
        store.claim_send(
            self.db,
            self.uid,
            "fixture-shop",
            "fixture-chat",
            "fixture-buyer",
            "request-fixture",
            "text",
            b"fixture",
        )
        self.db.conn.execute(
            "UPDATE chat_send_attempts SET created_at=?", (time.time() - 90,)
        )
        self.db.conn.commit()
        self.db.close()
        self.db = DBManager(self.path)
        claimed, row = store.claim_send(
            self.db,
            self.uid,
            "fixture-shop",
            "fixture-chat",
            "fixture-buyer",
            "request-fixture",
            "text",
            b"fixture",
        )
        self.assertFalse(claimed)
        self.assertEqual(row["status"], "unknown")


class SendTests(Fixture, IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup_db()

    async def asyncTearDown(self):
        self.close_db()

    async def invoke(self, operation=None, request="request-fixture"):
        return await send_once(
            self.db,
            self.uid,
            "fixture-shop",
            "fixture-chat",
            "fixture-buyer",
            request,
            "text",
            b"fixture",
            self.run,
            operation or self.instance.send_im_text,
        )

    async def test_receipt_and_duplicate_send_only_one_frame(self):
        one = await self.invoke()
        two = await self.invoke()
        self.assertEqual(one["data"]["status"], "sent")
        self.assertEqual(two["data"]["status"], "sent")
        self.instance.send_im_text.assert_awaited_once()

    async def test_unknown_never_auto_repeats(self):
        self.instance.send_im_text.side_effect = TimeoutError("fixture")
        self.assertEqual((await self.invoke())["data"]["status"], "unknown")
        await self.invoke()
        self.instance.send_im_text.assert_awaited_once()

    async def test_empty_or_rejected_receipt_cannot_claim_success(self):
        for i, value in enumerate(
            [
                None,
                {},
                {"code": 500, "body": {}},
                {"code": 200, "body": {"reason": "fixture"}},
            ]
        ):
            self.instance.send_im_text.return_value = value
            self.assertEqual(
                (await self.invoke(request=f"request-fixture-{i}"))["data"]["status"],
                "unknown",
            )

    async def test_cancellation_preserves_uncertain_attempt(self):
        self.instance.send_im_text.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.invoke()
        self.assertEqual(
            store.outgoing(self.db, self.uid, "fixture-shop", "fixture-chat")[0][
                "status"
            ],
            "unknown",
        )

    async def test_concurrent_duplicate_has_one_sender(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def op(instance):
            entered.set()
            await release.wait()
            return {"code": 200, "body": {}}

        task = asyncio.create_task(self.invoke(op))
        await entered.wait()
        r = await self.invoke(op)
        self.assertEqual(r["data"]["status"], "sending")
        release.set()
        await task

    async def test_event_stream_is_bounded_and_account_isolated(self):
        hub = store.EventHub()
        with hub.subscribe("fixture-shop") as first, hub.subscribe(
            "fixture-shop-b"
        ) as other:
            for i in range(120):
                hub.publish("fixture-shop", "message", "fixture-chat")
            await asyncio.sleep(0.01)
            self.assertLessEqual(first.qsize(), 100)
            self.assertTrue(other.empty())
            self.assertEqual((await first.get())["type"], "reset")
        self.assertEqual(len(hub.subscribers), 0)

    async def test_image_sender_uses_correlated_request_and_reports_rejection(self):
        live = XianyuLive.__new__(XianyuLive)
        live.myid = "fixture-shop"
        live._send_im_request = AsyncMock(
            return_value={"code": 200, "body": {"messageId": "image-fixture"}}
        )
        await live.send_im_image(
            "fixture-chat",
            "fixture-buyer",
            "https://fixture.example/image.jpg",
            200,
            100,
        )
        self.assertEqual(
            live._send_im_request.await_args.args[0],
            "/r/MessageSend/sendByReceiverScope",
        )
        live._send_im_request.return_value = {"body": {"reason": "fixture"}}
        with self.assertRaises(RuntimeError):
            await live.send_im_image(
                "fixture-chat",
                "fixture-buyer",
                "https://fixture.example/image.jpg",
                200,
                100,
            )


class ApiTests(Fixture, TestCase):
    def setUp(self):
        self.setup_db()

        def user():
            return {"user_id": self.uid}

        def owned(cid, user):
            if cid not in self.db.get_all_cookies(user["user_id"]):
                raise HTTPException(403, "fixture forbidden")

        app = FastAPI()
        app.include_router(create_chat_workspace_router(self.db, user, owned, self.run))
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.close_db()

    def test_context_matches_account_and_chat_before_candidates(self):
        self.db.insert_or_update_order(
            "fixture-order",
            cookie_id="fixture-shop",
            buyer_id="fixture-buyer",
            item_id="fixture-item",
            chat_id="fixture-chat",
            order_status="completed",
            buyer_nick="示例昵称",
            receiver_name="示例收件人",
            receiver_phone="13800000000",
            receiver_address="示例地址",
        )
        self.db.insert_or_update_order(
            "fixture-candidate",
            cookie_id="fixture-shop",
            buyer_id="fixture-buyer",
            item_id="fixture-item",
            chat_id="different-chat",
        )
        self.db.insert_or_update_order(
            "fixture-foreign",
            cookie_id="fixture-shop-b",
            buyer_id="fixture-buyer",
            item_id="fixture-item",
            chat_id="fixture-chat",
        )
        r = self.client.get("/chat/context/fixture-shop/fixture-chat")
        self.assertEqual(r.status_code, 200)
        d = r.json()["data"]
        self.assertEqual([o["order_id"] for o in d["orders"]], ["fixture-order"])
        self.assertEqual(
            [o["order_id"] for o in d["candidate_orders"]], ["fixture-candidate"]
        )
        self.assertEqual(d["orders"][0]["receiver_address"], "示例地址")

    def test_unknown_conversation_and_foreign_account_rejected(self):
        self.assertEqual(
            self.client.get("/chat/context/fixture-shop/not-loaded").status_code, 404
        )
        self.assertEqual(
            self.client.get("/chat/context/foreign/fixture-chat").status_code, 403
        )

    def test_buyer_view_can_read_order_from_owned_seller_account_and_reject_unrelated_order(self):
        store.observe_conversations(self.db,self.uid,"fixture-shop-b",[{
            "cid":"buyer-view-chat","otherUserId":"fixture-shop","otherUserName":"卖家昵称",
            "itemId":"buyer-view-item","lastMessageTime":1,"lastMessageSummary":"交易状态","unreadCount":0,
        }])
        self.db.insert_or_update_order("seller-side-order",cookie_id="fixture-shop",buyer_id="fixture-shop-b",
            buyer_nick="买家昵称",item_id="buyer-view-item",chat_id="buyer-view-chat",order_status="completed",
            receiver_name="当前用户",receiver_phone="13800000000",receiver_address="当前用户地址")
        self.db.insert_or_update_order("unrelated-order",cookie_id="fixture-shop",buyer_id="different-buyer",
            item_id="buyer-view-item",chat_id="buyer-view-chat",order_status="completed")
        response=self.client.get("/chat/context/fixture-shop-b/buyer-view-chat?order_id=seller-side-order")
        self.assertEqual(response.status_code,200)
        order=response.json()["data"]["orders"][0]
        self.assertEqual(order["cookie_id"],"fixture-shop")
        self.assertEqual(order["receiver_name"],"当前用户")
        self.assertEqual(self.client.get("/chat/context/fixture-shop-b/buyer-view-chat?order_id=unrelated-order").status_code,403)

    def test_note_and_handover_apis(self):
        self.assertEqual(
            self.client.patch(
                "/chat/state/fixture-shop/fixture-chat",
                json={"note": "示例备注", "pinned": True},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                "/chat/handover/fixture-shop/fixture-chat", json={"action": "pause"}
            ).status_code,
            200,
        )
        self.assertTrue(store.handover(self.db, "fixture-shop", "fixture-chat"))
        self.client.post(
            "/chat/handover/fixture-shop/fixture-chat", json={"action": "resume"}
        )
        self.assertFalse(store.handover(self.db, "fixture-shop", "fixture-chat"))

    def test_invalid_note_and_image_rejected_without_upload(self):
        self.assertEqual(
            self.client.patch(
                "/chat/state/fixture-shop/fixture-chat", json={"note": "x" * 2001}
            ).status_code,
            422,
        )
        body = {
            "cid": "fixture-chat",
            "to_user_id": "fixture-buyer",
            "client_request_id": "request-fixture",
        }
        self.assertEqual(
            self.client.post(
                "/chat/send-image/fixture-shop",
                data=body,
                files={"image": ("fixture.svg", b"<svg/>", "image/svg+xml")},
            ).status_code,
            415,
        )
        self.assertEqual(
            self.client.post(
                "/chat/send-image/fixture-shop",
                data=body,
                files={"image": ("fixture.png", b"bad", "image/png")},
            ).status_code,
            400,
        )
        body["to_user_id"] = "wrong-buyer"
        self.assertEqual(
            self.client.post(
                "/chat/send-image/fixture-shop",
                data=body,
                files={"image": ("fixture.png", b"bad", "image/png")},
            ).status_code,
            403,
        )

    def test_image_upload_and_send_only_once(self):
        buff = io.BytesIO()
        Image.new("RGB", (20, 10), "white").save(buff, format="PNG")
        raw = buff.getvalue()
        body = {
            "cid": "fixture-chat",
            "to_user_id": "fixture-buyer",
            "client_request_id": "request-fixture",
        }
        uploader = SimpleNamespace(
            upload_image=AsyncMock(return_value="https://fixture.example/image.jpg")
        )
        manager = AsyncMock()
        manager.__aenter__.return_value = uploader
        with patch("utils.image_uploader.ImageUploader", return_value=manager):
            for _ in range(2):
                r = self.client.post(
                    "/chat/send-image/fixture-shop",
                    data=body,
                    files={"image": ("fixture.png", raw, "image/png")},
                )
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["data"]["status"], "sent")
        uploader.upload_image.assert_awaited_once()
        self.instance.send_im_image.assert_awaited_once()


class CacheInvalidationTests(IsolatedAsyncioTestCase):
    async def test_push_invalidates_cached_results_for_only_its_account(self):
        from app.reply_server import _AccountRequestDedup

        cache = _AccountRequestDedup()
        factory = AsyncMock(return_value={"fixture": 1})
        await cache.run("messages|fixture-shop|chat", factory)
        await cache.run("messages|other-shop|chat", factory)
        cache.invalidate_account("fixture-shop")
        factory.return_value = {"fixture": 2}
        self.assertEqual(
            await cache.run("messages|fixture-shop|chat", factory), {"fixture": 2}
        )
        self.assertEqual(
            await cache.run("messages|other-shop|chat", factory), {"fixture": 1}
        )
        self.assertEqual(factory.await_count, 3)

    async def test_push_during_request_cannot_recache_stale_message_history(self):
        from app.reply_server import _AccountRequestDedup

        cache = _AccountRequestDedup()
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                return {"fixture": "old"}
            return {"fixture": "new"}

        pending = asyncio.create_task(cache.run("messages|fixture-shop|chat", factory))
        await entered.wait()
        cache.invalidate_account("fixture-shop")
        release.set()
        self.assertEqual(await pending, {"fixture": "new"})
        self.assertEqual(calls, 2)

    async def test_push_does_not_remove_platform_throttle_backoff(self):
        from app.reply_server import _AccountRequestDedup

        cache = _AccountRequestDedup()
        factory = AsyncMock(return_value={"reason": "429 flow controled"})
        await cache.run("messages|fixture-shop|chat", factory)
        cache.invalidate_account("fixture-shop")
        await cache.run("messages|fixture-shop|chat", factory)
        factory.assert_awaited_once()


class EntryPointTests(Fixture, IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup_db()

    async def asyncTearDown(self):
        self.close_db()

    async def invoke_reply(self, live):
        from app.services.auto_reply import process, claim
        from XianyuAutoAsync import ConnectionState
        self.db.save_default_reply("fixture-shop", True, "示例回复", False, "")
        live.connection_state = ConnectionState.CONNECTED
        msg = {"cid":"fixture-chat", "senderId":"fixture-buyer", "senderName":"示例买家", "text":"询问规格", "itemId":"fixture-item", "time":int(time.time()*1000), "messageId":"fixture-reply"}
        await process(live,self.db,msg,"push",claim(self.db,"fixture-shop",msg))

    async def test_pause_during_keyword_generation_blocks_actual_send(self):
        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = "fixture-shop"

        async def keyword(*args):
            store.set_handover(
                self.db, "fixture-shop", "fixture-chat", time.time() + 600
            )
            return "示例回复"

        live.get_keyword_reply = AsyncMock(side_effect=keyword)
        live._find_reply_keyword = Mock(return_value="fixture")
        live.send_im_text = AsyncMock()
        live.send_im_image = AsyncMock()
        with patch("XianyuAutoAsync.AUTO_REPLY", {"api": {"enabled": False}}):
            await self.invoke_reply(live)
        live.send_im_text.assert_not_awaited()
        live.send_im_image.assert_not_awaited()
        self.assertEqual(
            self.db.get_auto_reply_logs(self.uid)[0]["decision_reason"],
            "chat_paused_before_send",
        )

    async def test_pause_during_default_image_prepare_blocks_image_send(self):
        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = "fixture-shop"
        live.get_keyword_reply = AsyncMock(return_value=None)
        live.get_ai_reply = AsyncMock(return_value=None)
        live.get_default_reply = AsyncMock(
            return_value={
                "image_url": "https://img.alicdn.com/fixture.jpg",
                "text": "示例回复",
            }
        )
        live._is_cdn_url = Mock(return_value=True)

        async def image_size(*args):
            store.set_handover(
                self.db, "fixture-shop", "fixture-chat", time.time() + 600
            )
            return 20, 10

        live._get_image_size_from_url = AsyncMock(side_effect=image_size)
        live.send_im_text = AsyncMock(return_value={"code":200,"body":{"messageId":"fixture-text"}})
        live.send_im_image = AsyncMock()
        with patch("XianyuAutoAsync.AUTO_REPLY", {"api": {"enabled": False}}):
            await self.invoke_reply(live)
        live.send_im_text.assert_awaited_once()
        live.send_im_image.assert_not_awaited()
        self.assertEqual(
            self.db.get_auto_reply_logs(self.uid)[0]["decision_reason"],
            "chat_paused_before_send",
        )

    async def test_manual_rating_shares_automatic_ledger(self):
        from app.reply_server import rate_orders

        self.db.insert_or_update_order(
            "fixture-order", cookie_id="fixture-shop", order_status="completed"
        )
        api = SimpleNamespace(
            create_rate=AsyncMock(
                return_value={
                    "success": True,
                    "success_order_ids": ["fixture-order"],
                    "fail_orders": [],
                }
            ),
            close=AsyncMock(),
        )
        with patch("utils.xianyu_seller_api.XianyuSellerAPI", return_value=api):
            for _ in range(2):
                await rate_orders(
                    "fixture-order", "示例好评", 1, False, {"user_id": self.uid}
                )
        api.create_rate.assert_awaited_once()
        self.assertFalse(
            self.db.claim_buyer_interaction("fixture-shop", "fixture-order", "rate")
        )
        self.assertEqual(
            self.db.conn.execute(
                "SELECT status FROM buyer_interaction_attempts WHERE action='rate'"
            ).fetchone()[0],
            "sent",
        )

    async def test_legacy_text_contract_and_new_idempotent_text_entry(self):
        from app import reply_server

        request = reply_server.ChatSendMessageRequest(
            cid="fixture-chat", to_user_id="fixture-buyer", text="fixture"
        )
        with patch("app.reply_server.db_manager", self.db), patch(
            "app.reply_server._run_on_account_loop", self.run
        ):
            r = await reply_server.send_chat_message(
                "fixture-shop", request, {"user_id": self.uid}
            )
            self.assertTrue(r["success"])
            self.assertIn("messageId", r["data"])
            request.client_request_id = "request-fixture"
            for _ in range(2):
                await reply_server.send_chat_message(
                    "fixture-shop", request, {"user_id": self.uid}
                )
        self.assertEqual(self.instance.send_im_text.await_count, 2)

    async def test_known_attempt_remains_queryable_when_account_becomes_paused(self):
        r = await send_once(
            self.db,
            self.uid,
            "fixture-shop",
            "fixture-chat",
            "fixture-buyer",
            "request-fixture",
            "text",
            b"fixture",
            self.run,
            self.instance.send_im_text,
        )
        with patch("app.services.auth_recovery.auth_paused", return_value=True):
            r = await send_once(
                self.db,
                self.uid,
                "fixture-shop",
                "fixture-chat",
                "fixture-buyer",
                "request-fixture",
                "text",
                b"fixture",
                self.run,
                self.instance.send_im_text,
            )
        self.assertEqual(r["data"]["status"], "sent")
        self.instance.send_im_text.assert_awaited_once()

    async def test_preflight_rejection_creates_no_uncertain_send(self):
        with patch("app.services.auth_recovery.auth_paused", return_value=True):
            with self.assertRaises(HTTPException):
                await send_once(
                    self.db,
                    self.uid,
                    "fixture-shop",
                    "fixture-chat",
                    "fixture-buyer",
                    "request-fixture",
                    "text",
                    b"fixture",
                    self.run,
                    self.instance.send_im_text,
                )
        self.assertEqual(
            store.outgoing(self.db, self.uid, "fixture-shop", "fixture-chat"), []
        )


class NamePersistenceTests(Fixture, TestCase):
    def setUp(self):
        self.setup_db()

    def tearDown(self):
        self.close_db()

    def test_outgoing_last_message_does_not_erase_buyer_name(self):
        incoming = dict(
            self.conversation, otherUserName="", lastMessageSenderId="fixture-shop"
        )
        store.observe_conversations(self.db, self.uid, "fixture-shop", [incoming])
        self.assertEqual(incoming["otherUserName"], "示例昵称")

    def test_history_recovers_only_confirmed_participant_name(self):
        with self.db.lock:
            self.db.conn.execute("UPDATE chat_workspace_state SET snapshot=?", (json.dumps({
                **self.conversation, "otherUserName": ""
            }),))
            self.db.conn.commit()
        store.observe_messages(self.db, self.uid, "fixture-shop", "fixture-chat", [
            {"type": "text", "senderId": "stranger", "senderName": "错误昵称", "time": 10001},
            {"type": "text", "senderId": "fixture-shop", "senderName": "店铺", "isSelf": True, "time": 10002},
            {"type": "text", "senderId": "fixture-buyer", "senderName": "历史买家昵称", "time": 10003},
        ])
        self.assertEqual(store.state(self.db, self.uid, "fixture-shop", "fixture-chat")["snapshot"]["otherUserName"], "历史买家昵称")


class BuyerProfileTests(Fixture, IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_db()

    def tearDown(self):
        self.close_db()

    async def identity(self, snapshot=None, account="fixture-shop"):
        return await buyer_identity(self.db, self.uid, account, "fixture-buyer", snapshot or {}, self.run, "fixture-chat", "fixture-item")

    def profile_api(self, nickname="公开买家昵称"):
        api = AsyncMock()
        api.cookies_str = self.instance.cookies_str
        api.get_user_profile.return_value = {"module": {"base": {"displayName": nickname}}}
        api.__aenter__.return_value = api
        return api

    async def test_existing_conversation_and_related_order_take_precedence(self):
        self.db.insert_or_update_order("fixture-candidate", cookie_id="fixture-shop", buyer_id="fixture-buyer", buyer_nick="订单昵称", chat_id="other-chat", item_id="fixture-item")
        self.assertEqual(await self.identity({"otherUserName": "会话昵称"}), ("会话昵称", "conversation"))
        with patch("utils.xianyu_seller_api.XianyuSellerAPI") as factory:
            self.assertEqual(await self.identity(), ("订单昵称", "order"))
            factory.assert_not_called()

    async def test_success_is_cached_and_survives_database_restart(self):
        api = self.profile_api()
        with patch("utils.xianyu_seller_api.XianyuSellerAPI", return_value=api):
            self.assertEqual(await self.identity(), ("公开买家昵称", "public_profile"))
            self.db.close()
            self.db = DBManager(self.path)
            self.assertEqual(await self.identity(), ("公开买家昵称", "public_profile_cache"))
        api.get_user_profile.assert_awaited_once_with("fixture-buyer")
        self.assertEqual(store.buyer_profile(self.db, self.uid + 1, "fixture-shop", "fixture-buyer")["nickname"], "")

    async def test_failure_has_six_hour_cooldown_and_does_not_fail_context(self):
        api = self.profile_api()
        api.get_user_profile.side_effect = RuntimeError("fixture network unavailable")
        with patch("utils.xianyu_seller_api.XianyuSellerAPI", return_value=api):
            self.assertEqual(await self.identity(), ("", "unavailable"))
            self.assertEqual(await self.identity(), ("", "unavailable"))
        api.get_user_profile.assert_awaited_once()
        cached = store.buyer_profile(self.db, self.uid, "fixture-shop", "fixture-buyer")
        self.assertGreater(cached["retry_after"], time.time() + 21590)
        self.db.close()
        self.db = DBManager(self.path)
        with patch("utils.xianyu_seller_api.XianyuSellerAPI") as factory:
            self.assertEqual(await self.identity(), ("", "unavailable"))
            factory.assert_not_called()
        self.assertTrue(store.claim_buyer_profile_fetch(self.db, self.uid, "fixture-shop", "fixture-buyer", now=cached["retry_after"] + 1))

    async def test_timeout_returns_missing_identity_and_cools_down(self):
        self.run = AsyncMock(side_effect=asyncio.TimeoutError)
        self.assertEqual(await self.identity(), ("", "unavailable"))
        self.assertEqual(await self.identity(), ("", "unavailable"))
        self.run.assert_awaited_once()

    async def test_parallel_and_cross_account_lookups_are_isolated(self):
        api = self.profile_api()
        with patch("utils.xianyu_seller_api.XianyuSellerAPI", return_value=api):
            await asyncio.gather(self.identity(), self.identity())
            self.assertEqual(api.get_user_profile.await_count, 1)
            await self.identity(account="fixture-shop-b")
            self.assertEqual(api.get_user_profile.await_count, 2)

    async def test_auth_paused_avoids_platform_lookup(self):
        with patch("app.services.auth_recovery.auth_paused", return_value=True), patch("utils.xianyu_seller_api.XianyuSellerAPI") as factory:
            self.assertEqual(await self.identity(), ("", "unavailable"))
            factory.assert_not_called()

    async def test_empty_profile_is_cached_as_failure_and_cookie_changes_do_not_overwrite_login(self):
        api = self.profile_api("")
        async def lookup(_):
            self.instance.cookies_str = "fixture=new-login"
            api.cookies_str = "fixture=old-response"
            return {"module": {"base": {"displayName": ""}}}
        api.get_user_profile.side_effect = lookup
        with patch("utils.xianyu_seller_api.XianyuSellerAPI", return_value=api):
            await self.identity()
            await self.identity()
        api.get_user_profile.assert_awaited_once()
        self.assertEqual(self.instance.cookies_str, "fixture=new-login")


class ProgrammaticEchoTests(TestCase):
    def test_automatic_echo_is_consumed_once_and_chat_scoped(self):
        live = XianyuLive.__new__(XianyuLive)
        live._remember_programmatic_echo("chat-a", "fixture card content")
        self.assertFalse(
            live._consume_programmatic_echo("chat-b", "fixture card content")
        )
        self.assertTrue(
            live._consume_programmatic_echo("chat-a", "fixture card content")
        )
        self.assertFalse(
            live._consume_programmatic_echo("chat-a", "fixture card content")
        )
        self.assertNotIn("fixture card content", str(live._programmatic_echoes))

    def test_image_aliases_share_one_echo_claim(self):
        live = XianyuLive.__new__(XianyuLive)
        live._remember_programmatic_echo("chat-a", ["[图片]", "[图片消息]"])
        self.assertTrue(live._consume_programmatic_echo("chat-a", "[图片]"))
        self.assertFalse(live._consume_programmatic_echo("chat-a", "[图片消息]"))

    def test_expired_echo_does_not_suppress_official_app_manual_handover(self):
        live = XianyuLive.__new__(XianyuLive)
        with patch("XianyuAutoAsync.time.monotonic", return_value=0):
            live._remember_programmatic_echo("chat-a", "fixture")
        with patch("XianyuAutoAsync.time.monotonic", return_value=121):
            self.assertFalse(live._consume_programmatic_echo("chat-a", "fixture"))


class CardParsingTests(TestCase):
    def test_explicit_card_identity_only_and_no_template_dump(self):
        from app.xianyu_im import parse_card, parse_message

        self.assertEqual(
            parse_card({"template": {"data": {"orderId": "fixture-order"}}}),
            {"kind": "order", "orderId": "fixture-order"},
        )
        self.assertIsNone(
            parse_card({"template": {"url": "https://fixture.example/?token=secret"}})
        )
        raw = {
            "message": {
                "extension": {"senderUserId": "fixture-buyer"},
                "content": {
                    "custom": {
                        "data": json.dumps(
                            {
                                "template": {
                                    "data": {"orderId": "fixture-order"},
                                    "signature": "never-render-fixture",
                                }
                            }
                        )
                    }
                },
            }
        }
        parsed = parse_message(raw, "fixture-shop")
        self.assertEqual(parsed["card"]["orderId"], "fixture-order")
        self.assertNotIn("never-render-fixture", parsed["text"])

    def test_platform_sender_does_not_appear_as_buyer_text(self):
        from app.xianyu_im import parse_message

        raw = {
            "message": {
                "extension": {"senderUserId": "0"},
                "content": {
                    "custom": {
                        "data": json.dumps(
                            {"contentType": 1, "text": {"text": "fixture notice"}}
                        )
                    }
                },
            }
        }
        self.assertEqual(parse_message(raw, "fixture-shop")["type"], "system")
