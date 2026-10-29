/** Operator identities are isolated across users, shops and conversations. */
export const draftKey = (owner, account, cid) =>
  `bygoofish-chat-draft-v1:${owner}:${account}:${cid}`;

export function mergeMessages(previous, incoming) {
  const messages = new Map();
  for (const message of [...previous, ...incoming]) {
    const key =
      message.messageId ||
      `${message.senderId}:${message.time}:${message.type}:${message.text}:${message.images.join()}`;
    messages.set(key, message);
  }
  return [...messages.values()].sort((a, b) => a.time - b.time);
}

/** Local echoes stay visible until their platform id appears in official history. */
export function messageTimeline(history, outgoing) {
  const platform = new Map(history.filter((m) => m.messageId).map((m) => [m.messageId, m]));
  const reconciled = new Set();
  const local = [];
  for (const message of outgoing) {
    const official = message.messageId !== message.client_request_id && platform.get(message.messageId);
    if (official) {
      reconciled.add(message.messageId);
      local.push({ ...message, ...official, client_request_id: message.client_request_id,
        send_status: 'sent' });
    } else local.push(message);
  }
  return mergeMessages(history.filter((m) => !reconciled.has(m.messageId)), local);
}

export function mergeOutgoing(previous, records, scope, account) {
  const byId = new Map(previous.filter((m) => m.workspace_scope === scope)
    .map((m) => [m.client_request_id, m]));
  for (const record of records) {
    const before = byId.get(record.request_id);
    byId.set(record.request_id, {
      ...before, workspace_scope: scope, messageId: record.message_id || before?.messageId || record.request_id,
      client_request_id: record.request_id, senderId: account, senderName: '', isSelf: true,
      type: record.kind === 'image' ? 'image' : 'text',
      text: before?.text ?? (record.kind === 'image' ? '[图片]' : '文字消息（正文待同步）'),
      images: before?.images || [], time: before?.time || record.created_at * 1000,
      send_status: before?.send_status === 'sent' ? 'sent' : record.status,
    });
  }
  return [...byId.values()].sort((a, b) => a.time - b.time);
}

export function unread(conversation) {
  const state = conversation.workspace;
  return state &&
    Number(state.viewed_message_time) >= conversation.lastMessageTime &&
    state.viewed_message_summary === conversation.lastMessageSummary &&
    Number(state.viewed_unread_count) >= conversation.unreadCount
    ? 0
    : conversation.unreadCount;
}

/** The batch transport may succeed while the actual order delivery fails. */
export function shipOrderResult(response) {
  const result = response?.results?.[0];
  return {
    success: response?.success === true && result?.success === true,
    message: result?.message || '未取得本订单的成功结果，请核查发货记录后再操作',
  };
}

/** Both the sidebar and forwarding use exactly this filtered, stable ordering. */
export function selectConversations(conversations, { bounds, query = '', filter = 'all' }) {
  const needle = query.trim().toLocaleLowerCase();
  const milliseconds = value => Number(value) < 1e12 ? Number(value) * 1000 : Number(value);
  return conversations.filter(v => {
    const time = milliseconds(v.lastMessageTime);
    return Number.isFinite(time) && time > 0 && (!bounds ||
      (time >= Date.parse(bounds.startTime) && time < Date.parse(bounds.endTime)));
  }).filter(v => !needle || [v.otherUserName, v.otherUserId, v.itemTitle, v.lastMessageSummary]
    .some(value => String(value || '').toLocaleLowerCase().includes(needle)))
    .filter(v => filter === 'unread' ? unread(v) > 0 : filter === 'pending' ? v.workspace?.needs_reply : true)
    .sort((a, b) => Number(Boolean(b.workspace?.pinned)) - Number(Boolean(a.workspace?.pinned)) ||
      milliseconds(b.lastMessageTime) - milliseconds(a.lastMessageTime) || a.cid.localeCompare(b.cid));
}

/** Recipient picker data intentionally excludes notes, orders and message bodies. */
export const forwardRecipients = conversations => conversations.map(v => ({
  cid: v.cid, otherUserId: v.otherUserId, otherUserName: v.otherUserName,
  itemTitle: v.itemTitle || '', lastMessageTime: v.lastMessageTime,
}));

/** A reconciled local echo is actionable only with its real platform record. */
export const confirmedMessage = message => Boolean(message.messageId &&
  message.messageId !== message.client_request_id && (!message.send_status || message.send_status === 'sent'));
