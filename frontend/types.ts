
// API Response Bases
export interface ApiResponse {
  success?: boolean;
  message?: string;
  msg?: string;
}

export interface PaginatedResponse<T> {
  success: boolean;
  data: T[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
  // 各状态的全量条数，不受分页和筛选影响
  status_counts?: Record<string, number>;
}

// Auth
export interface AccountProfile { user_id: number; username: string; email: string }
export interface CredentialResult extends ApiResponse {
  success: boolean;
  error_code?: string;
  request_id?: string;
  reauth_required?: boolean;
  profile?: AccountProfile;
  retry_after_seconds?: number;
}
export interface LoginResponse {
  success: boolean;
  token?: string;
  message?: string;
  user_id?: number;
  username?: string;
  is_admin?: boolean;
  /** true 表示连续登录失败已达阈值，下一次登录必须携带图形验证码 */
  captcha_required?: boolean;
}

// Accounts
export interface AccountDetail {
  cookie_configured?: boolean;
  cookie_length?: number;
  login_password_configured?: boolean;
  id: string;
  value?: string; // cookie value from backend
  cookie?: string; // alias for value
  enabled: boolean;
  auto_confirm: boolean;
  remark?: string;
  note?: string; // alias for remark
  pause_duration?: number;
  // 登录信息
  username?: string;
  login_password?: string;
  show_browser?: boolean;
  // Frontend helpers
  nickname?: string;
  avatar_url?: string;
  location?: string;
  bio?: string;
  followers?: number;
  following?: number;
  profile_updated_at?: string;
  runtime_state?: 'running' | 'connecting' | 'need_relogin' | 'stopped' | 'cancelled' | 'failed';
  // AI设置
  ai_enabled?: boolean;
  max_discount_percent?: number;
  max_discount_amount?: number;
  max_bargain_rounds?: number;
  custom_prompts?: string;
}

// Orders
export type OrderStatus = 
  | 'processing'      
  | 'pending_ship'    
  | 'shipped'         
  | 'completed'       
  | 'cancelled'       
  | 'refunding';

export interface Order {
  id: string;
  order_id: string;
  cookie_id: string;
  item_id: string;
  item_title?: string;
  item_image?: string;
  item_price?: string;
  buyer_id: string;
  buyer_nick?: string;
  buyer_info_private?: boolean;
  quantity: number;
  amount: string;
  buy_num?: number;
  auction_price?: string;
  confirm_fee?: string;
  refund_fee?: string;
  post_fee?: string;
  status: OrderStatus;
  order_status?: OrderStatus;
  receiver_name?: string;
  receiver_phone?: string;
  receiver_address?: string;
  created_at?: string;
  updated_at?: string;
}

// 快捷短语：人工客服常用话术
export interface QuickPhrase {
  id: number;
  category: string;
  title: string;
  content: string;
  image_url?: string;
  sort_order: number;
  enabled: boolean;
  use_count: number;
  created_at?: string;
  updated_at?: string;
}

// Cards
export interface Card {
  id: number;
  name: string;
  type: 'api' | 'text' | 'data' | 'image';
  description?: string;
  enabled: boolean;
  // 文本类型
  text_content?: string;
  // 批量数据类型
  data_content?: string;
  // API 类型配置
  api_config?: {
    url: string;
    method: 'GET' | 'POST';
    timeout?: number;
    headers?: string;
    params?: string;
  };
  // 图片类型
  image_url?: string;
  // 通用配置
  delay_seconds?: number;
  // 发货详情文案：开关、文案内容（{参数}/{分隔符}/{图片N}）、图片映射（N -> url）
  delivery_template?: string;
  delivery_template_enabled?: boolean;
  delivery_template_images?: Record<string, string>;
  created_at: string;
  updated_at: string;
}

/** 批量卡密每发出一行生成一条已发货记录。 */
export interface CardShipment {
  id: number;
  card_id?: number;
  card_name: string;
  content: string;
  order_id: string;
  item_id: string;
  buyer_id: string;
  cookie_id: string;
  shipped_at: string;
}

// Items
export interface Item {
  id: string | number;
  cookie_id: string;
  item_id: string;
  item_title?: string;
  item_description?: string;
  item_detail?: string;
  detail_source?: 'unknown' | 'manual' | 'platform';
  detail_status?: 'missing' | 'fetching' | 'ready' | 'failed' | 'blocked';
  detail_updated_at?: number | null;
  detail_retry_at?: number | null;
  detail_failure?: string;
  item_price?: string;
  item_image?: string; // Inferred from common usage, though not explicitly in list model sometimes
  item_category?: string;
  is_multi_spec?: number | boolean;
  multi_quantity_delivery?: number | boolean;
  /** 仅经过在售分组同步确认的商品为 on_sale；资料补录为 unknown。 */
  listing_status?: 'on_sale' | 'off_shelf' | 'unknown';
  /** 最后一次被闲鱼接口返回的时间，用于在误判时提供依据。 */
  last_seen_at?: string;
  created_at?: string;
  updated_at?: string;
}

export interface ProductVariantBinding {
  id?: number;
  binding_id?: number;
  display_name: string;
  spec_text: string;
  spec_payload?: Record<string, string>;
  canonical_spec_key?: string;
  platform_sku_id: string;
  card_id: number;
  card_name?: string;
  card_type?: Card['type'];
  card_enabled?: boolean;
  stock_count?: number | null;
  delivery_count: number;
  delivery_times?: number;
  enabled: boolean;
  binding_enabled: boolean;
  source?: string;
}

export interface ItemDeliveryConfigSummary {
  cookie_id: string;
  item_id: string;
  enabled: boolean;
  is_multi_spec: boolean;
  variant_count: number;
  configured_count: number;
  complete: boolean;
  delivery_times: number;
}

export interface ItemDeliveryConfig extends ItemDeliveryConfigSummary {
  configured: boolean;
  variants: ProductVariantBinding[];
}

export type ProductPublishStatus = 'draft' | 'published' | 'failed' | string;

export interface ProductMaterial {
  id: number;
  user_id: number;
  cookie_id: string;
  rule_id?: number;
  source_item_id: string;
  title: string;
  description: string;
  category: string;
  price?: number;
  images: string[];
  source_url: string;
  short_url: string;
  delivery_content: string;
  publish_status: ProductPublishStatus;
  published_item_id: string;
  publish_trace_code: string;
  auto_card_id?: number;
  created_at?: string;
  updated_at?: string;
}

export interface ProductFilterRule {
  id: number;
  user_id: number;
  cookie_id: string;
  name: string;
  include_keywords: string[];
  exclude_keywords: string[];
  min_price?: number;
  max_price?: number;
  category: string;
  daily_limit: number;
  enabled: boolean;
  today_count: number;
  total_count: number;
  counter_date?: string;
  last_run_at?: string;
  last_run_status?: string;
  created_at?: string;
  updated_at?: string;
}

export interface ProductDeleteRule {
  id: number;
  user_id: number;
  cookie_id: string;
  name: string;
  min_publish_days: number;
  daily_limit: number;
  skip_reply_activity: boolean;
  skip_order_activity: boolean;
  enabled: boolean;
  execution_mode: 'dry_run';
  last_run_at?: string;
  last_run_status?: string;
  created_at?: string;
  updated_at?: string;
}

export interface AutomationTaskRun {
  id: number;
  user_id: number;
  cookie_id?: string;
  task_type: string;
  rule_id?: number;
  execution_mode: string;
  checked_count: number;
  matched_count: number;
  changed_count: number;
  failed_count: number;
  summary: string;
  details: Array<Record<string, unknown>>;
  error_message?: string;
  created_at: string;
}

export interface ProductAutomationResult {
  run_id: number;
  summary: string;
  details?: Array<Record<string, unknown>>;
}

export interface ProductDeletePreview extends ProductAutomationResult {
  mode: 'dry_run';
  candidates: Array<{
    item_id: string;
    item_title?: string;
    created_at?: string;
    age_days?: number;
    reason: string;
  }>;
  skipped: Array<{
    item_id: string;
    item_title?: string;
    created_at?: string;
    age_days?: number;
    reason: string;
  }>;
}

// Rules
export interface ShippingRule {
  id: string;
  name: string;
  item_keyword: string; // Matches item title
  cookie_id?: string;
  item_id?: string;
  item_title?: string;
  card_group_id: number; // ID from Card list
  card_group_name?: string; // UI helper
  priority: number;
  enabled: boolean;
  delivery_times?: number;
}

export interface DeliveryBlockRule {
  rule_code: string;
  rule_name: string;
  rule_description: string;
  enabled: boolean;
  priority: number;
  block_reason: string;
  auto_close_order: boolean;
  only_card_after_close: boolean;
  excluded_item_ids: string[];
  config: Record<string, unknown>;
}

export interface PersonalBlacklistEntry {
  id: number;
  owner_id: number;
  account_id?: string;
  buyer_id: string;
  buyer_nick: string;
  item_id?: string;
  reason: string;
  is_enabled: boolean;
  created_at?: string;
  updated_at?: string;
}

export type NotificationChannelType =
  | 'dingtalk'
  | 'feishu'
  | 'bark'
  | 'email'
  | 'webhook'
  | 'wechat'
  | 'telegram';

export interface NotificationChannel {
  configured_fields?: string[];
  id: string;
  name: string;
  type: NotificationChannelType;
  config: Record<string, unknown>;
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface MessageNotification {
  id: string;
  cookie_id: string;
  channel_id: number;
  channel_name: string;
  channel_type?: NotificationChannelType;
  channel_enabled?: boolean;
  name?: string | null;
  event_types: string[];
  enabled: boolean;
}

export type NotificationPriority = 'error' | 'warning' | 'info';

export interface NotificationEventDefinition {
  id: string;
  label: string;
  priority: NotificationPriority;
  severity: 'error' | 'warning' | 'info';
  description: string;
  impact: string;
  action: string;
  actions: string[];
  actionable: boolean;
  default_subscribed: boolean;
}

export interface NotificationPriorityDefinition {
  id: NotificationPriority;
  label: string;
}

export interface RiskControlLog {
  id: number;
  cookie_id: string;
  cookie_name?: string;
  event_type: string;
  event_description?: string;
  processing_result?: string;
  processing_status: 'processing' | 'success' | 'failed' | string;
  error_message?: string;
  severity?: 'error' | 'warning' | 'info';
  action_code?: string;
  recommended_action?: string;
  title?: string;
  impact?: string;
  action_codes?: string[];
  occurrences?: number;
  diagnostic_id?: string | null;
  created_at: string;
  updated_at?: string;
}

export interface SystemLog {
  timestamp: string;
  level: string;
  source: string;
  function?: string;
  line?: number;
  message: string;
}

export type MessageFilterType = 'skip_reply' | 'skip_notify';

export interface MessageFilter {
  id: number;
  cookie_id: string;
  keyword: string;
  filter_type: MessageFilterType;
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface AutoReplyLog {
  id: number;
  cookie_id: string;
  chat_id?: string;
  item_id?: string;
  source_message_id?: string;
  sender_user_id?: string;
  sender_user_name?: string;
  source_message?: string;
  process_status: 'success' | 'skipped' | 'failed' | string;
  decision_reason?: string;
  reply_strategy: 'keyword' | 'ai' | 'default' | 'api' | 'none' | string;
  matched_keyword?: string;
  reply_text?: string;
  error_message?: string;
  send_status: 'success' | 'failed' | 'unknown' | string;
  message_source?: 'push' | 'history';
  config_updated_at?: string;
  text_send_status?: string;
  image_send_status?: string;
  remaining_pause_seconds?: number;
  created_at?: string;
  updated_at?: string;
}

export interface ChatAccount {
  authPaused?: boolean;
  recoveryMessage?: string;
  remark?: string;
  nickname?: string;
  accountId: string;
  displayName: string;
  avatarUrl?: string;
  connected: boolean;
  xianyuUserId?: string;
}

export interface ChatConversation {
  cid: string;
  rawCid: string;
  otherUserId: string;
  otherUserName: string;
  otherUserAvatar?: string;
  itemId?: string;
  itemTitle?: string;
  itemImage?: string;
  lastMessageSummary: string;
  lastMessageTime: number;
  unreadCount: number;
}

export interface ChatMessage {
  event_type?: string | null;
  event_actor_role?: string;
  event_label?: string;
  event_order_match?: string;
  receiverId?: string;
  event_recipient_role?: string;
  event_direction?: 'inbound' | 'outbound' | 'platform' | 'unknown' | '';
  event_from?: string;
  event_to?: string;
  event_order_id?: string;
  event_order_candidates?: string[];
  card?: {kind: 'order' | 'item'; orderId?: string; itemId?: string} | null;
  messageId: string;
  senderId: string;
  senderName: string;
  isSelf: boolean;
  type: 'text' | 'image' | 'system' | 'card';
  text: string;
  images: string[];
  time: number;
}

export interface ReplyRule {
  id: string;
  keyword: string;
  reply_content: string;
  match_type: 'exact' | 'fuzzy';
  enabled: boolean;
}

// Stats
export interface AdminStats {
  total_users: number;
  total_cookies: number;
  active_cookies: number;
  total_cards: number;
  total_keywords: number;
  total_orders: number;
}

// 用户管理（管理员专用）。内置 admin 是唯一管理员，其余注册用户均为普通用户。
export interface AdminUser {
  id: number;
  username: string;
  email: string;
  is_active?: boolean;
  cookie_count?: number;
  card_count?: number;
  created_at?: string;
  updated_at?: string;
}

export interface OrderAnalytics {
  revenue_stats: {
    total_amount: number;
    total_orders: number;
    avg_amount?: number;
    unique_buyers?: number;
    unique_items?: number;
    // 确认收货后卖家实收，退款订单为 0
    confirmed_amount?: number;
    refunded_amount?: number;
    total_items_sold?: number;
    // 全部状态的口径，与订单页数字一致
    all_orders?: number;
    all_amount?: number;
  };
  daily_stats: Array<{
    date: string;
    amount: number;
    order_count?: number;
    confirmed_amount?: number;
    refunded_amount?: number;
  }>;
  item_stats?: Array<{
    item_id: string;
    order_count: number;
    total_amount: number;
    avg_amount: number;
  }>;
}

// Settings
export interface SystemSettings {
  ai_model?: string;
  ai_api_key?: string;
  /** 后端不返回密钥本身，只告诉前端是否已经配置 */
  ai_api_key_configured?: boolean;
  ai_api_url?: string;
  default_reply?: string;
  registration_enabled?: boolean;
  show_default_login_info?: boolean;
  // 注册是否必须填邮箱验证码。没有可用的邮件渠道时关掉，否则注册会卡在收不到验证码
  email_verification_enabled?: boolean;
  // 定时从卖家端拉取订单，默认开启
  order_sync_enabled?: boolean;
  order_sync_interval?: number;
  // 商品擦亮，默认关闭
  auto_polish_enabled?: boolean;
  auto_polish_interval?: number;
  // 对买家产生实际动作，默认关闭
  auto_rate_enabled?: boolean;
  // 订单页评价框的预填文案，避免每单重复手打
  auto_rate_template?: string;
  /** 确认收货致谢文案，全账号共用 */
  auto_thanks_template?: string;
  /** 买家互动兜底轮询间隔（秒）。确认收货已由消息事件即时触发，这里只是兜底。 */
  buyer_interaction_interval?: string | number;
  auto_flower_enabled?: boolean;
  [key: string]: any;
}

export interface AIReplySettings {
  ai_enabled: boolean;
  model_name: string;
  api_key: string;
  api_key_configured?: boolean;
  base_url: string;
  user_agent?: string;
  max_discount_percent: number;
  max_discount_amount?: number;
  max_bargain_rounds: number;
  context_enabled: boolean;
  context_message_limit: number;
  context_expire_minutes: number;
  custom_prompts: string;
}

// Default Reply
export interface DefaultReply {
  cookie_id: string;
  enabled: boolean;
  reply_content: string;
  reply_once: boolean;
  reply_image_url?: string;
  updated_at?: string;
  runtime?: { connected: boolean; paused_conversations: number; remaining_pause_seconds: number };
}

export interface AccountRecoveryStatus {
  cookie_id: string;
  blocked: boolean;
  remaining_seconds: number;
  consecutive_hits: number;
  reason: string;
  verification_type: 'none' | 'login' | 'message' | 'recovery' | 'risk_control';
  verification_message: string;
  verification_url?: string;
  latest_event: string;
  latest_event_at?: string | null;
  recovery_state: 'none' | 'required' | 'verifying' | 'checking' | 'recovered';
  event_type: string;
  severity: 'error' | 'warning' | 'info';
  event_label: string;
  event_impact: string;
  recommended_action: string;
  verification_scope: 'login' | 'message' | 'none';
  action_codes: string[];
  auth_paused: boolean;
  requires_action: boolean;
  requires_manual_verification: boolean;
  can_start_verification: boolean;
  can_check_connection?: boolean;
  recovery_action?: 'none' | 'qr_login' | 'sync_web_session' | 'check_connection' | 'view_diagnostic';
  failure_category: string;
  recovery_message: string;
  recovery_detail: string;
  diagnostic_id?: string | null;
}

export interface CaptchaDiagnosticEntry {
  seq: number;
  created_at: number;
  level: string;
  kind: string;
  message: string;
}

export interface CaptchaDiagnostic {
  diagnostic_id: string;
  status: 'processing' | 'completed' | 'failed' | 'cancelled';
  stage: string;
  failure_category: string;
  error_code: string;
  close_reason: string;
  created_at: number;
  dropped_count: number;
  event_count: number;
  entries: CaptchaDiagnosticEntry[];
  next_seq: number;
  has_more: boolean;
}
