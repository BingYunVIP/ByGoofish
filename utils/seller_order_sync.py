"""卖出订单同步：把卖家端接口的数据落到 orders 表。

数据来源有两个，互为补充：

- ``merchant.sold.get``：订单列表，字段最全（含成交额和收货信息），但服务端
  只保留近期订单。实测账号在订单管理页也只显示这批，与接口返回一致。
- ``merchant.refund.list``：退款列表，能返回订单列表已经查不到的历史归档单，
  但没有收货信息，成交额只能按单价乘件数估算。

订单列表的数据优先，退款列表只用来补全订单列表缺失的单。
"""

from typing import Any, Dict, Optional
import asyncio
import time

from loguru import logger

from utils.order_status_rules import normalize_order_status
from utils.order_buyer_info import (
    buyer_info_complete, field_state, log_buyer_info_capture,
    merge_buyer_info, parse_privacy_address,
)
from utils.xianyu_seller_api import (
    SellerApiError,
    XianyuSellerAPI,
    parse_consign_render,
    parse_refund_order,
    parse_sold_order,
)


def _save_order(db_manager, cookie_id: str, parsed: Dict[str, Any]) -> bool:
    """把解析后的订单写入数据库。"""
    order_id = parsed.get("order_id")
    if not order_id:
        return False

    previous = db_manager.get_order_by_id(order_id) or {}
    # 列表/详情可能暂时缺项或脱敏，不能用空值擦掉上次平台返回的完整信息。
    values = dict(parsed)
    for key in ('buyer_nick', 'receiver_name', 'receiver_phone', 'receiver_address'):
        if field_state(previous.get(key)) == 'present' and field_state(values.get(key)) != 'present':
            values[key] = previous[key]

    saved = db_manager.insert_or_update_order(
        order_id=order_id,
        item_id=parsed.get("item_id") or None,
        buyer_id=parsed.get("buyer_id") or None,
        buyer_nick=values.get("buyer_nick") or None,
        buyer_info_private=parsed.get('is_privacy') if 'is_privacy' in parsed else None,
        quantity=str(parsed.get("buy_num") or 1),
        amount=parsed.get("amount") or None,
        order_status=normalize_order_status("", parsed.get("status_text")),
        cookie_id=cookie_id,
        created_at=parsed.get("created_at") or None,
        verified_seller=True,
        receiver_name=values.get("receiver_name") or None,
        receiver_phone=values.get("receiver_phone") or None,
        receiver_address=values.get("receiver_address") or None,
        buy_num=parsed.get("buy_num"),
        auction_price=parsed.get("auction_price") or None,
        confirm_fee=parsed.get("confirm_fee") or None,
        refund_fee=parsed.get("refund_fee") or None,
        post_fee=parsed.get("post_fee") or None,
    )

    # 订单同步拿到的商品标题回填 item_info，保证订单列表不依赖
    # "商品详情已生成" 也能显示商品名（不覆盖商品同步写入的完整数据）
    if saved:
        log_buyer_info_capture(cookie_id, order_id, parsed, 'seller_sync_saved')
        item_title = parsed.get("item_title")
        item_id = parsed.get("item_id")
        if item_title and item_id:
            try:
                db_manager.upsert_item_title(cookie_id, str(item_id), str(item_title))
            except Exception as exc:
                logger.warning(f"回填商品标题失败 {item_id}: {exc}")

    return saved


async def supplement_buyer_info(api, cookie_id, order_id, result, fetch_consign=True):
    """缺项时补调只读发货详情与隐私地址；每个来源最多一次，不循环重试。"""
    if buyer_info_complete(result):
        return result
    if fetch_consign:
        try:
            payload = await api.get_consign_render(order_id)
            render = parse_consign_render(payload)
            log_buyer_info_capture(cookie_id, order_id, render, 'consign.page.render', payload)
            merge_buyer_info(result, render)
        except SellerApiError as exc:
            logger.warning(f'买家信息采集 source=consign.page.render order_suffix={str(order_id)[-5:]} error={type(exc).__name__}')
    if not all(field_state(result.get(key)) == 'present' for key in
               ('receiver_name', 'receiver_phone', 'receiver_address')):
        try:
            payload = await api.get_privacy_address(order_id)
            address = parse_privacy_address(payload)
            log_buyer_info_capture(cookie_id, order_id, address, 'privacy.address', payload)
            merge_buyer_info(result, address)
        except SellerApiError as exc:
            logger.warning(f'买家信息采集 source=privacy.address order_suffix={str(order_id)[-5:]} error={type(exc).__name__}')
    return result


async def fetch_order_detail_direct(
    cookie_id: str,
    cookies_str: str,
    order_id: str,
) -> Optional[Dict[str, Any]]:
    """直连获取单个订单的完整详情，返回结构兼容浏览器抓取的结果。

    组合两个接口：``sold.get`` 给出成交额和状态，``consign.page.render``
    给出规格 —— 后者是规格的唯一来源，订单列表的 itemInfoLines 是空的。

    自动发货依赖规格来决定发哪个卡密，因此两个接口都要调；任一失败时返回
    已获取的部分，由调用方决定是否回退到浏览器抓取。

    Returns:
        字段与 ``utils.order_detail_fetcher.fetch_order_detail_simple`` 对齐；
        完全拿不到数据时返回 None。
    """
    if not order_id or not cookies_str:
        return None

    api = XianyuSellerAPI(cookie_id, cookies_str)
    result: Dict[str, Any] = {}
    try:
        # 成交额、状态、买家和收货信息
        try:
            batch = await api.get_sold_orders(order_ids=str(order_id), rows_per_page=10)
            for item in batch.get("items") or []:
                parsed = parse_sold_order(item)
                if parsed.get("order_id") == str(order_id):
                    result.update(parsed)
                    log_buyer_info_capture(cookie_id, order_id, parsed, 'sold.get', item)
                    break
        except SellerApiError as exc:
            logger.debug(f"【{cookie_id}】订单 {order_id} 列表查询失败: {exc}")

        # 规格信息优先从买家端订单详情接口拿（skuInfo 格式固定为 "规格名:规格值"）
        try:
            detail = await api.get_order_detail(order_id)
            components = detail.get("components") or []
            for comp in components:
                if comp.get("render") == "orderInfoVO":
                    data = comp.get('data') or {}
                    buyer = data.get('buyerInfo') or {}
                    address = parse_privacy_address(data.get('addressInfo') or {})
                    address.update(buyer_id=buyer.get('userId') or '',
                                   buyer_nick=buyer.get('userNick') or buyer.get('nick') or '')
                    log_buyer_info_capture(cookie_id, order_id, address, 'order.detail', data)
                    merge_buyer_info(result, address)
                    item_info = data.get("itemInfo") or {}
                    sku_info = item_info.get("skuInfo", "")
                    if sku_info and ":" in sku_info:
                        parts = sku_info.split(":", 1)
                        result["spec_name"] = parts[0].strip()
                        result["spec_value"] = parts[1].strip() if len(parts) > 1 else ""
                        logger.debug(
                            f"【{cookie_id}】订单 {order_id} 从 order.detail 获取规格: "
                            f"{result['spec_name']}={result['spec_value']}"
                        )
                    break
        except (SellerApiError, Exception) as exc:
            logger.debug(f"【{cookie_id}】订单 {order_id} order.detail 查询失败: {exc}")

        # 规格信息的备用来源：发货页渲染接口的 itemInfoLines
        consign_attempted = False
        if not result.get("spec_value"):
            consign_attempted = True
            try:
                payload = await api.get_consign_render(order_id)
                render = parse_consign_render(payload)
                log_buyer_info_capture(cookie_id, order_id, render, 'consign.page.render', payload)
                merge_buyer_info(result, render)
                for key in (
                    "spec_name",
                    "spec_value",
                    "need_serial_number",
                    "need_upload_report",
                    "support_consign_type",
                ):
                    if render.get(key) not in (None, "", []):
                        result[key] = render[key]
                # 列表接口缺字段时用渲染接口补齐
                for key in (
                    "item_id",
                    "item_title",
                    "receiver_name",
                    "receiver_phone",
                    "receiver_address",
                    "buyer_id",
                    "buyer_nick",
                    "auction_price",
                ):
                    if not result.get(key) and render.get(key):
                        result[key] = render[key]
                if not result.get("buy_num"):
                    result["buy_num"] = render.get("buy_num") or 1
            except SellerApiError as exc:
                logger.debug(f"【{cookie_id}】订单 {order_id} 发货页渲染失败: {exc}")
        if result:
            await supplement_buyer_info(api, cookie_id, order_id, result, fetch_consign=not consign_attempted)
            log_buyer_info_capture(cookie_id, order_id, result, 'seller_merged')
    except Exception as e:
        logger.warning(f"【{cookie_id}】订单 {order_id} 直连获取异常: {e}")
    finally:
        await api.close()

    if not result:
        return None

    result.setdefault("order_id", str(order_id))
    result["url"] = (
        f"https://www.goofish.com/order-detail?orderId={order_id}&role=seller"
    )
    result["title"] = f"订单详情 - {order_id}"
    result["quantity"] = str(result.get("buy_num") or 1)
    result["order_time"] = result.get("created_at") or ""
    result["order_status"] = normalize_order_status("", result.get("status_text"))
    result["from_seller_api"] = True
    return result


async def sync_account_orders(
    cookie_id: str,
    cookies_str: str,
    include_refund_history: bool = True,
) -> Dict[str, Any]:
    """同步单个账号的全部卖出订单。

    不按时间范围筛选 —— 服务端本身只保留近期订单，加时间条件反而会把边界上的
    订单切掉（例如账号最早一单正好落在 30 天前后）。

    Args:
        include_refund_history: 是否用退款列表补全历史归档订单。

    Returns:
        ``{"total", "saved", "failed", "from_refund", "expected", "complete", "cookies_str"}``。
        ``complete`` 为 False 表示拉取数量少于服务端角标，调用方应关注。
    """
    result: Dict[str, Any] = {
        "total": 0,
        "saved": 0,
        "failed": 0,
        "from_refund": 0,
        "expected": None,
        "complete": True,
        "cookies_str": cookies_str,
    }
    if not cookies_str:
        return result

    from app.db_manager import db_manager

    api = XianyuSellerAPI(cookie_id, cookies_str)
    try:
        # 服务端角标是订单总数的权威值，用来判断是否拉全
        expected = await api.get_expected_order_total()
        result["expected"] = expected

        try:
            items = await api.iter_sold_orders(rows_per_page=50)
        except SellerApiError as exc:
            logger.warning(f"【{cookie_id}】卖出订单拉取失败: {exc}")
            items = []

        seen = set()
        for item in items:
            parsed = parse_sold_order(item)
            order_id = parsed.get("order_id")
            if not order_id:
                continue
            seen.add(order_id)
            try:
                # 批量同步只补近期缺失字段，历史归档单不逐笔发额外请求。
                created_at = parsed.get('created_at') or ''
                from datetime import datetime, timezone
                try:
                    age = time.time() - datetime.fromisoformat(created_at).replace(tzinfo=timezone.utc).timestamp()
                except (TypeError, ValueError):
                    age = float('inf')
                if 0 <= age <= 7 * 86400 and not buyer_info_complete(parsed):
                    previous = db_manager.get_order_by_id(order_id) or {}
                    if not buyer_info_complete(previous):
                        await supplement_buyer_info(api, cookie_id, order_id, parsed)
                        await asyncio.sleep(2)
                ok = _save_order(db_manager, cookie_id, parsed)
                result["saved"] += 1 if ok else 0
                result["failed"] += 0 if ok else 1
            except Exception as e:
                result["failed"] += 1
                logger.error(f"【{cookie_id}】保存卖出订单失败 {order_id}: {e}")

        result["total"] = len(seen)

        if expected is not None and len(seen) < expected:
            result["complete"] = False
            logger.warning(
                f"【{cookie_id}】卖出订单可能未拉全: 已获取 {len(seen)} 单，"
                f"服务端角标为 {expected} 单"
            )

        # 补全订单列表已查不到的历史归档单
        if include_refund_history:
            try:
                refunds = await api.iter_refund_orders(rows_per_page=50)
            except SellerApiError as exc:
                logger.warning(f"【{cookie_id}】退款单拉取失败: {exc}")
                refunds = []

            for item in refunds:
                parsed = parse_refund_order(item)
                order_id = parsed.get("order_id")
                if not order_id or order_id in seen:
                    continue
                seen.add(order_id)
                try:
                    ok = _save_order(db_manager, cookie_id, parsed)
                    if ok:
                        result["saved"] += 1
                        result["from_refund"] += 1
                    else:
                        result["failed"] += 1
                except Exception as e:
                    result["failed"] += 1
                    logger.error(f"【{cookie_id}】保存历史退款单失败 {order_id}: {e}")

            result["total"] = len(seen)

        # 接口会下发新的签名令牌，回传给调用方以便同步到账号会话
        result["cookies_str"] = api.cookies_str
    except Exception as e:
        logger.error(f"【{cookie_id}】卖出订单同步异常: {e}")
    finally:
        await api.close()

    logger.info(
        f"【{cookie_id}】卖出订单同步完成: 共 {result['total']} 单"
        f"（其中退款历史补全 {result['from_refund']} 单），"
        f"成功 {result['saved']}，失败 {result['failed']}"
        + ("" if result["complete"] else f"，⚠️ 少于服务端角标 {result['expected']}")
    )
    return result
