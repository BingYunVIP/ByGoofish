"""Single source of truth for operational issues and notification events."""

import json
from typing import Any, Dict, List, Optional

SEVERITY_ORDER = ("error", "warning", "info")
SEVERITY_LABELS = {"error": "错误", "warning": "警告", "info": "通知"}
DEFAULT_EVENT_TYPE = "transient_warning"

# `action` is stable UI guidance, not a free-form instruction inferred from logs.
EVENT_DEFINITIONS: List[Dict[str, Any]] = [
    {"id":"login_verification_required", "label":"登录安全验证", "priority":"error", "severity":"error",
     "description":"扫码或登录阶段明确要求本人完成安全验证", "impact":"登录凭证尚未完成验证，账号暂时不可用",
     "action":"使用同一闲鱼账号完成官方登录或重新扫码；扫码确认后等待连接检查", "actionable":True,"default_subscribed":True},
    {"id":"login_session_expired", "label":"登录会话已失效", "priority":"error", "severity":"error",
     "description":"平台明确返回登录会话过期", "impact":"当前凭证不能继续登录", "action":"使用同一账号重新扫码登录", "actionable":True,"default_subscribed":True},
    {"id":"message_verification_required", "label":"消息安全验证", "priority":"error", "severity":"error",
     "description":"消息令牌或消息会话明确要求本人验证", "impact":"后台暂时不能接收消息或自动处理新订单",
     "action":"在已登录的闲鱼官网打开「消息」并完成验证，再同步网页登录态并检查连接", "actionable":True,"default_subscribed":True},
    {"id":"credentials_invalid", "label":"凭证或令牌异常", "priority":"error", "severity":"error",
     "description":"凭证、签名令牌或账号数据检查失败，且未确认是人工验证", "impact":"认证或账号数据同步可能不可用",
     "action":"查看诊断详情；根据具体原因更新网页登录态或账号凭证", "actionable":True,"default_subscribed":True},
    {"id":"account_offline", "label":"账号消息连接中断", "priority":"error", "severity":"error",
     "description":"消息服务持续断开超过观察期，且不是已分类的人工验证事件", "impact":"后台无法监听新消息或及时处理新订单",
     "action":"查看账号诊断并检查消息连接", "actionable":True,"default_subscribed":True},
    {"id":"login_failed", "label":"账号密码登录失败", "priority":"error", "severity":"error",
     "description":"密码登录流程返回失败", "impact":"本次登录或凭证更新未完成", "action":"检查账号登录状态及凭证后再操作", "actionable":True,"default_subscribed":True},
    {"id":"delivery_failed", "label":"自动发货失败", "priority":"error", "severity":"error",
     "description":"订单自动发货未完成", "impact":"买家订单可能仍待发货", "action":"核对订单、库存和发货台账后按订单状态处理", "actionable":True,"default_subscribed":True},
    {"id":"delivery_timeout", "label":"发货超时告警", "priority":"error", "severity":"error",
     "description":"订单接近或达到发货时限", "impact":"订单履约可能逾期", "action":"打开订单核实平台状态和发货记录", "actionable":True,"default_subscribed":True},
    {"id":"delivery_confirm_failed", "label":"平台确认发货失败", "priority":"error", "severity":"error",
     "description":"发货提交后平台未确认成功", "impact":"平台订单状态可能与发货台账不一致", "action":"先核对订单和发货记录，不要直接重复发送", "actionable":True,"default_subscribed":True},
    {"id":"instance_restart_failed", "label":"账号实例重启失败", "priority":"error", "severity":"error",
     "description":"凭证更新后账号运行实例未能重启", "impact":"账号功能可能未加载新凭证", "action":"查看实例日志和账号运行状态", "actionable":True,"default_subscribed":True},
    {"id":"logistics_quote", "label":"物流报价异常", "priority":"warning", "severity":"warning",
     "description":"物流报价解析失败或需要人工确认", "impact":"本次物流报价结果不可用", "action":"核对原始报价内容和解析结果", "actionable":True,"default_subscribed":False},
    {"id":"buyer_interaction_failed", "label":"买家互动待核查", "priority":"error", "severity":"error",
     "description":"评价、求花或致谢未确认完成", "impact":"平台互动结果可能未知，重复操作可能造成重复触达",
     "action":"先核对订单互动记录，再决定是否需要手动处理", "actionable":True,"default_subscribed":True},
    {"id":"buyer_info_incomplete", "label":"订单买家信息未完整获取", "priority":"warning", "severity":"warning",
     "description":"订单接口未返回全部买家或收货信息", "impact":"发货或联系买家前仍需确认订单资料",
     "action":"打开订单详情核对平台当前返回的信息", "actionable":True,"default_subscribed":False},
    {"id":"transient_warning", "label":"平台暂时波动", "priority":"warning", "severity":"warning",
     "description":"平台限流、网络波动或认证重试正在退避，尚未确认需要本人操作", "impact":"当前请求可能延迟，系统会按策略重试",
     "action":"暂不重复操作；若状态持续，请查看账号诊断", "actionable":False,"default_subscribed":False},
    {"id":"login_recovered", "label":"登录认证已恢复", "priority":"info", "severity":"info",
     "description":"登录认证检查完成", "impact":"账号凭证已通过登录阶段检查", "action":"无需操作", "actionable":False,"default_subscribed":False},
    {"id":"message_connection_recovered", "label":"消息连接已恢复", "priority":"info", "severity":"info",
     "description":"消息令牌有效且消息服务注册确认成功", "impact":"后台消息监听已恢复", "action":"无需操作", "actionable":False,"default_subscribed":False},
    {"id":"delivery_success", "label":"自动发货成功", "priority":"info", "severity":"info",
     "description":"订单发货流程完成", "impact":"订单发货结果已记录", "action":"无需操作", "actionable":False,"default_subscribed":False},
    {"id":"buyer_message", "label":"买家消息", "priority":"info", "severity":"info",
     "description":"收到买家私聊消息", "impact":"有新的买家消息", "action":"按需在消息中心回复", "actionable":False,"default_subscribed":False},
]

EVENT_IDS = [item["id"] for item in EVENT_DEFINITIONS]
LOG_EVENT_DEFINITIONS = {
    "buyer_interaction": {"id":"buyer_interaction", "label":"买家互动记录", "severity":"info", "action":""},
    "buyer_info_collected": {"id":"buyer_info_collected", "label":"订单买家信息采集", "severity":"info", "action":""},
    "buyer_info_incomplete": {"id":"buyer_info_incomplete", "label":"订单买家信息未完整获取", "severity":"warning", "action":"查看订单详情并确认平台返回的收件信息"},
}
EVENT_ACTIONS = {
    "login_verification_required": ["qr_login"],
    "login_session_expired": ["qr_login"],
    "message_verification_required": ["sync_web_session", "check_connection"],
    "credentials_invalid": ["view_diagnostic", "edit_credentials", "qr_login", "check_connection"],
    "account_offline": ["view_diagnostic", "check_connection"],
    "login_failed": ["edit_credentials", "qr_login"],
    "delivery_failed": ["view_order", "view_delivery_ledger"],
    "delivery_timeout": ["view_order"],
    "delivery_confirm_failed": ["view_order", "view_delivery_ledger"],
    "instance_restart_failed": ["view_diagnostic", "check_connection"],
    "logistics_quote": ["view_diagnostic"],
    "buyer_interaction_failed": ["view_order", "view_delivery_ledger"],
    "buyer_info_incomplete": ["view_order"],
    "transient_warning": ["view_diagnostic"],
}


def get_event_definitions() -> List[Dict[str, Any]]:
    return [get_event(item["id"]) for item in EVENT_DEFINITIONS]


def get_priority_definitions() -> List[Dict[str, str]]:
    return [{"id": key, "label": SEVERITY_LABELS[key]} for key in SEVERITY_ORDER]


def get_event(event_id: str) -> Optional[Dict[str, Any]]:
    item = next((dict(item) for item in EVENT_DEFINITIONS if item["id"] == event_id), None)
    if item is None and event_id in LOG_EVENT_DEFINITIONS:
        item = dict(LOG_EVENT_DEFINITIONS[event_id])
        item.update({"priority":"info", "description":"内部业务结果记录", "impact":"", "actionable":False, "default_subscribed":False})
    if item:
        item["actions"] = list(EVENT_ACTIONS.get(event_id, []))
    return item


def get_log_event(event_id: str) -> Optional[Dict[str, Any]]:
    return get_event(event_id)


def resolve_event_type(event_type: str) -> str:
    if event_type not in EVENT_IDS:
        raise ValueError('未定义的通知事件')
    return event_type


def normalize_event_types(event_types: Any) -> Optional[List[str]]:
    if event_types is None:
        return [item['id'] for item in EVENT_DEFINITIONS if item['default_subscribed']]
    if isinstance(event_types, bytes):
        event_types = event_types.decode("utf-8", errors="ignore")
    if isinstance(event_types, str):
        text = event_types.strip()
        if not text:
            return [item['id'] for item in EVENT_DEFINITIONS if item['default_subscribed']]
        try:
            event_types = json.loads(text)
        except json.JSONDecodeError:
            event_types = [item.strip() for item in text.split(",") if item.strip()]
    if isinstance(event_types, (list, tuple, set)):
        selected = {str(item).strip() for item in event_types if str(item).strip()}
        unknown = sorted(selected - set(EVENT_IDS))
        if unknown:
            raise ValueError(f"不支持的通知事件类型: {', '.join(unknown)}")
        return [event_id for event_id in EVENT_IDS if event_id in selected]
    raise ValueError("通知事件类型必须是数组或 JSON 数组")


def parse_event_types(raw: Any) -> Optional[List[str]]:
    try:
        return normalize_event_types(raw)
    except ValueError:
        return []


def serialize_event_types(event_types: Any) -> Optional[str]:
    normalized = normalize_event_types(event_types)
    return None if normalized is None else json.dumps(normalized, ensure_ascii=False)


def rule_accepts_event(event_types: Any, event_type: str) -> bool:
    if event_type not in EVENT_IDS:
        return False
    selected = parse_event_types(event_types)
    return event_type in (selected or [])
