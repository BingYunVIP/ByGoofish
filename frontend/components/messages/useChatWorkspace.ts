import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { get, patch, post } from '../../lib/request';
import {
  getChatAccounts,
  getChatConversations,
  getChatMessages,
  getQuickPhrases,
  verifyToken,
} from '../../services/api';
import type { ChatAccount, ChatMessage, QuickPhrase } from '../../types';
import type {
  ConversationContext,
  ConversationState,
  OutgoingMessage,
  WorkspaceConversation,
} from './workspaceTypes';
import { dateRangeBounds, timestampInRange } from '../DateRangeFilter';
import { notify } from '../../services/feedback';
import { draftKey, mergeMessages, mergeOutgoing, messageTimeline, unread } from '../../lib/chatWorkspace.mjs';
export { draftKey, mergeMessages, unread };
const readDraft = (key: string) => {
  try {
    return localStorage.getItem(key) || '';
  } catch {
    return '';
  }
};
const statusOf = (e: any) => e?.response?.status;
const errorText = (e: any) => String(e?.response?.data?.detail || e?.message || '请求失败');
export default function useChatWorkspace(isActive: boolean) {
  const [accounts, setAccounts] = useState<ChatAccount[]>([]),
    [owner, setOwner] = useState(0),
    [accountId, setAccountId] = useState(''),
    [cid, setCid] = useState('');
  const [conversations, setConversations] = useState<WorkspaceConversation[]>([]),
    [messages, setMessages] = useState<ChatMessage[]>([]),
    [context, setContext] = useState<ConversationContext>();
  const [query, setQuery] = useState(''),
    [filter, setFilter] = useState('all'),
    [days, setDays] = useState('7'),
    [pane, setPane] = useState('list');
  const [loading, setLoading] = useState(true),
    [messageLoading, setMessageLoading] = useState(false),
    [error, setError] = useState(''),
    [messageError, setMessageError] = useState('');
  const [hasMore, setHasMore] = useState(false),
    [hasOlder, setHasOlder] = useState(false),
    [olderLoading, setOlderLoading] = useState(false),
    [phrases, setPhrases] = useState<QuickPhrase[]>([]);
  const [sending, setSending] = useState(false),
    [outgoing, setOutgoing] = useState<OutgoingMessage[]>([]),
    [stream, setStream] = useState(false),
    [visible, setVisible] = useState(!document.hidden);
  const [contextError, setContextError] = useState('');
  const [conversationAccount, setConversationAccount] = useState('');
  const [messageScope, setMessageScope] = useState(''),
    [contextScope, setContextScope] = useState('');
  const messageScopeRef = useRef('');
  const scope = accountId + '\u0000' + cid;
  const enabled = useRef(isActive && visible);
  enabled.current = isActive && visible;
  const [draftState, setDraftState] = useState({ key: '', value: '' });
  const accountsRef = useRef<ChatAccount[]>([]);
  accountsRef.current = accounts;
  const streamRef = useRef(false);
  streamRef.current = stream;
  const pendingOps = useRef<Record<string, string>>({});
  const current = useRef({ accountId, cid });
  current.current = { accountId, cid };
  const conversationCursor = useRef<number>();
  const messageCursor = useRef<number>();
  const loadedHistory = useRef(false);
  const backoff = useRef(0);
  const inFlight = useRef(new Set<string>());
  const refreshAfter = useRef(new Set<string>());
  const previews = useRef(new Set<string>());
  useEffect(() => () => {
    for (const url of previews.current) URL.revokeObjectURL(url);
    previews.current.clear();
  }, [scope]);
  const key = owner && accountId && cid ? draftKey(owner, accountId, cid) : '';
  const draft = key ? (draftState.key === key ? draftState.value : readDraft(key)) : '';
  const [replyState, setReplyState] = useState<{ key: string; value?: ChatMessage }>({ key: '' });
  const replyKey = key ? key + ':reference' : '';
  let reference: ChatMessage | undefined;
  if (replyKey) {
    if (replyState.key === replyKey) reference = replyState.value;
    else { try { reference = JSON.parse(readDraft(replyKey) || 'null') || undefined; } catch {} }
  }
  const setReference = (value?: ChatMessage) => {
    if (!replyKey) return;
    setReplyState({ key: replyKey, value });
    try { if (value) localStorage.setItem(replyKey, JSON.stringify(value)); else localStorage.removeItem(replyKey); } catch {}
  };
  const setDraft = (value: string) => {
    if (!key) return;
    setDraftState({ key, value });
    try {
      if (value) localStorage.setItem(key, value);
      else localStorage.removeItem(key);
    } catch {
      /* Keep in-memory draft when storage is unavailable. */
    }
  };
  const account = accounts.find((a) => a.accountId === accountId);
  const conversation =
    conversationAccount === accountId ? conversations.find((c) => c.cid === cid) : undefined;
  const fail = (e: any) => {
    if ([429, 409, 503].includes(statusOf(e))) backoff.current = Date.now() + 30000;
    return errorText(e);
  };
  const loadAccounts = useCallback(async () => {
    try {
      const list = await getChatAccounts();
      accountsRef.current = list;
      setAccounts(list);
      setAccountId((old) =>
        list.some((a) => a.accountId === old)
          ? old
          : list.find((a) => a.connected)?.accountId || list[0]?.accountId || ''
      );
    } catch (e) {
      setError(errorText(e));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    if (!isActive) return;
    void loadAccounts();
    void verifyToken()
      .then((x) => setOwner(x.user_id || 0))
      .catch(() => {});
    void getQuickPhrases()
      .then(setPhrases)
      .catch(() => {});
  }, [isActive, loadAccounts]);
  useEffect(() => {
    const onVisibility = () => setVisible(!document.hidden);
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, []);
  const loadConversations = useCallback(async (append = false) => {
    const a = current.current.accountId;
    const selectedAccount = accountsRef.current.find((v) => v.accountId === a);
    if (selectedAccount?.authPaused) {
      setError(selectedAccount.recoveryMessage || '账号待人工验证，请前往账号管理完成验证');
      setLoading(false);
      return;
    }
    if (!enabled.current || !a || Date.now() < backoff.current) return;
    const request = `conversations:${a}:${append}`;
    if (inFlight.current.has(request)) {
      if (!append) refreshAfter.current.add(request);
      return;
    }
    inFlight.current.add(request);
    try {
      const result = await getChatConversations(a, append ? conversationCursor.current : undefined);
      if (a !== current.current.accountId) return;
      const incoming = result.conversations as WorkspaceConversation[];
      setConversationAccount(a);
      setConversations((old) =>
        append || loadedHistory.current
          ? [...new Map([...old, ...incoming].map((v) => [v.cid, v])).values()]
          : incoming
      );
      const previous = conversationCursor.current;
      if (append || !loadedHistory.current) {
        const times = incoming.map((v) => Number(v.lastMessageTime)).filter((v) => v > 0);
        const next = result.nextCursor ?? (times.length ? Math.min(...times) : undefined);
        conversationCursor.current = next;
        setHasMore(
          Boolean(
            result.hasMore && next !== undefined && (!append || Number(next) < Number(previous))
          )
        );
      }
      if (append) loadedHistory.current = true;
      setError('');
    } catch (e) {
      if (a === current.current.accountId) setError(fail(e));
    } finally {
      inFlight.current.delete(request);
      setLoading(false);
      if (refreshAfter.current.delete(request) && a === current.current.accountId)
        void loadConversations();
    }
  }, []);
  const loadMessages = useCallback(async (older = false) => {
    const { accountId: a, cid: c } = current.current;
    if (accountsRef.current.find((v) => v.accountId === a)?.authPaused) return;
    if (!enabled.current || !a || !c || Date.now() < backoff.current) return;
    const request = `messages:${a}:${c}:${older}`;
    if (inFlight.current.has(request)) {
      if (!older) refreshAfter.current.add(request);
      return;
    }
    inFlight.current.add(request);
    if (older) setOlderLoading(true);
    else setMessageLoading(true);
    try {
      const r = await getChatMessages(a, c, older ? messageCursor.current : undefined);
      if (a !== current.current.accountId || c !== current.current.cid) return;
      const nextScope = a + '\u0000' + c,
        sameScope = messageScopeRef.current === nextScope;
      messageScopeRef.current = nextScope;
      setMessageScope(nextScope);
      setMessages((old) => mergeMessages(sameScope ? old : [], r.messages || []));
      if (older || messageCursor.current === undefined) {
        const prev = messageCursor.current;
        const times = (r.messages || []).map((v) => Number(v.time)).filter((v) => v > 0);
        const next = r.nextCursor ?? (times.length ? Math.min(...times) : undefined);
        messageCursor.current = next;
        setHasOlder(
          Boolean(r.hasMore && next !== undefined && (!older || Number(next) < Number(prev)))
        );
      }
      setMessageError('');
      return r.messages || [];
    } catch (e) {
      if (a === current.current.accountId && c === current.current.cid) setMessageError(fail(e));
    } finally {
      inFlight.current.delete(request);
      if (a === current.current.accountId && c === current.current.cid) {
        setMessageLoading(false);
        setOlderLoading(false);
        if (refreshAfter.current.delete(request)) void loadMessages();
      }
    }
  }, []);
  const loadContext = useCallback(async (orderId?: string) => {
    const { accountId: a, cid: c } = current.current;
    if (!a || !c) return;
    try {
      const r = await get<{ data: ConversationContext }>(
        `/chat/context/${encodeURIComponent(a)}/${encodeURIComponent(c)}`,
        orderId ? { order_id: orderId } : undefined
      );
      if (a === current.current.accountId && c === current.current.cid) {
        setContextScope(a + '\u0000' + c);
        setContext(r.data);
        setContextError('');
        if (r.data.buyer.nickname) setConversations((old) => old.map((v) =>
          v.cid === c ? { ...v, otherUserName: r.data.buyer.nickname } : v));
        setOutgoing((old) => mergeOutgoing(old, r.data.outgoing || [], a + '\u0000' + c, a));
      }
    } catch (e) {
      if (a === current.current.accountId && c === current.current.cid)
        setContextError(errorText(e));
    }
  }, []);
  useEffect(() => {
    current.current = { accountId, cid: '' };
    setCid('');
    setPane('list');
    setConversations([]);
    setMessages([]);
    setContext(undefined);
    conversationCursor.current = undefined;
    messageCursor.current = undefined;
    loadedHistory.current = false;
    backoff.current = 0;
    setHasMore(false);
    setHasOlder(false);
    if (accountId) {
      setLoading(true);
      void loadConversations();
    }
  }, [accountId, loadConversations]);
  useEffect(() => {
    setMessages([]);
    setOutgoing([]);
    setContext(undefined);
    setContextError('');
    setMessageError('');
    messageCursor.current = undefined;
    messageScopeRef.current = '';
    setMessageScope('');
    setContextScope('');
    setHasOlder(false);
    if (cid) {
      void loadMessages();
      void loadContext();
    }
  }, [accountId, cid, loadMessages, loadContext]);
  useEffect(() => {
    if (!isActive || !visible || !accountId) return;
    const timer = window.setInterval(() => {
      if (!streamRef.current || (backoff.current > 0 && Date.now() >= backoff.current)) {
        if (Date.now() >= backoff.current) backoff.current = 0;
        void loadConversations();
        if (current.current.cid) void loadMessages();
      }
    }, 10000);
    const accountTimer = window.setInterval(() => void loadAccounts(), 30000);
    void loadConversations();
    if (cid) {
      void loadMessages();
      void loadContext();
    }
    return () => {
      clearInterval(timer);
      clearInterval(accountTimer);
    };
  }, [isActive, visible, accountId, loadConversations, loadMessages, loadAccounts, loadContext]);
  useEffect(() => {
    if (!isActive || !visible || !accountId) return;
    const controller = new AbortController();
    let retry: ReturnType<typeof setTimeout> | undefined;
    let refresh: ReturnType<typeof setTimeout> | undefined;
    let delay = 1000;
    const connect = async () => {
      try {
        const response = await fetch(`/chat/events/${encodeURIComponent(accountId)}`, {
          headers: { Authorization: `Bearer ${localStorage.getItem('auth_token') || ''}` },
          signal: controller.signal,
        });
        if (!response.ok || !response.body) throw new Error('event stream unavailable');
        setStream(true);
        delay = 1000;
        const reader = response.body.getReader(),
          decoder = new TextDecoder();
        let pending = '';
        while (!controller.signal.aborted) {
          const chunk = await reader.read();
          if (chunk.done) break;
          pending += decoder.decode(chunk.value, { stream: true });
          const parts = pending.split('\n\n');
          pending = parts.pop() || '';
          for (const part of parts) {
            if (!part.startsWith('data:')) continue;
            try {
              const event = JSON.parse(part.slice(5));
              if (event.type === 'heartbeat') continue;
              if (event.type === 'connection') void loadAccounts();
              if (refresh) clearTimeout(refresh);
              refresh = setTimeout(() => {
                void loadConversations();
                if (current.current.cid) {
                  void loadMessages();
                  void loadContext();
                }
              }, 500);
            } catch {
              /* Ignore malformed events; periodic fallback remains available. */
            }
          }
        }
      } catch {
        /* Disconnects enable the bounded fallback poll. */
      } finally {
        if (!controller.signal.aborted) {
          setStream(false);
          retry = setTimeout(connect, delay);
          delay = Math.min(30000, delay * 2);
        }
      }
    };
    void connect();
    return () => {
      controller.abort();
      if (retry) clearTimeout(retry);
      if (refresh) clearTimeout(refresh);
      setStream(false);
    };
  }, [isActive, visible, accountId, loadConversations, loadMessages, loadContext, loadAccounts]);
  useEffect(() => {
    if (isActive && visible && account?.connected && !account.authPaused) {
      backoff.current = 0;
      void loadConversations();
      if (cid) void loadMessages();
    }
  }, [
    account?.connected,
    account?.authPaused,
    isActive,
    visible,
    accountId,
    loadConversations,
    loadMessages,
  ]);
  const updateState = async (changes: ConversationState, target = cid) => {
    const a = accountId;
    if (!a || !target) return;
    try {
      const r = await patch<{ data: ConversationState }>(
        `/chat/state/${encodeURIComponent(a)}/${encodeURIComponent(target)}`,
        changes
      );
      if (a !== current.current.accountId) return;
      setConversations((old) =>
        old.map((v) => (v.cid === target ? { ...v, workspace: { ...v.workspace, ...r.data } } : v))
      );
      if (target === current.current.cid)
        setContext((old) =>
          old
            ? {
                ...old,
                workspace: { ...old.workspace, ...r.data },
                buyer: { ...old.buyer, note: r.data.note ?? old.buyer.note },
              }
            : old
        );
    } catch (e) {
      notify(errorText(e), 'error');
    }
  };
  const markViewed = useCallback(
    (v: WorkspaceConversation) => {
      if (!v || !current.current.accountId) return;
      if (
        v.workspace?.viewed_message_time === v.lastMessageTime &&
        v.workspace?.viewed_message_summary === v.lastMessageSummary &&
        (v.workspace?.viewed_unread_count || 0) >= v.unreadCount
      )
        return;
      void updateState(
        {
          viewed_message_time: v.lastMessageTime,
          viewed_message_summary: v.lastMessageSummary,
          viewed_unread_count: v.unreadCount,
        },
        v.cid
      );
    },
    [accountId, cid]
  );
  const open = (v: WorkspaceConversation) => {
    setCid(v.cid);
    current.current = { accountId, cid: v.cid };
    setPane('chat');
  };
  const visibleConversations = useMemo(
    () =>
      (conversationAccount === accountId ? conversations : [])
        .filter((v) =>
          timestampInRange(v.lastMessageTime, dateRangeBounds({ preset: days, start: '', end: '' }))
        )
        .filter(
          (v) =>
            !query ||
            [v.otherUserName, v.otherUserId, v.itemTitle, v.lastMessageSummary].some((x) =>
              String(x || '')
                .toLowerCase()
                .includes(query.toLowerCase())
            )
        )
        .filter((v) =>
          filter === 'unread'
            ? unread(v) > 0
            : filter === 'pending'
              ? v.workspace?.needs_reply
              : true
        )
        .sort(
          (a, b) =>
            Number(Boolean(b.workspace?.pinned)) - Number(Boolean(a.workspace?.pinned)) ||
            b.lastMessageTime - a.lastMessageTime
        ),
    [conversations, days, query, filter, conversationAccount, accountId]
  );
  const send = async (file?: File) => {
    const a = accountId,
      c = cid,
      target = conversation?.otherUserId,
      text = draft.trim(),
      savedKey = key,
      savedReplyKey = replyKey,
      replying = reference;
    if (!a || !c || !target || sending || (!text && !file)) return { ok: false, imageSent: false };
    setSending(true);
    let imageSent = false;
    try {
      for (const kind of [...(file ? ['image'] : []), ...(text ? ['text'] : [])]) {
        const bytes = kind === 'image' ? await file!.arrayBuffer() : new TextEncoder().encode(text);
        const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)))
          .map((x) => x.toString(16).padStart(2, '0'))
          .join('');
        const operationKey = savedKey + ':' + kind + ':' + digest + ':' + (replying?.messageId || '');
        let id = pendingOps.current[operationKey] || readDraft(operationKey);
        if (!id) {
          id = crypto.randomUUID();
          pendingOps.current[operationKey] = id;
          try {
            localStorage.setItem(operationKey, id);
          } catch {}
        }
        const temporary: OutgoingMessage = {
          workspace_scope: a + '\u0000' + c,
          messageId: id,
          client_request_id: id,
          senderId: account?.xianyuUserId || a,
          senderName: '',
          isSelf: true,
          type: kind === 'image' ? 'image' : 'text',
          text: kind === 'text' ? text : '[图片]',
          images: [],
          time: Date.now(),
          send_status: 'sending',
          reply: replying ? { messageId: replying.messageId, senderId: replying.senderId, senderName: replying.senderName, time: replying.time, text: replying.text, images: replying.images, type: replying.type } : undefined,
        };
        if (current.current.accountId === a && current.current.cid === c) {
          if (kind === 'image') {
            const preview = URL.createObjectURL(file!);
            previews.current.add(preview);
            temporary.images = [preview];
            temporary.text = '';
          }
          setOutgoing((old) => [...old.filter((m) => m.client_request_id !== id), temporary]);
        }
        let r: any;
        try {
          if (kind === 'image') {
            const form = new FormData();
            form.append('image', file!);
            form.append('cid', c);
            form.append('to_user_id', target);
            form.append('client_request_id', id);
            if (replying) form.append('reply_to_message_id', replying.messageId);
            r = await post(`/chat/send-image/${encodeURIComponent(a)}`, form, { timeout: 60000 });
          } else
            r = await post(`/chat/send/${encodeURIComponent(a)}`, {
              cid: c,
              to_user_id: target,
              text,
              client_request_id: id,
              reply_to_message_id: replying?.messageId,
            });
        } catch (e) {
          const state =
            [400, 403, 413, 415, 422].includes(statusOf(e)) ||
            (e as any)?.response?.headers?.['x-chat-send-status'] === 'not_sent'
              ? 'failed'
              : 'unknown';
          if (current.current.accountId === a && current.current.cid === c)
            setOutgoing((old) =>
              old.map((m) =>
                m.client_request_id === id ? { ...m, send_status: state, error: errorText(e) } : m
              )
            );
          throw e;
        }
        const state = r.data?.status || 'sent';
        if (current.current.accountId === a && current.current.cid === c)
          setOutgoing((old) =>
            old.map((m) =>
              m.client_request_id === id
                ? { ...m, send_status: state, messageId: r.data?.messageId || id }
                : m
            )
          );
        if (state !== 'sent')
          throw new Error(
            state === 'unknown' ? '发送结果待核查，请查看历史，勿重复发送' : '本次发送未完成'
          );
        delete pendingOps.current[operationKey];
        try {
          localStorage.removeItem(operationKey);
        } catch {}
        if (kind === 'image') imageSent = true;
        else {
          try {
            localStorage.removeItem(savedKey);
          } catch {}
          if (current.current.accountId === a && current.current.cid === c)
            setDraftState({ key: savedKey, value: '' });
        }
      }
      if (current.current.accountId === a && current.current.cid === c) {
        await Promise.all([loadMessages(), loadConversations(), loadContext()]);
      }
      try { localStorage.removeItem(savedReplyKey); } catch {}
      if (current.current.accountId === a && current.current.cid === c) setReplyState({ key: savedReplyKey });
      return { ok: true, imageSent };
    } catch (e) {
      notify(errorText(e), 'error');
      return { ok: false, imageSent };
    } finally {
      setSending(false);
    }
  };
  const handover = async (action: 'pause' | 'resume') => {
    try {
      await post(`/chat/handover/${encodeURIComponent(accountId)}/${encodeURIComponent(cid)}`, {
        action,
      });
      await loadContext();
    } catch (e) {
      notify(errorText(e), 'error');
    }
  };
  return {
    accounts,
    owner,
    accountId,
    setAccountId,
    account,
    cid,
    conversation,
    conversations: conversationAccount === accountId ? conversations : [],
    visibleConversations,
    open,
    query,
    setQuery,
    filter,
    setFilter,
    days,
    setDays,
    pane,
    setPane,
    loading,
    messageLoading,
    error,
    messageError,
    contextError,
    messages: messageTimeline(messageScope === scope ? messages : [],
      outgoing.filter((v) => v.workspace_scope === scope)) as OutgoingMessage[],
    messagesReady: messageScope === scope,
    outgoing: outgoing.filter((v) => v.workspace_scope === scope),
    context: contextScope === scope ? context : undefined,
    loadContext,
    loadAccounts,
    loadConversations,
    loadMessages,
    hasMore,
    hasOlder,
    olderLoading,
    phrases,
    sending,
    send,
    draft,
    setDraft,
    reference,
    setReference,
    stream,
    updateState,
    markViewed,
    handover,
  };
}
