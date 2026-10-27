#!/usr/bin/env python3
"""真实隔离SQLite/API/SSE与构建产物联调，闲鱼IM及CDN仅使用内存夹具。"""
import argparse, asyncio, base64, io, json, os, secrets, socket, threading, time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import AsyncMock, patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    path = Path(os.environ.get("DB_PATH", "")).resolve()
    if not path.parent.name.startswith("bygoofish-isolated-") or path.exists():
        parser.error("必须使用全新dev_isolated环境")
    os.environ["APP_STATIC_DIR"] = str(args.stage.resolve())
    args.output.mkdir(parents=True, exist_ok=True)
    import uvicorn
    from playwright.sync_api import sync_playwright, expect
    from PIL import Image
    from app.reply_server import app
    from app.db_manager import db_manager as db
    from app import cookie_manager
    from app.auth import SESSION_TOKENS
    from app.services.chat_workspace import events
    from utils.direct_network import direct_browser_options

    uid = db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
    for cid in ["workspace-shop-a", "workspace-shop-b"]:
        db.save_cookie(cid, "fixture=value", uid)
    db.conn.execute(
        "UPDATE cookies SET remark='一号店 · 售前客服' WHERE id='workspace-shop-a'"
    )
    db.conn.commit()
    db.insert_or_update_order(
        "9000000000000000001",
        cookie_id="workspace-shop-a",
        item_id="workspace-item",
        buyer_id="workspace-buyer",
        buyer_nick="林间来客",
        chat_id="workspace-chat",
        order_status="pending_ship",
        amount="5.00",
        quantity="1",
        receiver_name="示例收件人",
        receiver_phone="13800000000",
        receiver_address="示例省示例市示例区测试路1号",
    )
    now = int(time.time() * 1000)

    def model(mid, text, sender="workspace-buyer", stamp=None):
        payload = base64.b64encode(
            json.dumps({"contentType": 1, "text": {"text": text}}).encode()
        ).decode()
        return {
            "message": {
                "messageId": mid,
                "createAt": stamp or now,
                "extension": {"senderUserId": sender, "reminderTitle": "林间来客"},
                "content": {"custom": {"data": payload}},
            }
        }

    class Fake:
        def __init__(self, cid):
            self.myid = cid
            self.cookie_id = cid
            self.cookies_str = "fixture=value"
            self.connection_state = "connected"
            self.ws = SimpleNamespace(closed=False)
            self.messages = [
                model(
                    f"fixture-{i}",
                    "这是一条用于检查滚动的历史消息，请勿向真实账号发送。",
                    stamp=now - 100000 + i * 1000,
                )
                for i in range(30)
            ]
            if cid == "workspace-shop-a":
                event = model("fixture-review", "快给ta一个评价吧～", stamp=now)
                event["message"]["extension"]["reminderTitle"] = "快给ta一个评价吧～"
                event["message"]["extension"]["reminderContent"] = "快给ta一个评价吧～"
                event["message"]["content"]["custom"]["data"] = json.dumps({"dxCard":{"item":{"main":{"exContent":{
                    "title":"快给ta一个评价吧～","button":{"intent":{"page":{"jumpUrl":"https://www.goofish.com/order-detail?orderId=9000000000000000001"}}}
                }}}}})
                self.messages.append(event)
            self.sent = []
            self.images = []
            self.fail = False
            self.delay_echo = False
            self.reads = 0

        async def get_im_conversations(self, cursor=None, limit=30):
            self.reads += 1
            return {
                "userConvs": [
                    {
                        "singleChatConversation": {
                            "cid": "workspace-chat@goofish",
                            "pairFirst": self.myid + "@goofish",
                            "pairSecond": "workspace-buyer@goofish",
                            "extension": {
                                "itemId": "workspace-item",
                                "itemTitle": "数字商品 · 标准规格",
                            },
                        },
                        "lastMessage": self.messages[-1],
                        "modifyTime": self.messages[-1]["message"]["createAt"],
                        "redPoint": 1,
                    }
                ],
                "hasMore": False,
            }

        async def get_im_messages(self, cid, cursor=None, limit=50):
            self.reads += 1
            return {
                "userMessageModels": list(reversed(self.messages)),
                "hasMore": False,
            }

        async def send_im_text(self, cid, toid, text):
            self.sent.append(text)
            if self.fail:
                raise TimeoutError("fixture only: response lost")
            mid = f"sent-fixture-{len(self.sent)}"
            if not self.delay_echo:
                self.messages.append(model(mid, text, self.myid, int(time.time() * 1000)))
            return {"code": 200, "body": {"messageId": mid}}

        async def send_im_image(self, cid, toid, url, width, height):
            self.images.append((width, height))
            return {"code": 200, "body": {"messageId": "image-fixture"}}

    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    one = Fake("workspace-shop-a")
    two = Fake("workspace-shop-b")
    cookie_manager.manager = SimpleNamespace(
        loop=loop,
        instances={"workspace-shop-a": one, "workspace-shop-b": two},
        get_cookie_status=lambda _: True,
        get_task_state=lambda _: "running",
    )
    token = secrets.token_urlsafe(32)
    SESSION_TOKENS[token] = {"user_id": uid}
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=sock.getsockname()[1],
            log_level="error",
            access_log=False,
        )
    )
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("isolated server startup timeout")
        time.sleep(0.05)
    errors = []
    external = []
    checks = []
    uploader = SimpleNamespace(
        upload_image=AsyncMock(return_value="https://fixture.example/attachment.jpg")
    )
    upload_context = AsyncMock()
    upload_context.__aenter__.return_value = uploader
    try:
        with patch(
            "utils.image_uploader.ImageUploader", return_value=upload_context
        ), sync_playwright() as pw:
            with pw.chromium.launch(
                **direct_browser_options({"headless": True})
            ) as browser:
                ctx = browser.new_context(
                    viewport={"width": 1440, "height": 1000},
                    timezone_id="Asia/Singapore",
                    reduced_motion="reduce",
                )

                def route(r):
                    if urlsplit(r.request.url).hostname != "127.0.0.1":
                        external.append("external blocked")
                        r.abort()
                        return
                    r.continue_()

                ctx.route("**/*", route)
                ctx.add_init_script(
                    'localStorage.setItem("auth_token",'
                    + json.dumps(token)
                    + ');localStorage.setItem("active_page","messages");'
                )
                page = ctx.new_page()
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(base, wait_until="domcontentloaded")
                page.get_by_role("button", name="打开会话 林间来客", exact=True).click()
                page.get_by_role("heading", name="买家资料", exact=True).wait_for()
                page.get_by_text("实时更新", exact=True).wait_for()
                checks.append("real API conversation/context and authorized SSE")
                expect(page.get_by_role('heading', name='林间来客', exact=True)).to_be_visible()
                event_card = page.get_by_label('交易评价提醒', exact=True)
                expect(event_card).to_be_visible()
                event_card.get_by_role('button', name='查看关联订单', exact=True).click()
                expect(page.get_by_label('选择关联订单', exact=True)).to_have_value('9000000000000000001')
                checks.append('real parser rejects event-heading nickname and resolves nested platform order id')
                page.get_by_label("客服备注 · 仅本工作台可见", exact=True).fill(
                    "隔离备注"
                )
                page.get_by_role("button", name="保存备注", exact=True).click()
                page.wait_for_timeout(600)
                assert (
                    db.conn.execute(
                        "SELECT note FROM chat_customer_notes WHERE cookie_id='workspace-shop-a'"
                    ).fetchone()[0]
                    == "隔离备注"
                )
                checks.append("real SQLite note persistence")
                page.get_by_role("button", name="人工接管", exact=True).click()
                page.get_by_role("button", name="恢复自动回复", exact=True).wait_for()
                page.get_by_role("button", name="恢复自动回复", exact=True).click()
                checks.append("real handover API and event refresh")
                box = page.get_by_role("textbox", name="消息内容", exact=True)
                box.fill("隔离正常发送")
                page.get_by_role("button", name="发送", exact=True).click()
                page.get_by_text("已发送", exact=True).wait_for()
                assert one.sent == ["隔离正常发送"]
                checks.append("acknowledged text and durable attempt")
                one.delay_echo = True
                box.fill('隔离延迟回显')
                page.get_by_role('button', name='发送', exact=True).click()
                echo = page.locator('.chat-thread [data-message-id="sent-fixture-2"]')
                expect(echo.get_by_text('隔离延迟回显', exact=True)).to_be_visible()
                expect(echo.get_by_role('status')).to_have_text('已发送')
                assert not any(m['message']['messageId'] == 'sent-fixture-2' for m in one.messages)
                expect(page.locator('.chat-thread > [aria-live="polite"]')).to_have_count(0)
                one.messages.append(model('sent-fixture-2', '隔离延迟回显', one.myid, int(time.time()*1000)))
                events.publish('workspace-shop-a', 'message', 'workspace-chat')
                page.wait_for_timeout(900)
                expect(page.locator('[data-message-id="sent-fixture-2"]')).to_have_count(1)
                expect(page.locator('.chat-bubble').get_by_text('隔离延迟回显', exact=True)).to_have_count(1)
                one.delay_echo = False
                checks.append('text echoes before delayed official history; matching platform id deduplicates; no separate send list')
                one.fail = True
                box.fill("隔离未知结果")
                page.get_by_role("button", name="发送", exact=True).click()
                page.get_by_text("结果待核查", exact=True).wait_for()
                expect(
                    page.get_by_role("button", name="发送", exact=True)
                ).to_be_enabled()
                page.get_by_role("button", name="发送", exact=True).click()
                page.wait_for_timeout(600)
                assert one.sent.count("隔离未知结果") == 1
                page.reload(wait_until="domcontentloaded")
                page.get_by_role("button", name="打开会话 林间来客", exact=True).click()
                page.get_by_text("结果待核查", exact=True).wait_for()
                expect(box).to_have_value("隔离未知结果")
                page.get_by_role("button", name="发送", exact=True).click()
                page.wait_for_timeout(600)
                assert one.sent.count("隔离未知结果") == 1
                page.screenshot(
                    path=str(args.output / "unknown-send.png"), full_page=True
                )
                one.fail = False
                account_selector = page.get_by_label("消息账号", exact=True)
                account_selector.select_option("workspace-shop-b")
                page.get_by_role("button", name="打开会话 林间来客", exact=True).click()
                expect(box).to_have_value("")
                expect(
                    page.get_by_label("客服备注 · 仅本工作台可见", exact=True)
                ).to_have_value("")
                expect(page.get_by_text("9000000000000000001", exact=True)).to_have_count(0)
                box.fill("隔离二号店草稿")
                account_selector.select_option("workspace-shop-a")
                page.get_by_role("button", name="打开会话 林间来客", exact=True).click()
                expect(box).to_have_value("隔离未知结果")
                expect(
                    page.get_by_label("客服备注 · 仅本工作台可见", exact=True)
                ).to_have_value("隔离备注")
                assert not two.sent and not two.images
                checks.append(
                    "same-cid shops isolate drafts, notes, order context and sends"
                )
                checks.append(
                    "unknown result survives reload and repeated click never resends"
                )
                box.fill("")
                buff = io.BytesIO()
                Image.new("RGB", (24, 16), "white").save(buff, format="PNG")
                page.locator("input[type=file]").set_input_files(
                    {
                        "name": "fixture.png",
                        "mimeType": "image/png",
                        "buffer": buff.getvalue(),
                    }
                )
                page.get_by_role("button", name="发送", exact=True).click()
                page.wait_for_function(
                    "!document.querySelector('img[alt=\"待发送图片\"]')"
                )
                assert len(one.images) == 1
                expect(page.locator('[data-message-id="image-fixture"] img[alt="聊天图片"]')).to_be_visible()
                assert page.locator('[data-message-id="image-fixture"] img').get_attribute('src').startswith('blob:')
                uploader.upload_image.assert_awaited_once()
                checks.append("real image decode/API/receipt with fake CDN only")
                (
                    page.locator(".chat-thread > div.overflow-y-auto").count()
                    if False
                    else None
                )
                scroll = page.locator(".chat-thread .overflow-y-auto").first
                scroll.evaluate(
                    'e=>{e.scrollTop=0;e.dispatchEvent(new Event("scroll"));}'
                )
                with db.lock:
                    viewed_before = db.conn.execute(
                        "SELECT viewed_time FROM chat_workspace_state WHERE cookie_id='workspace-shop-a' AND cid='workspace-chat'"
                    ).fetchone()[0]
                one.messages.append(
                    model(
                        "new-live-fixture",
                        "实时推送夹具",
                        stamp=int(time.time() * 1000),
                    )
                )
                events.publish("workspace-shop-a", "message", "workspace-chat")
                try:
                    page.get_by_role("region", name="聊天区域").get_by_text(
                        "实时推送夹具", exact=True
                    ).wait_for()
                except Exception:
                    page.screenshot(path=str(args.output / "push-failure.png"), full_page=True)
                    (args.output / "push-failure.json").write_text(json.dumps({
                        "errors": errors, "text": page.locator("body").inner_text(),
                        "fixture_last_id": one.messages[-1]["message"]["messageId"],
                        "rendered_ids": page.locator('[data-message-id]').evaluate_all("els=>els.map(e=>e.dataset.messageId)"),
                        "api_requests": api_requests,
                        "fake_message_count": len(one.messages),
                        "fake_latest_id": one.messages[-1]["message"]["messageId"],
                    }, ensure_ascii=False, indent=2))
                    raise
                assert scroll.evaluate("e=>e.scrollTop<100")
                checks.append("SSE updates without stealing history scroll")
                with db.lock:
                    viewed_after = db.conn.execute(
                        "SELECT viewed_time FROM chat_workspace_state WHERE cookie_id='workspace-shop-a' AND cid='workspace-chat'"
                    ).fetchone()[0]
                assert (
                    viewed_after == viewed_before
                ), "reading history must not mark unseen incoming messages as viewed"
                page.get_by_role("button").filter(has_text="回到底部").click()
                page.wait_for_timeout(700)
                with db.lock:
                    assert (
                        db.conn.execute(
                            "SELECT viewed_time FROM chat_workspace_state WHERE cookie_id='workspace-shop-a' AND cid='workspace-chat'"
                        ).fetchone()[0]
                        > viewed_before
                    )
                checks.append(
                    "unseen incoming stays unread until user returns to bottom"
                )

                page.wait_for_timeout(1200)
                reads = one.reads
                page.wait_for_timeout(10500)
                assert one.reads == reads, (reads, one.reads)
                checks.append("healthy SSE eliminates idle platform polling")
                assert not errors, errors
                assert not external, external
                (args.output / "result.json").write_text(
                    json.dumps(
                        {
                            "checks": checks,
                            "page_errors": errors,
                            "external_requests": external,
                            "fixture_text_frames": len(one.sent),
                            "fixture_image_frames": len(one.images),
                            "no_real_account_traffic": True,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                page.screenshot(
                    path=str(args.output / "integrated.png"), full_page=True
                )
                print(
                    json.dumps(
                        {
                            "checks": checks,
                            "errors": errors,
                            "no_real_account_traffic": True,
                        },
                        ensure_ascii=False,
                    )
                )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)
        SESSION_TOKENS.pop(token, None)


if __name__ == "__main__":
    main()
