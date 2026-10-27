#!/usr/bin/env python3
"""隔离静态客服工作台冒烟；所有接口由本地夹具拦截，禁止平台流量。"""
import argparse, json, time, threading, http.server, functools
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from playwright.sync_api import sync_playwright, expect


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stage", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=True)
    http_requests = []

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            http_requests.append((self.path, str(self.directory)))
            if self.path.startswith("/static/"):
                self.path = self.path[len("/static") :]
            super().do_GET()

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Quiet, directory=str(a.stage.resolve()))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    now = int(time.time() * 1000)
    accounts = [
        {
            "accountId": "workspace-shop-a",
            "displayName": "一号店",
            "remark": "一号店 · 售前客服",
            "nickname": "测试店铺",
            "connected": True,
        },
        {
            "accountId": "workspace-shop-b",
            "displayName": "二号店",
            "remark": "二号店 · 售后客服",
            "connected": True,
        },
    ]
    conv = [
        {
            "cid": "workspace-chat-a",
            "rawCid": "workspace-chat-a@goofish",
            "otherUserId": "workspace-buyer-a",
            "otherUserName": "林间来客",
            "itemId": "workspace-item-a",
            "itemTitle": "数字商品 · 标准规格",
            "lastMessageTime": now,
            "lastMessageSummary": "已经付款了，请问多久发货？",
            "unreadCount": 2,
            "workspace": {"needs_reply": True},
        },
        {
            "cid": "workspace-chat-b",
            "rawCid": "workspace-chat-b@goofish",
            "otherUserId": "workspace-buyer-b",
            "otherUserName": "海边的风",
            "itemId": "workspace-item-b",
            "itemTitle": "常用服务 · 月卡",
            "lastMessageTime": now - 60000,
            "lastMessageSummary": "谢谢，已经收到了",
            "unreadCount": 0,
            "workspace": {},
        },
    ]
    order = {
        "id": "workspace-order-a",
        "order_id": "workspace-order-a",
        "cookie_id": "workspace-shop-a",
        "item_id": "workspace-item-a",
        "buyer_id": "workspace-buyer-a",
        "buyer_nick": "林间来客",
        "item_title": "数字商品 · 标准规格",
        "quantity": 1,
        "amount": "5.00",
        "status": "pending_ship",
        "order_status": "pending_ship",
        "receiver_name": "示例收件人",
        "receiver_phone": "13800000000",
        "receiver_address": "示例省示例市示例区示例街道很长很长的完整地址，测试楼101室",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "interaction_attempts": [],
    }
    messages = [
        {
            "messageId": f"workspace-message-{i}",
            "senderId": "workspace-buyer-a" if i % 2 else "workspace-shop-a",
            "senderName": "",
            "isSelf": not i % 2,
            "type": "system" if i == 18 else "text",
            "text": (
                "[我已付款，等待你发货]"
                if i == 18
                else (
                    "已经付款了，请问多久发货？"
                    if i == 19
                    else (
                        "您好，欢迎光临，商品详情与订单信息都可以在右侧查看。"
                        if not i % 2
                        else "这个规格可以使用多久？"
                    )
                )
            ),
            "images": [],
            "time": now - (20 - i) * 10000,
        }
        for i in range(20)
    ]
    state = {}
    for i, (event_type, text, direction) in enumerate([
        ("review_requested", "快给ta一个评价吧～", "platform"),
        ("receipt_thanks", "亲，感谢支持！有任何问题随时找我~", "outbound"),
        ("flower_requested", "可以送我闲鱼小红花吗～", "outbound"),
        ("review_completed", "[我完成了评价]", "outbound"),
        ("order_pending_payment", "[我已拍下，待付款]", "inbound"),
        ("order_paid_pending_ship", "[我已付款，等待你发货]", "inbound"),
        ("seller_shipped", "[你已发货]", "outbound"),
        ("buyer_received", "[买家确认收货，交易成功]", "inbound"),
        ("flower_sent_confirmation", "已求买家送我闲鱼小红花", "outbound"),
        ("refund_requested", "[我发起了退款申请]", "inbound"),
        ("refund_completed", "[退款成功，钱款已原路退返]", "platform"),
    ]):
        messages.append({"messageId": f"transaction-{i}", "senderId": "workspace-shop-a" if direction == "outbound" else "workspace-buyer-a",
                         "senderName": "", "isSelf": direction == "outbound", "type": "system", "event_type": event_type,
                         "event_direction": direction, "event_actor_role": "闲鱼" if direction == "platform" else "卖家" if direction == "outbound" else "买家",
                         "event_label": "我方 → 买家" if direction == "outbound" else "买家 → 我方" if direction == "inbound" else "闲鱼通知 · 面向我方",
                         "event_order_id": "workspace-order-a" if i != 1 else "",
                         "text": text, "images": [], "time": now + i})
    scenario = {"mode": "normal", "held": []}
    errors = []
    external = []
    sends = []
    checks = []
    api_requests = []
    notes = {}
    pause = {"paused": False, "expires_at": 0, "duration_minutes": 10}

    def context(cid):
        buyer = "workspace-buyer-a" if cid.endswith("-a") else "workspace-buyer-b"
        return {
            "owner_id": 1,
            "buyer": {
                "id": buyer,
                "nickname": "林间来客" if buyer.endswith("-a") else "海边的风",
                "note": notes.get(buyer, ""),
            },
            "item": {
                "item_id": "workspace-item-a",
                "item_title": "数字商品 · 标准规格",
            },
            "orders": ([order, {**order, "order_id": "workspace-order-b", "id": "workspace-order-b"}]
                       if scenario["mode"] == "multiple-orders" else [order]) if cid.endswith("-a") else [],
            "candidate_orders": [],
            "handover": pause,
            "features": {"auto_rate_enabled": True, "auto_flower_enabled": True},
            "workspace": state.get(cid, {}),
            "source": "official_cache",
        }

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--no-proxy-server"]
            )
            ctx = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                timezone_id="Asia/Singapore",
                reduced_motion="reduce",
            )

            def intercept(route):
                req = route.request
                u = urlsplit(req.url)
                if u.hostname != "127.0.0.1":
                    external.append(u.hostname)
                    route.abort()
                    return
                path = u.path
                if (
                    path == "/"
                    or path.startswith("/assets/")
                    or path.endswith((".svg", ".ico", ".png", ".css", ".js"))
                ):
                    route.continue_()
                    return
                api_requests.append((req.method, path))
                data = {}
                body = (
                    json.loads(req.post_data or "{}")
                    if req.headers.get("content-type", "").startswith(
                        "application/json"
                    )
                    else {}
                )
                if path == "/verify":
                    data = {
                        "authenticated": True,
                        "user_id": 1,
                        "username": "fixture-owner",
                        "is_admin": True,
                    }
                elif path == "/system-settings/public":
                    data = {}
                elif path == "/chat/accounts" and scenario["mode"] != "paused":
                    data = {
                        "success": True,
                        "data": (
                            [{**a, "connected": False} for a in accounts]
                            if scenario["mode"] == "offline"
                            else accounts
                        ),
                    }
                elif path == "/chat/accounts" and scenario["mode"] == "paused":
                    data = {
                        "success": True,
                        "data": [
                            {
                                **account,
                                "connected": False,
                                "authPaused": True,
                                "recoveryMessage": "账号待人工验证，请前往账号管理完成验证",
                            }
                            for account in accounts
                        ],
                    }
                elif path.startswith("/chat/events/"):
                    route.abort()
                    return
                elif (
                    path.startswith("/chat/conversations/")
                    and scenario["mode"] == "loading"
                ):
                    scenario["held"].append(route)
                    return
                elif (
                    path.startswith("/chat/conversations/")
                    and scenario["mode"] == "error"
                ):
                    route.fulfill(
                        status=503,
                        content_type="application/json",
                        body=json.dumps({"detail": "夹具：会话读取暂不可用"}),
                    )
                    return
                elif (
                    path.startswith("/chat/conversations/")
                    and scenario["mode"] == "empty"
                ):
                    data = {
                        "success": True,
                        "data": {"conversations": [], "hasMore": False},
                    }
                elif path.startswith("/chat/conversations/"):
                    data = {
                        "success": True,
                        "data": {
                            "conversations": [
                                {
                                    **v,
                                    "workspace": {
                                        **v.get("workspace", {}),
                                        **state.get(v["cid"], {}),
                                    },
                                }
                                for v in conv
                            ],
                            "hasMore": False,
                        },
                    }
                elif path.startswith("/chat/messages/"):
                    older = bool(parse_qs(u.query).get("cursor"))
                    data = {
                        "success": True,
                        "data": {
                            "messages": (
                                [
                                    {
                                        "messageId": "older-fixture",
                                        "senderId": "workspace-buyer-a",
                                        "senderName": "",
                                        "isSelf": False,
                                        "type": "text",
                                        "text": "更早的消息夹具",
                                        "images": [],
                                        "time": now - 900000,
                                    }
                                ]
                                if older
                                else messages
                            ),
                            "hasMore": not older,
                            "nextCursor": now - 300000 if not older else now - 900000,
                        },
                    }
                elif path.startswith("/chat/context/"):
                    data = {"success": True, "data": context(path.rsplit("/", 1)[-1])}
                elif path.startswith("/chat/state/"):
                    cid = path.rsplit("/", 1)[-1]
                    state[cid] = {**state.get(cid, {}), **body}
                    notes[context(cid)["buyer"]["id"]] = body.get(
                        "note", context(cid)["buyer"]["note"]
                    )
                    data = {"success": True, "data": state[cid]}
                elif path.startswith("/chat/handover/"):
                    pause.update(
                        paused=body.get("action") == "pause",
                        expires_at=(
                            time.time() + 600 if body.get("action") == "pause" else 0
                        ),
                    )
                    data = {"success": True, "data": pause}
                elif path.startswith("/chat/send-image/") or path.startswith(
                    "/chat/send/"
                ):
                    sends.append(path)
                    data = {
                        "success": True,
                        "data": {
                            "status": "sent",
                            "messageId": "fixture-sent-" + str(len(sends)),
                        },
                    }
                elif "quick-phrase" in path:
                    data = {
                        "success": True,
                        "data": [
                            {
                                "id": 1,
                                "title": "欢迎语",
                                "content": "您好，请问有什么可以帮您？",
                                "category": "售前",
                                "enabled": True,
                            }
                        ],
                    }
                elif path == "/delivery-attempts":
                    data = {"data": []}
                elif path == "/cookies/details":
                    data = []
                else:
                    data = {"success": True, "data": []}
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(data, ensure_ascii=False),
                )

            ctx.route("**/*", intercept)
            ctx.add_init_script(
                "localStorage.setItem('auth_token','fixture-only');localStorage.setItem('active_page','messages');"
            )
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base, wait_until="networkidle")
            page.screenshot(path=str(a.output / "initial.png"))
            (a.output / "initial-debug.json").write_text(
                json.dumps(
                    {
                        "errors": errors,
                        "requests": api_requests,
                        "http_requests": http_requests,
                        "text": page.locator("body").inner_text()[:2500],
                    },
                    ensure_ascii=False,
                )
            )
            page.get_by_role("button", name="打开会话 林间来客", exact=True).click()
            page.get_by_role("heading", name="买家资料", exact=True).wait_for()
            for title in ["交易评价提醒", "收货致谢", "小红花请求", "评价已完成", "卖家已发货", "买家已确认收货", "退款完成"]:
                expect(page.locator('.chat-event').get_by_text(title, exact=True)).to_have_count(1)
            expect(page.locator('.chat-thread > [aria-live="polite"]')).to_have_count(0)
            page.locator('.chat-event').first.get_by_role('button', name='查看关联订单', exact=True).click()
            expect(page.get_by_label('选择关联订单', exact=True)).to_have_value('workspace-order-a')
            scenario['mode'] = 'multiple-orders'
            page.get_by_label('刷新账号和会话', exact=True).click()
            page.get_by_label('收货致谢', exact=True).get_by_role('button', name='选择关联订单', exact=True).click()
            expect(page.get_by_label('选择关联订单', exact=True)).to_have_value('')
            expect(page.get_by_text('请先选择订单，再查看对应交易资料。', exact=True)).to_be_visible()
            page.get_by_label('选择关联订单', exact=True).select_option('workspace-order-b')
            expect(page.get_by_text('workspace-order-b', exact=True)).to_be_visible()
            scenario['mode'] = 'normal'
            page.reload(wait_until='networkidle')
            page.get_by_role('button', name='打开会话 林间来客', exact=True).click()
            checks.append('transaction lifecycle cards; explicit order navigation; ambiguous cards require selection; no sends')
            for width in [1440, 1920, 1280, 768, 375]:
                page.set_viewport_size({"width": width, "height": 1000})
                page.wait_for_timeout(250)
                page.wait_for_function(
                    "innerWidth>=1024 || document.querySelector('aside.fixed').getBoundingClientRect().right<=1"
                )
                if width < 1024:
                    menu = page.get_by_role("button", name="关闭导航菜单", exact=True)
                    if menu.is_visible():
                        menu.click()
                assert page.evaluate(
                    "document.documentElement.scrollWidth<=innerWidth"
                ), f"overflow {width}"
                rect = page.locator(".chat-shell").bounding_box()
                cols = page.locator(".chat-grid").evaluate(
                    'e=>getComputedStyle(e).gridTemplateColumns.split(" ").length'
                )
                expected = (
                    3 if rect["width"] >= 1100 else 2 if rect["width"] >= 760 else 1
                )
                assert cols == expected, (width, rect, cols)
                for card in page.locator('.chat-event').all():
                    bounds = card.evaluate("e=>{const r=e.getBoundingClientRect(),p=e.parentElement.getBoundingClientRect();return {left:r.left,right:r.right,width:r.width,parentLeft:p.left,parentRight:p.right,overflow:e.scrollWidth>e.clientWidth+1,direction:e.dataset.eventDirection}}")
                    assert not bounds['overflow'], (width,bounds)
                    assert bounds['left'] >= bounds['parentLeft']-1 and bounds['right'] <= bounds['parentRight']+1
                    if bounds['direction']=='outbound': assert abs(bounds['right']-bounds['parentRight'])<2
                    else: assert abs(bounds['left']-bounds['parentLeft'])<2
                page.screenshot(
                    path=str(a.output / f"workspace-{width}.png"), full_page=True
                )
            checks.append("responsive content-width columns: 375/768/1280/1440/1920")
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.get_by_role("textbox", name="消息内容", exact=True).fill("会话甲草稿")
            page.get_by_role("button", name="打开会话 海边的风", exact=True).click()
            expect(
                page.get_by_role("textbox", name="消息内容", exact=True)
            ).to_have_value("")
            page.get_by_role("textbox", name="消息内容", exact=True).fill("会话乙草稿")
            page.get_by_role("button", name="打开会话 林间来客", exact=True).click()
            expect(
                page.get_by_role("textbox", name="消息内容", exact=True)
            ).to_have_value("会话甲草稿")
            page.reload(wait_until="networkidle")
            page.get_by_role("button", name="打开会话 林间来客", exact=True).click()
            expect(
                page.get_by_role("textbox", name="消息内容", exact=True)
            ).to_have_value("会话甲草稿")
            checks.append("conversation drafts survive switching and reload")
            search = page.get_by_label("搜索联系人、商品或消息", exact=True)
            search.click()
            search.fill("海边")
            expect(
                page.get_by_role("button", name="打开会话 林间来客", exact=True)
            ).to_have_count(0)
            expect(
                page.get_by_role("button", name="打开会话 海边的风", exact=True)
            ).to_be_visible()
            search.fill("")
            compose = page.get_by_role("textbox", name="消息内容", exact=True)
            compose.fill("组合输入测试")
            compose.evaluate(
                "e=>e.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',isComposing:true,keyCode:229,bubbles:true}))"
            )
            assert not sends
            compose.fill("多行输入\n" * 15)
            height = compose.evaluate("e=>e.getBoundingClientRect().height")
            assert 64 < height <= 180
            compose.fill("会话甲草稿")
            checks.append(
                "loaded conversation search, IME guard and bounded auto-height composer"
            )

            page.get_by_role("textbox", name="消息内容", exact=True).focus()
            page.get_by_role("textbox", name="消息内容", exact=True).press(
                "Control+End"
            )
            page.get_by_role("button", name="选择表情", exact=True).click()
            page.get_by_role("button", name="[微笑]", exact=True).click()
            expect(
                page.get_by_role("textbox", name="消息内容", exact=True)
            ).to_have_value("会话甲草稿[微笑]")
            page.get_by_role("button", name="加载更早消息", exact=True).click()
            page.get_by_text("更早的消息夹具", exact=True).wait_for()
            checks.append("emoji insertion; history pagination")
            page.get_by_role("button", name="人工接管", exact=True).click()
            page.get_by_role("button", name="恢复自动回复", exact=True).wait_for()
            page.get_by_role("button", name="恢复自动回复", exact=True).click()
            checks.append("handover and resume")
            page.get_by_label("客服备注 · 仅本工作台可见", exact=True).fill(
                "fixture note"
            )
            page.get_by_role("button", name="保存备注", exact=True).click()
            page.wait_for_timeout(250)
            assert notes.get("workspace-buyer-a") == "fixture note"
            checks.append("notes and context")
            page.get_by_role("button", name="订单详情", exact=True).click()
            page.get_by_role("button", name="关闭订单详情", exact=True).click()
            checks.append("shared order detail dialog")
            order.update(status="completed", order_status="completed")
            page.get_by_role("button", name="刷新官方状态", exact=True).click()
            page.get_by_role("button", name="评价买家", exact=True).click()
            rating_dialog = page.get_by_role("dialog", name="给买家评价", exact=True)
            rating_dialog.get_by_role("button", name="中评", exact=True).click()
            rating_dialog.get_by_role("checkbox", name="匿名评价", exact=True).check()
            expect(rating_dialog.get_by_role("checkbox", name="匿名评价", exact=True)).to_be_checked()
            page.keyboard.press("Escape")
            expect(rating_dialog).not_to_be_visible()
            checks.append("shared rating dialog: neutral/anonymous/Escape without submission")
            page.evaluate("document.documentElement.dataset.theme='dark'")
            page.screenshot(path=str(a.output / "workspace-dark.png"), full_page=True)
            page.set_viewport_size({"width": 375, "height": 900})
            page.get_by_role("button", name="打开买家与订单资料", exact=True).click()
            page.get_by_role("button", name="关闭买家资料", exact=True).click()
            page.screenshot(
                path=str(a.output / "workspace-mobile-dark.png"), full_page=True
            )
            checks.append("dark theme and mobile context drawer")
            page.wait_for_function(
                "document.querySelector('aside.fixed').getBoundingClientRect().right<=1"
            )
            page.screenshot(
                path=str(a.output / "workspace-mobile-dark.png"), full_page=True
            )
            page.set_viewport_size({"width": 375, "height": 500})
            page.get_by_role("textbox", name="消息内容", exact=True).focus()
            assert page.locator(".chat-composer").evaluate(
                "e=>{const r=e.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1;}"
            )
            checks.append("small-height mobile composer remains fully inside viewport")
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.evaluate("document.documentElement.dataset.theme='light'")
            for mode in ["loading", "empty", "offline", "error", "paused"]:
                scenario["mode"] = mode
                platform_reads_before = len([x for x in api_requests if x[1].startswith(('/chat/conversations/', '/chat/messages/'))])
                page.reload(wait_until="domcontentloaded")
                if mode == "paused":
                    page.get_by_role("alert").filter(
                        has_text="账号待人工验证"
                    ).wait_for()
                    page.wait_for_timeout(500)
                    assert len([x for x in api_requests if x[1].startswith(('/chat/conversations/', '/chat/messages/'))]) == platform_reads_before
                elif mode == "loading":
                    page.get_by_text("正在加载会话…", exact=True).wait_for()
                elif mode == "empty":
                    page.get_by_text("所选范围内暂无会话", exact=True).wait_for()
                elif mode == "offline":
                    page.get_by_role(
                        "button", name="打开会话 林间来客", exact=True
                    ).click()
                    expect(
                        page.get_by_role("textbox", name="消息内容", exact=True)
                    ).to_be_disabled()
                else:
                    page.get_by_role("alert").filter(
                        has_text="夹具：会话读取暂不可用"
                    ).wait_for()
                page.wait_for_timeout(300)
                page.screenshot(
                    path=str(a.output / ("state-" + mode + ".png")), full_page=True
                )
                for pending in scenario["held"]:
                    try:
                        pending.abort()
                    except Exception:
                        pass
                scenario["held"].clear()
            checks.append(
                "loading, empty, offline and error states captured without sends"
            )

            assert not sends, "smoke must never send messages"
            assert not errors, errors
            assert not external, external
            result = {
                "checks": checks,
                "page_errors": errors,
                "external_requests": external,
                "message_sends": sends,
                "api_requests": len(api_requests),
            }
            (a.output / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2)
            )
            print(json.dumps(result, ensure_ascii=False))
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
