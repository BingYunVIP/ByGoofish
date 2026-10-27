"""买家信息来源诊断：只记录字段状态，不记录姓名、电话或地址原文。"""
from loguru import logger


BUYER_FIELDS = (
    ("buyer_id", "buyer_id"),
    ("buyer_nick", "buyer_nick"),
    ("receiver_name", "receiver_name"),
    ("receiver_phone", "receiver_phone"),
    ("receiver_address", "receiver_address"),
)


def field_state(value) -> str:
    text = str(value or "").strip()
    if not text:
        return "missing"
    if any(marker in text for marker in ("*", "•", "隐私", "已隐藏", "未公开")):
        return "masked"
    return "present"


def buyer_info_complete(info) -> bool:
    return all(field_state(info.get(key)) == "present" for key, _ in BUYER_FIELDS)


def log_buyer_info_capture(cookie_id, order_id, info, source, payload=None):
    states = " ".join(f"{label}={field_state(info.get(key))}" for key, label in BUYER_FIELDS)
    privacy = bool(info.get("is_privacy"))
    suffix = str(order_id or "")[-5:] or "unknown"
    account_suffix = str(cookie_id or "")[-4:] or "unknown"
    message = (
        f"买家信息采集 source={source} order_suffix={suffix} "
        f"account_suffix={account_suffix} privacy={str(privacy).lower()} {states}"
    )
    if isinstance(payload, dict):
        sections = ('buyerInfoVO', 'addressInfo', 'module', 'data', 'result',
                    'components', 'bizOrderInfoList')
        message += ' sections=' + ','.join(key for key in sections if key in payload)
    if buyer_info_complete(info) and not privacy:
        logger.info(message)
    else:
        logger.warning(message)


def parse_privacy_address(data):
    """从闲鱼隐私地址响应中提取字段；只匹配明确的收件字段键。"""
    result = {"receiver_name": "", "receiver_phone": "", "receiver_address": ""}
    seen = 0

    def walk(node):
        nonlocal seen
        seen += 1
        if seen > 1000:
            return
        if isinstance(node, dict):
            for key, target in (
                ("receiverName", "receiver_name"), ("receiver_name", "receiver_name"),
                ("receiverMobile", "receiver_phone"), ("receiverPhone", "receiver_phone"),
                ("receiver_phone", "receiver_phone"), ("mobile", "receiver_phone"),
                ("fullAddress", "receiver_address"), ("receiverAddress", "receiver_address"),
                ("receiver_address", "receiver_address"),
            ):
                value = node.get(key)
                if isinstance(value, (str, int)) and str(value).strip() and not result[target]:
                    result[target] = str(value).strip()
            if not result["receiver_address"]:
                parts = [str(node.get(key) or "").strip() for key in
                         ("province", "city", "district", "detailAddress")]
                address = " ".join(part for part in parts if part)
                if address:
                    result["receiver_address"] = address
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return result


def merge_buyer_info(target, incoming):
    """合并平台的非空字段；空响应不擦掉已知字段，明文不被掩码倒覆盖。"""
    for key, _ in BUYER_FIELDS:
        value = incoming.get(key)
        if field_state(value) == 'missing':
            continue
        if field_state(target.get(key)) == 'present' and field_state(value) == 'masked':
            continue
        target[key] = str(value).strip()
    if incoming.get('is_privacy'):
        target['is_privacy'] = True
    return target
