import {
  Ban,
  CheckCircle2,
  ChevronRight,
  CreditCard,
  Flower2,
  HeartHandshake,
  MessageCircleHeart,
  PackageCheck,
  RotateCcw,
  ShoppingBag,
  Star,
  Truck,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { OutgoingMessage } from './workspaceTypes';
import MessageSendStatus from './MessageSendStatus';

type EventView = { title: string; icon: LucideIcon };
const EVENTS: Record<string, EventView> = {
  order_pending_payment: { title: '订单已拍下 · 待付款', icon: ShoppingBag },
  order_paid_pending_ship: { title: '买家已付款 · 待发货', icon: CreditCard },
  seller_shipped: { title: '卖家已发货', icon: Truck },
  buyer_received: { title: '买家已确认收货', icon: PackageCheck },
  seller_received: { title: '交易已确认收货', icon: PackageCheck },
  shipping_reminder: { title: '发货提醒', icon: Truck },
  review_requested: { title: '交易评价提醒', icon: Star },
  review_completed: { title: '评价已完成', icon: CheckCircle2 },
  receipt_thanks: { title: '收货致谢', icon: HeartHandshake },
  flower_requested: { title: '小红花请求', icon: Flower2 },
  flower_sent_confirmation: { title: '已向买家发起求花', icon: Flower2 },
  flower_received: { title: '收到小红花', icon: MessageCircleHeart },
  refund_requested: { title: '退款申请', icon: RotateCcw },
  refund_completed: { title: '退款完成', icon: RotateCcw },
  order_closed: { title: '订单已关闭', icon: Ban },
  order_status: { title: '交易状态通知', icon: CreditCard },
};

export default function TransactionEvent({ message, orderCount, onOpen, onCheck }: {
  message: OutgoingMessage;
  orderCount: number;
  onOpen: (orderId?: string) => void;
  onCheck: () => void;
}) {
  const event = EVENTS[message.event_type || ''];
  if (!event) return null;
  const Icon = event.icon;
  const orderId = message.event_order_id || message.card?.orderId;
  const direction = message.event_direction || 'unknown';
  const align = direction === 'outbound'
    ? 'justify-end'
    : direction === 'inbound' || direction === 'platform'
      ? 'justify-start'
      : 'justify-center';
  const bubble = direction === 'outbound' ? 'own' : direction === 'platform' ? 'platform' : '';
  const directionLabel = message.event_label || (message.event_from && message.event_to
    ? `${message.event_from} → ${message.event_to}`
    : direction === 'unknown'
      ? `${message.event_actor_role || '交易事件'} · 方向待核实`
      : direction === 'outbound'
        ? '我方 → 买家'
        : direction === 'inbound'
          ? '买家 → 我方'
          : '闲鱼 → 我方');
  return (
    <div className={`flex w-full ${align}`}>
      <div className={`chat-event ${bubble}`} aria-label={event.title} data-event-direction={direction}>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs font-bold">
          <Icon size={16} aria-hidden="true" />
          <span>{event.title}</span>
          <span className="min-w-0 break-words text-[10px] font-normal text-[var(--text-soft)]">
            {directionLabel}
          </span>
        </div>
        <p className="mt-2 whitespace-pre-wrap text-xs leading-5 text-[var(--text-muted)]">{message.text}</p>
        {(orderId || orderCount > 0) && (
          <button type="button" className="ios-btn-secondary mt-3 inline-flex items-center gap-1 rounded-md px-3 py-2 text-xs"
            onClick={() => onOpen(orderId)}>
            {orderId ? '查看关联订单' : '选择关联订单'}
            <ChevronRight size={12} aria-hidden="true" />
          </button>
        )}
        <MessageSendStatus status={message.send_status} onCheck={onCheck} />
      </div>
    </div>
  );
}
