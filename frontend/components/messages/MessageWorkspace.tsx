import { forwardRecipients } from '../../lib/chatWorkspace.mjs';
import { createPortal } from 'react-dom';
import { useModalFocus } from '../../lib/useModalFocus';
import HandoverBanner from './HandoverBanner';
import ConversationSearch from './ConversationSearch';
import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import {
  ArrowLeft,
  ArrowDown,
  ChevronRight,
  Clock,
  Inbox,
  Loader2,
  MessageCircle,
  Package,
  PanelRight,
  Pin,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  UserRound,
  Wifi,
  X,
  PauseCircle,
  MoreHorizontal,
} from 'lucide-react';
import { PageTabs } from '../ui';
import { accountDisplayName } from '../../lib/accountDisplay';
import { MessageText, type OfficialEmoji } from './OfficialEmoji';
import { MessageMenu, ChatAreaMenu, ForwardDialog, type AreaMenuTarget, type MenuTarget } from './MessageActions';
import type { ChatMessage } from '../../types';
import type { OutgoingMessage } from './workspaceTypes';
import { get } from '../../lib/request';
import { notify } from '../../services/feedback';
import { formatOrderTime } from '../../lib/orderTime';
import { StatusBadge } from '../OrderDialogs';
import ChatComposer from './ChatComposer';
import OrderWorkspaceActions from './OrderWorkspaceActions';
import TransactionEvent from './TransactionEvent';
import MessageSendStatus from './MessageSendStatus';
import useChatWorkspace, { unread } from './useChatWorkspace';
import './workspace.css';
const when = (v: number) => {
  if (!v) return '';
  const d = new Date(v < 1e12 ? v * 1000 : v);
  return d.toDateString() === new Date().toDateString()
    ? d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
};
function Avatar({ name, url }: { name: string; url?: string }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [url]);
  return (
    <span className="chat-avatar overflow-hidden" aria-hidden="true">
      {url && !failed ? (
        <img
          src={url.startsWith('//') ? `https:${url}` : url}
          className="h-full w-full object-cover"
          alt=""
          referrerPolicy="no-referrer"
          onError={() => setFailed(true)}
        />
      ) : (
        name.slice(0, 1) || <UserRound size={18} />
      )}
    </span>
  );
}
export default function MessageWorkspace({
  isActive = true,
  onFilters,
}: {
  isActive?: boolean;
  onFilters: () => void;
}) {
  const w = useChatWorkspace(isActive),
    [details, setDetails] = useState(false),
    [messageSearch, setMessageSearch] = useState(''),
    [searchOpen, setSearchOpen] = useState(false),
    [image, setImage] = useState('');
  const [selectedOrder, setSelectedOrder] = useState(''),
    [note, setNote] = useState(''),
    [newCount, setNewCount] = useState(0);
  const scroller = useRef<HTMLDivElement>(null),
    shell = useRef<HTMLDivElement>(null),
    nearBottom = useRef(true),
    lastMessage = useRef(''),
    listScroll = useRef<HTMLDivElement>(null),
    listPosition = useRef(0);
  const drawerRef = useRef<HTMLElement>(null),
    imageRef = useRef<HTMLDivElement>(null);
  const [menu,setMenu] = useState<MenuTarget | null>(null);
  const [areaMenu,setAreaMenu] = useState<AreaMenuTarget | null>(null);
  const closeAreaMenu=useCallback(()=>setAreaMenu(null),[]);
  const [forwarding,setForwarding] = useState<ChatMessage>();
  const [capabilities,setCapabilities] = useState<Record<string,{supported:boolean;reason:string}>>({});
  const [emojiState,setEmojiState] = useState<{ account: string; items: OfficialEmoji[] }>({account:'',items:[]});
  const emojis = emojiState.account === w.accountId ? emojiState.items : [];
  const closeMenu = useCallback(() => setMenu(null), []);
  useEffect(() => { setMenu(null);setAreaMenu(null);setForwarding(undefined); }, [w.accountId,w.cid,isActive]);
  useEffect(() => {
    if (!isActive || !w.accountId) return;
    let active = true;
    setCapabilities({});
    get<Record<string,{supported:boolean;reason:string}>>(`/chat/capabilities/${encodeURIComponent(w.accountId)}`).then(result => { if (active) setCapabilities(result); }).catch(() => {});
    get<{items: OfficialEmoji[]}>(`/chat/emojis/${encodeURIComponent(w.accountId)}`,undefined,{timeout:60000})
      .then(result => { if (active) setEmojiState({ account:w.accountId,items:result.items || [] }); }).catch(() => {});
    return () => { active = false; };
  }, [isActive,w.accountId]);
  const openMenu = (event: React.MouseEvent<HTMLElement>, message: OutgoingMessage, fromButton=false) => {
    event.preventDefault();event.stopPropagation();setAreaMenu(null);
    const selection = window.getSelection();
    let selected = '';
    if (selection && selection.rangeCount && event.currentTarget.contains(selection.anchorNode) && event.currentTarget.contains(selection.focusNode)) {
      const fragment = selection.getRangeAt(0).cloneContents();
      fragment.querySelectorAll('img[alt]').forEach(image => image.replaceWith(document.createTextNode(image.getAttribute('alt') || '')));
      selected = fragment.textContent || '';
    }
    const box = event.currentTarget.getBoundingClientRect();
    setMenu({message,x:!fromButton && Number.isFinite(event.clientX)?event.clientX:box.left,y:!fromButton && Number.isFinite(event.clientY)?event.clientY:box.bottom,selection:selected,anchor:event.currentTarget});
  };
  const locateReference = async (id: string) => {
    if (!id) return;
    const selection = selectionRef.current;
    for (let page=0;page<=5;page++) {
      const node = scroller.current?.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(id)}"]`);
      if (node) { node.scrollIntoView({block:'center',behavior:'smooth'});node.animate([{outline:'2px solid var(--brand)'},{outline:'2px solid transparent'}],{duration:1800});return; }
      if (page===5) break;
      const loaded = await w.loadMessages(true);
      if (selection!==selectionRef.current) return;
      if (!loaded?.length) break;
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }
    notify('原消息未在已加载的记录中找到，可继续加载更早消息', 'warning');
  };
  const [workspaceWidth, setWorkspaceWidth] = useState(0);
  useEffect(() => {
    if (!shell.current) return;
    const observer = new ResizeObserver((entries) =>
      setWorkspaceWidth(entries[0].contentRect.width)
    );
    observer.observe(shell.current);
    return () => observer.disconnect();
  }, []);
  useModalFocus(details && workspaceWidth < 1100, () => setDetails(false), drawerRef);
  useModalFocus(Boolean(image), () => setImage(''), imageRef);
  const accountName = accountDisplayName(w.account, '未选择账号'),
    c = w.conversation,
    ctx = w.context;
  useEffect(() => {
    setSelectedOrder('');
    setNote('');
    setMessageSearch('');
    setNewCount(0);
    nearBottom.current = true;
    lastMessage.current = '';
  }, [w.accountId, w.cid]);
  useEffect(() => {
    setNote(ctx?.buyer.note || '');
  }, [w.cid, ctx?.buyer.note]);
  useEffect(() => {
    const el = listScroll.current;
    if (el && w.pane === 'list') el.scrollTop = listPosition.current;
  }, [w.pane]);
  const resolvedMessage = (message: OutgoingMessage): OutgoingMessage => {
    if (!message.reply || message.reply.type!=='unknown') return message;
    const source = w.messages.find(other => other.messageId===message.reply!.messageId);
    return source ? { ...message, reply: { messageId:source.messageId,senderId:source.senderId,senderName:source.senderName,time:source.time,text:source.text,images:source.images,type:source.type } } : message;
  };
  const latest = w.messages[w.messages.length - 1],
    latestKey = latest ? `${latest.messageId}:${latest.time}` : '';
  useLayoutEffect(() => {
    if (!latestKey || latestKey === lastMessage.current) return;
    const initial = !lastMessage.current;
    lastMessage.current = latestKey;
    const el = scroller.current;
    if (el && (initial || nearBottom.current)) {
      el.scrollTop = el.scrollHeight;
      setNewCount(0);
    } else if (!initial) setNewCount((n) => n + 1);
  }, [latestKey]);
  useEffect(() => {
    if (
      !c ||
      document.hidden ||
      !isActive ||
      !latestKey ||
      !w.messagesReady ||
      w.messageLoading ||
      !nearBottom.current
    )
      return;
    const width = shell.current?.getBoundingClientRect().width || 0;
    if (w.pane === 'chat' || width >= 760) w.markViewed(c);
  }, [
    latestKey,
    w.messagesReady,
    w.messageLoading,
    c?.cid,
    c?.lastMessageTime,
    c?.lastMessageSummary,
    c?.unreadCount,
    w.pane,
    isActive,
    w.markViewed,
  ]);
  const orders = ctx?.orders || [],
    candidates = ctx?.candidate_orders || [],
    relatedOrderCount = orders.length + candidates.length,
    order =
      [...orders, ...candidates].find((o) => o.order_id === selectedOrder) ||
      (!selectedOrder ? orders[0] : undefined);
  const messages = w.messages.filter(
    (m) => !messageSearch || m.text.toLowerCase().includes(messageSearch.toLowerCase())
  );
  const refresh = () =>
    Promise.all([w.loadContext(), w.loadConversations(), w.loadMessages()]).then(() => {});
  const openEventOrder = async (orderId?: string) => {
    const selection = selectionRef.current;
    await w.loadContext(orderId);
    if (selection !== selectionRef.current) return;
    setSelectedOrder(orderId || '__choose__');
    setDetails(true);
  };
  const older = async () => {
    const el = scroller.current;
    if (!el) return;
    const height = el.scrollHeight,
      top = el.scrollTop;
    await w.loadMessages(true);
    requestAnimationFrame(() => {
      el.scrollTop = top + el.scrollHeight - height;
    });
  };
  const selectionRef = useRef('');
  selectionRef.current = w.accountId + '\u0000' + w.cid;
  const send = async (file?: File) => {
    const selection = selectionRef.current;
    const result = await w.send(file);
    if ((result.ok || result.imageSent) && selection === selectionRef.current)
      requestAnimationFrame(bottom);
    return result;
  };
  const bottom = () => {
    const el = scroller.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
    nearBottom.current = true;
    setNewCount(0);
    if (c) w.markViewed(c);
  };
  return (
    <div ref={shell} className="chat-shell" aria-label="消息客服工作台">
      <header className="chat-topbar">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="hidden rounded-xl bg-[var(--brand-soft)] p-2 sm:block">
            <MessageCircle size={20} />
          </span>
          <div>
            <h1 className="text-base font-bold">消息中心</h1>
            <p className="text-[11px] text-[var(--text-soft)]">客服工作台 · 按账号处理会话</p>
          </div>
        </div>
        <div className="flex items-center gap-1">
          <span className="chat-status hidden sm:inline-flex">
            {w.stream ? <Wifi size={12} /> : <Clock size={12} />}{' '}
            {w.stream ? '实时更新' : '定时更新'}
          </span>
          <button
            className="chat-icon"
            aria-label="刷新账号和会话"
            onClick={() => void Promise.all([w.loadAccounts(), refresh()])}
          >
            <RefreshCw size={17} className={w.loading ? 'animate-spin' : ''} />
          </button>
          <button
            className="chat-icon"
            aria-label="消息过滤规则"
            title="消息过滤规则"
            onClick={onFilters}
          >
            <Settings2 size={17} />
          </button>
        </div>
      </header>
      <div className="chat-grid relative">
        <aside
          className={`chat-list ${w.pane === 'chat' ? 'is-hidden' : ''}`}
          aria-label="会话列表"
        >
          <div className="space-y-3 border-b border-[var(--border)] p-3.5">
            <div className="flex items-center gap-2">
              <select
                aria-label="消息账号"
                className="ios-input min-w-0 flex-1 rounded-md px-2 py-2 text-sm font-bold"
                value={w.accountId}
                onChange={(e) => w.setAccountId(e.target.value)}
              >
                {!w.accounts.length && <option value="">暂无账号</option>}
                {w.accounts.map((a) => (
                  <option key={a.accountId} value={a.accountId}>
                    {a.connected ? '🟢' : '⚪'} {accountDisplayName(a)}
                  </option>
                ))}
              </select>
            </div>
            <PageTabs
              compact
              ariaLabel="消息日期范围"
              value={w.days}
              onChange={w.setDays}
              items={[
                { id: '3', label: '3天内' },
                { id: '7', label: '7天内' },
                { id: '30', label: '1个月内' },
              ]}
            />
            <ConversationSearch value={w.query} onChange={w.setQuery} />
            <div className="flex gap-1" role="group" aria-label="会话筛选">
              {[
                ['all', '全部'],
                ['unread', '未读'],
                ['pending', '待回复'],
              ].map(([v, label]) => (
                <button
                  key={v}
                  aria-pressed={w.filter === v}
                  className={`flex-1 rounded-md py-1.5 text-xs ${w.filter === v ? 'bg-[var(--surface-strong)] font-bold' : 'text-[var(--text-muted)] hover:bg-[var(--surface-hover)]'}`}
                  onClick={() => w.setFilter(v)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          {w.error && (
            <div
              className="m-3 rounded-lg bg-[var(--warning-soft)] p-3 text-xs text-[var(--warning)]"
              role="alert"
            >
              {w.error}
              <button className="mt-2 block underline" onClick={() => void w.loadConversations()}>
                重试加载
              </button>
            </div>
          )}
          <div
            ref={listScroll}
            className="min-h-0 flex-1 overflow-y-auto"
            onScroll={(e) => {
              listPosition.current = e.currentTarget.scrollTop;
            }}
          >
            {w.loading && (
              <div className="chat-empty">
                <Loader2 className="animate-spin" size={22} />
                <span className="text-xs">正在加载会话…</span>
              </div>
            )}
            {w.visibleConversations.map((v) => (
              <div key={v.cid} data-conversation-id={v.cid} className={`chat-conversation ${w.cid === v.cid ? 'selected' : ''}`}>
                <Avatar name={v.otherUserName || v.otherUserId} url={v.otherUserAvatar} />
                <button
                  className="min-w-0 text-left"
                  aria-label={`打开会话 ${v.otherUserName || v.otherUserId}`}
                  onClick={() => w.open(v)}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-bold">
                      {v.workspace?.pinned && <Pin size={11} className="mr-1 inline" />}
                      {v.otherUserName || `闲鱼用户 ${v.otherUserId}`}
                    </span>
                    <time className="shrink-0 text-[10px] text-[var(--text-soft)]">
                      {when(v.lastMessageTime)}
                    </time>
                  </div>
                  <div className="mt-1 flex items-center gap-2">
                    <p className="min-w-0 flex-1 truncate text-xs text-[var(--text-muted)]">
                      <MessageText text={v.lastMessageSummary || '暂无消息'} emojis={emojis} />
                    </p>
                    {unread(v) > 0 && (
                      <span className="rounded-full bg-[var(--unread-badge)] px-1.5 text-[10px] leading-4 text-white">
                        {unread(v) > 99 ? '99+' : unread(v)}
                      </span>
                    )}
                  </div>
                  <p className="mt-1.5 truncate text-[10px] text-[var(--text-soft)]">
                    {v.workspace?.needs_reply ? '待回复 · ' : ''}
                    {v.itemTitle || '普通会话'}
                  </p>
                </button>
              </div>
            ))}
            {!w.loading && !w.visibleConversations.length && (
              <div className="chat-empty">
                <Inbox size={30} />
                <p className="text-sm">{w.error ? '暂时无法读取会话' : '所选范围内暂无会话'}</p>
                <p className="text-xs">可调整日期、筛选或加载更早会话</p>
              </div>
            )}
            {w.hasMore && (
              <button
                className="w-full p-4 text-xs text-[var(--text-muted)] hover:bg-[var(--surface-hover)]"
                onClick={() => void w.loadConversations(true)}
              >
                加载更早会话
              </button>
            )}
          </div>
          <p className="border-t border-[var(--border)] px-3 py-2 text-[10px] text-[var(--text-soft)]">
            {w.visibleConversations.length} 条已加载会话 · 未读为工作台查看状态
          </p>
        </aside>
        <section
          className={`chat-thread ${w.pane === 'list' ? 'is-hidden' : ''}`}
          aria-label="聊天区域"
        >
          {c ? (
            <>
              <div className="flex min-h-16 shrink-0 items-center gap-3 border-b border-[var(--border)] bg-[var(--surface)] px-4 py-3">
                <button
                  className="chat-icon chat-mobile-back"
                  aria-label="返回会话列表"
                  onClick={() => w.setPane('list')}
                >
                  <ArrowLeft size={19} />
                </button>
                <Avatar name={c.otherUserName || c.otherUserId} url={ctx?.buyer.avatar_url || c.otherUserAvatar} />
                <div className="min-w-0 flex-1">
                  <h2 className="truncate text-sm font-bold">{c.otherUserName || c.otherUserId}</h2>
                  <p className="mt-1 truncate text-[11px] text-[var(--text-soft)]">
                    回复身份：{accountName} · {w.account?.connected ? '消息连接正常' : '消息连接断开'}
                  </p>
                </div>
                <button
                  className="chat-icon"
                  aria-label="搜索当前聊天"
                  onClick={() => setSearchOpen((v) => !v)}
                >
                  <Search size={17} />
                </button>
                <button
                  className="chat-icon"
                  aria-label={c.workspace?.pinned ? '取消置顶' : '置顶会话'}
                  onClick={() => void w.updateState({ pinned: !c.workspace?.pinned })}
                >
                  <Pin
                    size={16}
                    className={c.workspace?.pinned ? 'text-[var(--brand-text)]' : ''}
                  />
                </button>
                <button
                  className="chat-icon chat-details-toggle"
                  aria-label="打开买家与订单资料"
                  onClick={() => setDetails(true)}
                >
                  <PanelRight size={18} />
                </button>
              </div>
              {searchOpen && (
                <div className="flex shrink-0 items-center gap-2 border-b border-[var(--border)] bg-[var(--surface)] px-4 py-2">
                  <Search size={14} />
                  <input
                    autoFocus
                    aria-label="搜索已加载聊天记录"
                    placeholder="搜索已加载消息；加载历史可扩大范围"
                    className="ios-input min-w-0 flex-1 rounded-md p-2 text-xs"
                    value={messageSearch}
                    onChange={(e) => setMessageSearch(e.target.value)}
                  />
                  <span className="text-xs text-[var(--text-soft)]">{messages.length}</span>
                  <button
                    className="chat-icon"
                    aria-label="关闭聊天搜索"
                    onClick={() => {
                      setSearchOpen(false);
                      setMessageSearch('');
                    }}
                  >
                    <X size={15} />
                  </button>
                </div>
              )}
              <HandoverBanner context={ctx} onChange={w.handover} />
              {w.account?.authPaused && (
                <p
                  className="mx-4 mt-3 rounded-lg bg-[var(--warning-soft)] p-3 text-xs text-[var(--warning)]"
                  role="alert"
                >
                  {w.account.recoveryMessage || '账号认证暂停，请前往账号管理完成验证'}
                </p>
              )}
              {w.messageError && (
                <div
                  className="mx-4 mt-3 rounded-lg bg-[var(--warning-soft)] p-3 text-xs text-[var(--warning)]"
                  role="alert"
                >
                  {w.messageError}
                  <button className="ml-2 underline" onClick={() => void w.loadMessages()}>
                    重试
                  </button>
                </div>
              )}
              <div
                ref={scroller} aria-label="聊天消息区域" tabIndex={-1}
                onContextMenuCapture={event=>{if(!(event.target as HTMLElement).closest('input,textarea,[contenteditable="true"]'))event.preventDefault();}}
                onContextMenu={event=>{if((event.target as HTMLElement).closest('input,textarea,[contenteditable="true"]'))return;event.preventDefault();setMenu(null);setAreaMenu({x:event.clientX,y:event.clientY,anchor:event.currentTarget});}}
                className="relative min-h-0 flex-1 overflow-y-auto px-4 py-5 sm:px-6"
                onScroll={(e) => {
                  setMenu(null);setAreaMenu(null);
                  const el = e.currentTarget;
                  nearBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
                  if (nearBottom.current) {
                    setNewCount(0);
                    if (c && w.messagesReady) w.markViewed(c);
                  }
                }}
              >
                {w.hasOlder && (
                  <button
                    className="mx-auto mb-5 flex items-center gap-2 rounded-full border border-[var(--border)] bg-[var(--surface)] px-4 py-2 text-xs text-[var(--text-muted)]"
                    disabled={w.olderLoading}
                    onClick={() => void older()}
                  >
                    {w.olderLoading && <Loader2 size={13} className="animate-spin" />}加载更早消息
                  </button>
                )}
                {w.messageLoading && !w.messages.length ? (
                  <div className="chat-empty">
                    <Loader2 className="animate-spin" size={22} />
                    <p className="text-xs">正在读取官方聊天记录…</p>
                  </div>
                ) : (
                  <div className="mx-auto max-w-3xl space-y-4">
                    {messages.map((rawMessage, i) => { const m = resolvedMessage(rawMessage); return (
                      <div key={m.client_request_id || m.messageId || `${m.time}:${i}`} data-message-id={m.messageId}
                        className="message-action-row group relative" tabIndex={0} aria-label="聊天消息"
                        onContextMenu={event => openMenu(event,m)}
                        onKeyDown={event => { if (event.key==='ContextMenu' || (event.shiftKey && event.key==='F10')) { event.preventDefault();const box=event.currentTarget.getBoundingClientRect();setMenu({message:m,x:box.left+20,y:box.top+20,selection:'',anchor:event.currentTarget}); } }}>
                        <button type="button" className={`message-more chat-icon ${m.isSelf?'left-0':'right-0'}`} aria-label="消息更多操作" onClick={event => openMenu(event,m,true)}><MoreHorizontal size={16} /></button>
                        {(!i || m.time - messages[i - 1].time > 300000) && (
                          <p className="mb-4 text-center text-[10px] text-[var(--text-soft)]">
                            {new Date(m.time).toLocaleString('zh-CN')}
                          </p>
                        )}
                        {m.event_type ? (
                          <TransactionEvent message={m} orderCount={relatedOrderCount} onOpen={(id) => void openEventOrder(id)} onCheck={() => void refresh()} />
                        ) : m.type === 'system' ? (
                          <div className="mx-auto max-w-[88%] rounded-lg border border-[var(--border)] bg-[var(--surface-subtle)] px-3 py-2 text-center text-xs text-[var(--text-muted)]">
                            {m.text}
                          </div>
                        ) : (
                          <div
                            className={`flex items-start gap-2 ${m.isSelf ? 'justify-end' : ''}`}
                          >
                            {!m.isSelf && (
                              <span className="hidden sm:block">
                                <Avatar
                                  name={c.otherUserName || c.otherUserId}
                                  url={ctx?.buyer.avatar_url || c.otherUserAvatar}
                                />
                              </span>
                            )}
                            <div className="min-w-0 w-fit max-w-[80%]">
                            <div className={`chat-bubble ${m.isSelf ? 'own' : ''}`}>
                              {m.reply && <button type="button" className="message-quote" aria-label="定位引用原消息" onClick={() => void locateReference(m.reply!.messageId)}>
                                <strong className="block truncate">{m.reply.senderId===w.account?.xianyuUserId?'我方':m.reply.senderName || m.reply.senderId || '原消息'}</strong>
                                <span className="line-clamp-2 whitespace-pre-wrap"><MessageText text={m.reply.text || '[图片]'} emojis={emojis} /></span>
                                {m.reply.images[0] && <img src={m.reply.images[0]} alt="引用图片" className="mt-1 h-12 w-12 rounded object-cover" />}
                              </button>}
                              {m.images.map((url) => (
                                <button
                                  type="button"
                                  className="block"
                                  key={url}
                                  aria-label="放大聊天图片"
                                  onClick={() => setImage(url)}
                                >
                                  <img
                                    src={url.startsWith('//') ? `https:${url}` : url}
                                    alt="聊天图片"
                                    className="max-h-72 max-w-full rounded-lg"
                                    loading="lazy"
                                    onLoad={() => {
                                      if (nearBottom.current && scroller.current)
                                        scroller.current.scrollTop = scroller.current.scrollHeight;
                                    }}
                                  />
                                </button>
                              ))}
                              {m.text && (
                                <div className="whitespace-pre-wrap"><MessageText text={m.text} emojis={emojis} /></div>
                              )}
                              {m.type === 'card' &&
                                (m.card?.kind === 'order' ? (
                                  <button
                                    className="mt-2 flex items-center gap-1 text-xs font-bold text-[var(--brand-text)]"
                                    onClick={() =>
                                      void w.loadContext(m.card!.orderId).then(() => {
                                        setSelectedOrder(m.card!.orderId || '');
                                        setDetails(true);
                                      })
                                    }
                                  >
                                    查看订单 {m.card.orderId}
                                    <ChevronRight size={12} />
                                  </button>
                                ) : m.card?.kind === 'item' ? (
                                  <a
                                    className="mt-2 flex items-center gap-1 text-xs font-bold text-[var(--brand-text)]"
                                    href={`https://www.goofish.com/item?id=${encodeURIComponent(m.card.itemId || '')}`}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                  >
                                    官方商品详情
                                    <ChevronRight size={12} />
                                  </a>
                                ) : (
                                  <p className="mt-2 text-[11px] text-[var(--text-soft)]">
                                    此卡片暂无可确认的详情，可在官方客户端查看
                                  </p>
                                ))}
                            </div>
                            <MessageSendStatus status={m.send_status} onCheck={() => void refresh()} />
                            </div>
                          </div>
                        )}
                      </div>
                    ); })}
                    {!messages.length && !w.messageLoading && (
                      <div className="chat-empty">
                        <MessageCircle size={28} />
                        <p className="text-sm">
                          {messageSearch ? '已加载消息中没有匹配项' : '暂无聊天记录'}
                        </p>
                      </div>
                    )}
                  </div>
                )}
              </div>
              {newCount > 0 && (
                <button
                  className="mx-auto -mt-11 mb-3 z-10 flex items-center gap-1 rounded-full border border-[var(--border)] bg-[var(--surface)] px-4 py-2 text-xs shadow-sm"
                  onClick={bottom}
                >
                  <ArrowDown size={13} />
                  {newCount} 条更新，回到底部
                </button>
              )}
              <ChatComposer
                key={`${w.owner}:${w.accountId}:${w.cid}`}
                draft={w.draft}
                setDraft={w.setDraft}
                send={send}
                sending={w.sending}
                disabled={!w.account?.connected || !!w.account?.authPaused || !w.owner}
                phrases={w.phrases}
                emojis={emojis}
                reference={w.reference}
                cancelReference={() => w.setReference(undefined)}
              />
            </>
          ) : (
            <div className="chat-empty">
              <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5">
                <MessageCircle size={34} />
              </div>
              <h2 className="mt-2 text-base font-bold text-[var(--text-muted)]">从一条会话开始</h2>
              <p className="max-w-xs text-sm leading-6">
                选择买家后，可在这里回复消息，并同时查看商品、订单和处理记录。
              </p>
            </div>
          )}
        </section>
        {details && (
          <button
            className="absolute inset-0 z-30 bg-black/20 chat-details-toggle"
            aria-label="关闭资料遮罩"
            onClick={() => setDetails(false)}
          />
        )}
        <aside
          ref={drawerRef}
          role={details && workspaceWidth < 1100 ? 'dialog' : undefined}
          aria-modal={details && workspaceWidth < 1100 ? true : undefined}
          className={`chat-details ${details ? 'open' : ''}`}
          aria-label="买家与订单资料"
        >
          <div className="flex h-16 items-center justify-between border-b border-[var(--border)] px-4">
            <h2 className="text-sm font-bold">会话资料</h2>
            <button
              className="chat-icon chat-details-close"
              aria-label="关闭买家资料"
              onClick={() => setDetails(false)}
            >
              <X size={18} />
            </button>
          </div>
          {!c ? (
            <div className="chat-empty">
              <UserRound size={25} />
              <p className="text-xs">选择会话后展示关联资料</p>
            </div>
          ) : (
            <>
              {w.contextError && (
                <p
                  className="m-3 rounded-lg bg-[var(--warning-soft)] p-3 text-xs text-[var(--warning)]"
                  role="alert"
                >
                  {w.contextError}
                  <button className="ml-1 underline" onClick={() => void w.loadContext()}>
                    重试
                  </button>
                </p>
              )}
              <section className="chat-section">
                <h3 className="chat-section-title">
                  <UserRound size={15} />
                  买家资料
                </h3>
                <div className="mb-3 flex items-center gap-3"><Avatar name={ctx?.buyer.nickname || c.otherUserName || c.otherUserId} url={ctx?.buyer.avatar_url || c.otherUserAvatar}/><span className="min-w-0 break-words text-sm font-bold">{ctx?.buyer.nickname || c.otherUserName || c.otherUserId}</span></div>
                <dl className="chat-facts">
                  <dt>买家昵称</dt>
                  <dd>{ctx?.buyer.nickname || c.otherUserName || `闲鱼用户 ${ctx?.buyer.id || c.otherUserId}`} </dd>
                  {ctx?.buyer.nickname_source && ctx.buyer.nickname && (
                    <>
                      <dt>昵称来源</dt>
                      <dd>{{ conversation: '闲鱼会话', order: '订单资料', public_profile: '闲鱼公开资料', public_profile_cache: '公开资料缓存' }[ctx.buyer.nickname_source] || '闲鱼资料'}</dd>
                    </>
                  )}
                  <dt>买家ID</dt>
                  <dd>{ctx?.buyer.id || c.otherUserId}</dd>
                </dl>
                <label
                  className="mt-4 block text-xs text-[var(--text-soft)]"
                  htmlFor="chat-customer-note"
                >
                  客服备注 · 仅本工作台可见
                </label>
                <textarea
                  id="chat-customer-note"
                  className="ios-input mt-2 min-h-20 w-full resize-y rounded-md p-2.5 text-xs"
                  value={note}
                  maxLength={2000}
                  placeholder="记录客户偏好或待处理事项"
                  onChange={(e) => setNote(e.target.value)}
                />
                <button
                  disabled={!ctx || note === ctx.buyer.note}
                  className="ios-btn-secondary mt-2 rounded-md px-3 py-1.5 text-xs"
                  onClick={() => void w.updateState({ note })}
                >
                  保存备注
                </button>
              </section>
              <section className="chat-section">
                <h3 className="chat-section-title">
                  <Package size={15} />
                  关联商品
                </h3>
                <div className="flex items-start gap-3">
                  {ctx?.item.item_image || c.itemImage ? (
                    <img
                      src={ctx?.item.item_image || c.itemImage}
                      alt=""
                      className="h-12 w-12 rounded-lg object-cover"
                    />
                  ) : (
                    <span className="chat-avatar">
                      <Package size={20} />
                    </span>
                  )}
                  <div className="min-w-0">
                    <p className="break-words text-xs font-bold leading-5">
                      {ctx?.item.item_title || c.itemTitle || '未关联商品'}
                    </p>
                    <p className="mt-1 break-all text-[10px] text-[var(--text-soft)]">
                      {ctx?.item.item_id || c.itemId || '普通会话'}
                    </p>
                  </div>
                </div>
              </section>
              <section className="chat-section">
                <h3 className="chat-section-title">
                  <Package size={15} />
                  关联订单<span className="ml-auto font-normal">{orders.length} 笔匹配</span>
                </h3>
                {(orders.length > 0 || candidates.length > 0) && (
                  <select
                    aria-label="选择关联订单"
                    className="ios-input mb-3 w-full rounded-md px-2 py-2 text-xs"
                    value={order?.order_id || ''}
                    onChange={(e) => setSelectedOrder(e.target.value)}
                  >
                    {!order && <option value="">请选择订单，核对后处理</option>}
                    {orders.map((o) => (
                      <option key={o.order_id} value={o.order_id}>
                        {o.order_id} · {o.amount}
                      </option>
                    ))}
                    {candidates.length > 0 && (
                      <optgroup label="其他候选 · 请核对后选择">
                        {candidates.map((o) => (
                          <option key={o.order_id} value={o.order_id}>
                            {o.order_id} · {o.amount}
                          </option>
                        ))}
                      </optgroup>
                    )}
                  </select>
                )}
                {order ? (
                  <>
                    <div className="mb-3 flex items-center justify-between">
                      <span className="text-xs font-bold">¥{order.amount || '未获取'}</span>
                      <StatusBadge status={(order.order_status || order.status) as any} />
                    </div>
                    <dl className="chat-facts">
                      <dt>订单号</dt>
                      <dd>{order.order_id}</dd>
                      <dt>规格／数量</dt>
                      <dd>
                        {order.spec_value || '默认规格'} · {order.quantity || '未获取'}件
                      </dd>
                      <dt>收货人</dt>
                      <dd>{order.receiver_name || '未获取'}</dd>
                      <dt>联系电话</dt>
                      <dd>{order.receiver_phone || '未获取'}</dd>
                      <dt>收货地址</dt>
                      <dd className="whitespace-pre-wrap">{order.receiver_address || '未获取'}</dd>
                      <dt>更新时间</dt>
                      <dd>{formatOrderTime(order.updated_at || order.created_at)}</dd>
                    </dl>
                    <p className="my-3 text-[10px] leading-4 text-[var(--text-soft)]">
                      官方订单同步缓存 · 刷新官方状态可核对最新信息
                    </p>
                    {order.cookie_id === w.accountId ? (
                      <OrderWorkspaceActions
                        key={order.order_id}
                        order={order}
                        features={
                          ctx?.features || { auto_rate_enabled: false, auto_flower_enabled: false }
                        }
                        refresh={w.loadContext}
                      />
                    ) : (
                      <p className="rounded-md bg-[var(--surface-subtle)] px-3 py-2 text-[10px] leading-4 text-[var(--text-soft)]">
                        这笔订单属于同一用户的卖家账号，当前显示买家侧订单资料。
                      </p>
                    )}
                  </>
                ) : (
                  <p className="text-xs leading-5 text-[var(--text-soft)]">
                    {relatedOrderCount ? '请先选择订单，再查看对应交易资料。' : '尚无可靠匹配的订单，付款及平台同步后将在此展示。'}
                  </p>
                )}
              </section>
              <section className="chat-section">
                <h3 className="chat-section-title">
                  <Clock size={15} />
                  处理记录
                </h3>
                {order?.delivery_attempt && (
                  <p className="mb-2 text-xs text-[var(--text-muted)]">
                    发货台账：
                    {(
                      {
                        completed: '已完成',
                        sending: '发送中',
                        reserved: '已预留库存',
                        needs_review: '结果待核查',
                        failed: '未完成',
                      } as Record<string, string>
                    )[order.delivery_attempt.status] || order.delivery_attempt.status}
                  </p>
                )}
                {order?.interaction_attempts?.length ? (
                  order.interaction_attempts.map((a) => (
                    <p key={a.action} className="mb-2 flex justify-between gap-2 text-xs">
                      <span>
                        {(
                          { rate: '评价买家', flower: '索要小红花', thanks: '收货致谢' } as Record<
                            string,
                            string
                          >
                        )[a.action] || a.action}
                      </span>
                      <span className="text-[var(--text-muted)]">
                        {(
                          {
                            sent: '已发送',
                            baseline: '历史记录',
                            needs_review: '待核查',
                            attempting: '处理中',
                            skipped: '已跳过',
                          } as Record<string, string>
                        )[a.status] || a.status}
                      </span>
                    </p>
                  ))
                ) : (
                  <p className="text-xs text-[var(--text-soft)]">暂无互动处理记录</p>
                )}
              </section>
            </>
          )}
        </aside>
      </div>
      {image &&
        createPortal(
          <div
            ref={imageRef}
            className="modal-overlay"
            role="dialog"
            aria-modal="true"
            aria-label="聊天图片预览"
            onClick={() => setImage('')}
          >
            <button
              className="absolute right-5 top-5 rounded-full bg-[var(--surface)] p-3"
              aria-label="关闭图片预览"
              onClick={() => setImage('')}
            >
              <X size={22} />
            </button>
            <img
              src={image.startsWith('//') ? `https:${image}` : image}
              alt="聊天大图"
              className="max-h-[88vh] max-w-[94vw] object-contain"
              onClick={(event) => event.stopPropagation()}
            />
          </div>,
          document.body
        )}
      {areaMenu && <ChatAreaMenu target={areaMenu} close={closeAreaMenu} bottom={()=>{const el=scroller.current;if(el)el.scrollTo({top:el.scrollHeight});}} refresh={()=>void w.loadMessages()} />}
      {menu && <MessageMenu target={menu} capabilities={capabilities} close={closeMenu} quote={() => w.setReference(menu.message)} forward={() => setForwarding(menu.message)} />}
      {forwarding && <ForwardDialog owner={w.owner} account={w.accountId} sourceCid={w.cid} message={forwarding}
        accountName={accountName} scopeKey={JSON.stringify([w.accountId,w.days,w.query,w.filter])}
        scopeLabel={`${w.days === '30' ? '1个月内' : w.days+'天内'} · ${{all:'全部',unread:'未读',pending:'待回复'}[w.filter] || '全部'}${w.query ? ' · 搜索：'+w.query : ''}`}
        recipients={forwardRecipients(w.visibleConversations)} hasMore={w.hasMore} loading={w.loading}
        loadMore={() => void w.loadConversations(true)} emojis={emojis}
        close={() => setForwarding(undefined)} sent={target => { void w.loadConversations();if (target===w.cid) void w.loadMessages(); }} />}
    </div>
  );
}
