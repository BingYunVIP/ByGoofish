import type { ChatConversation, ChatMessage, Order } from '../../types';
export type ConversationState = {
  pinned?: boolean;
  viewed_at?: number;
  viewed_message_time?: number;
  viewed_message_summary?: string;
  viewed_unread_count?: number;
  needs_reply?: boolean;
  note?: string;
};
export type WorkspaceConversation = ChatConversation & { workspace?: ConversationState };
export type OutgoingMessage = ChatMessage & {
  workspace_scope?: string;
  client_request_id?: string;
  send_status?: 'sending' | 'sent' | 'failed' | 'unknown';
  error?: string;
};
export type WorkspaceOrder = Order & {
  order_status?: string;
  system_shipped?: number;
  chat_id?: string;
  interaction_attempts?: { action: string; status: string }[];
  delivery_attempt?: { status: string; reviewable: boolean; reserved_count: number };
};
export interface ConversationContext {
  owner_id: number;
  buyer: { id: string; nickname: string; note: string; nickname_source?: string; avatar_url?: string };
  item: { item_id?: string; item_title?: string; item_image?: string; item_price?: string };
  orders: WorkspaceOrder[];
  candidate_orders: WorkspaceOrder[];
  handover: { paused: boolean; expires_at: number; duration_minutes: number };
  features: { auto_rate_enabled: boolean; auto_flower_enabled: boolean };
  workspace: ConversationState;
  outgoing?: {
    request_id: string;
    kind: string;
    status: string;
    message_id: string;
    created_at: number;
  }[];
  source: string;
}
