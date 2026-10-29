import { confirmedMessage } from '../../lib/chatWorkspace.mjs';
import { createPortal } from 'react-dom';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Copy, Forward, Reply, X, Search } from 'lucide-react';
import { post } from '../../lib/request';
import { notify } from '../../services/feedback';
import type { ChatMessage } from '../../types';
import type { OutgoingMessage } from './workspaceTypes';
import { MessageText, type OfficialEmoji } from './OfficialEmoji';
import { useDialogFocus } from '../../lib/useDialogFocus';

export type MenuTarget = { message: OutgoingMessage; x: number; y: number; selection: string; anchor: HTMLElement };
export function actionable(message: OutgoingMessage) {
  return Boolean(message.canReference && confirmedMessage(message) && !message.event_type && ['text','image'].includes(message.type));
}
export function MessageMenu({ target, close, quote, forward, capabilities }: {
  capabilities: Record<string,{supported:boolean;reason:string}>;
  target: MenuTarget; close: () => void; quote: () => void; forward: () => void;
}) {
  const menu = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ left: target.x, top: target.y });
  const [fallback, setFallback] = useState('');
  useLayoutEffect(() => {
    const box = menu.current?.getBoundingClientRect();
    if (box) setPosition({ left: Math.max(8, Math.min(target.x, innerWidth - box.width - 8)), top: Math.max(8, Math.min(target.y, innerHeight - box.height - 8)) });
    menu.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus();
  }, [target]);
  useEffect(() => {
    const dismiss = (event: PointerEvent) => { if (!menu.current?.contains(event.target as Node)) close(); };
    const scroll = () => close();
    document.addEventListener('pointerdown', dismiss);window.addEventListener('resize', scroll);
    return () => { document.removeEventListener('pointerdown', dismiss);window.removeEventListener('resize', scroll);target.anchor?.focus({preventScroll:true}); };
  }, [target, close]);
  const copy = async (value: string) => {
    try { await navigator.clipboard.writeText(value);notify('已复制');close(); }
    catch { setFallback(value); }
  };
  const allowed = actionable(target.message) && capabilities.reply_text?.supported;
  const canForward = Boolean(capabilities[target.message.type==='image'?'forward_image':'forward_text']?.supported && target.message.canForward && confirmedMessage(target.message));
  return createPortal(<div ref={menu} role="menu" aria-label="消息操作" className="message-context-menu" style={position}
    onContextMenu={event => event.preventDefault()} onKeyDown={event => {
      if (event.key === 'Escape') { event.preventDefault();close(); }
      if (['ArrowDown','ArrowUp','Home','End'].includes(event.key)) {
        event.preventDefault();const items = Array.from(menu.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') || []) as HTMLButtonElement[];
        const index = items.indexOf(document.activeElement as HTMLButtonElement);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length-1 : (index+(event.key==='ArrowDown'?1:-1)+items.length)%items.length;
        items[next]?.focus();
      }
      if (event.key === 'Tab') close();
    }}>
    <button role="menuitem" disabled={!allowed} title={allowed ? '引用此消息回复' : capabilities.reply_text?.reason || '仅支持已确认的普通文字、图片消息'} onClick={() => { close();quote(); }}><Reply size={16} />引用回复</button>
    <button role="menuitem" disabled={!target.message.text && !target.selection} onClick={() => void copy(target.selection || target.message.text)}><Copy size={16} />复制文字</button>
    <button role="menuitem" disabled={!target.message.images.length} onClick={() => void copy(target.message.images.join('\n'))}><Copy size={16} />复制图片链接</button>
    <button role="menuitem" disabled={!canForward} title={canForward ? '转发到当前账号的会话' : capabilities[target.message.type==='image'?'forward_image':'forward_text']?.reason || '此消息暂不支持转发'} onClick={() => { close();forward(); }}><Forward size={16} />转发</button>
    {!allowed && <p className="px-3 py-2 text-[11px] text-[var(--text-soft)]">{!capabilities.reply_text?.supported ? capabilities.reply_text?.reason || '原生引用验收中' : '仅支持有平台记录的文字与图片'}</p>}
    {fallback && <label className="block p-2 text-xs">剪贴板不可用，请选中复制<textarea autoFocus readOnly value={fallback} onFocus={e => e.currentTarget.select()} className="ios-input mt-2 w-full rounded p-2" /></label>}
  </div>,document.body);
}
export type ForwardRecipient = { cid: string; otherUserId: string; otherUserName: string; itemTitle: string; lastMessageTime: number };
export function ForwardDialog({ owner, account, accountName, scopeLabel, scopeKey, sourceCid, message, recipients, hasMore, loading, loadMore, emojis, close, sent }: {
  owner: number; account: string; accountName: string; scopeLabel: string; scopeKey: string;
  sourceCid: string; message: ChatMessage; recipients: ForwardRecipient[]; hasMore: boolean; loading: boolean; loadMore: () => void;
  emojis: OfficialEmoji[]; close: () => void; sent: (cid: string) => void;
}) {
  const [search,setSearch] = useState(''), [target,setTarget] = useState(''), [busy,setBusy] = useState(false), [uncertain,setUncertain] = useState(false);
  const request = useRef(crypto.randomUUID());
  const dialog = useDialogFocus(true,() => { if (!busy) close(); });
  const needle = search.trim().toLocaleLowerCase();
  const candidates = recipients.filter(c => !needle || [c.otherUserName,c.otherUserId,c.itemTitle].some(v => String(v || '').toLocaleLowerCase().includes(needle)));
  const selected = candidates.find(c => c.cid === target);
  const latest = useRef({ candidates, scopeKey });latest.current = { candidates, scopeKey };
  useEffect(() => { setTarget(''); }, [scopeKey]);
  useEffect(() => { if (!selected && !busy && !uncertain) setTarget(''); }, [selected,busy,uncertain]);
  const stamp = (n: number) => new Date(n < 1e12 ? n*1000 : n).toLocaleString('zh-CN', { hour12: false });
  const submit = async () => {
    if (busy || uncertain || !selected || latest.current.scopeKey !== scopeKey || !latest.current.candidates.some(c => c.cid === target && c.otherUserId === selected.otherUserId)) return;
    const operationKey = `chat-forward:${owner}:${account}:${sourceCid}:${message.messageId}:${target}`;
    try { const existing=localStorage.getItem(operationKey);if (existing) request.current=existing;else localStorage.setItem(operationKey,request.current); } catch {}
    setBusy(true);
    try {
      const result = await post<any>(`/chat/forward/${encodeURIComponent(account)}`, { source_cid:sourceCid,message_id:message.messageId,target_cid:target,client_request_id:request.current });
      if (result.data?.status !== 'sent') { setUncertain(true);return; }
      try { localStorage.removeItem(operationKey); } catch {}
      notify('消息已转发');sent(target);close();
    } catch (error: any) {
      const notSent = error.response?.headers?.['x-chat-send-status'] === 'not_sent' || [400,403,404,422].includes(error.response?.status);
      if (notSent) { try { localStorage.removeItem(operationKey); } catch {} request.current=crypto.randomUUID(); }
      else setUncertain(true);
      notify(error.response?.data?.detail || '转发结果未确认，请检查目标会话', 'error');
    } finally { setBusy(false); }
  };
  return createPortal(<div className="modal-overlay"><div ref={dialog} role="dialog" aria-modal="true" aria-label="转发消息" className="modal-container forward-dialog">
    <div className="modal-header flex items-center justify-between gap-3"><h3 className="font-bold">转发消息</h3><button className="chat-icon" aria-label="关闭转发" disabled={busy} onClick={close}><X size={18} /></button></div>
    <div className="modal-body min-h-0 space-y-4">
      <div className="space-y-1 text-xs text-[var(--text-muted)]"><p className="break-words font-bold text-[var(--text)]">发送账号：{accountName}</p><p aria-label="转发筛选范围" className="break-words">{scopeLabel}</p><p>与左侧筛选一致 · {candidates.length} 个可选会话</p></div>
      <section aria-label="转发内容预览" className="max-h-44 overflow-auto rounded-md border border-[var(--border)] bg-[var(--surface-subtle)] p-3 text-sm">
        <p className="mb-2 text-xs font-bold text-[var(--text-muted)]">{message.type === 'image' ? '图片消息' : '文字消息'}</p>
        {message.reply && <p className="mb-2 break-words border-l-2 border-[var(--border)] pl-2 text-xs text-[var(--text-soft)]">引用：{message.reply.text || '原消息未加载'}</p>}
        <MessageText text={message.text} emojis={emojis} />{message.images.map(url => <img key={url} src={url} alt="转发图片预览" className="mt-2 max-h-32 max-w-full rounded object-contain" />)}
      </section>
      <label className="flex items-center gap-2"><Search size={16} className="shrink-0" /><input aria-label="搜索转发会话" disabled={busy || uncertain} className="ios-input min-w-0 w-full rounded-md px-3 py-2 text-sm" placeholder="在当前筛选结果中搜索联系人或商品" value={search} onChange={e => setSearch(e.target.value)} /></label>
      <div className="max-h-64 space-y-1 overflow-auto" role="listbox" aria-label="选择转发目标">
        {candidates.map(c => <button type="button" role="option" aria-selected={target===c.cid} disabled={busy || uncertain} key={c.cid}
          className={`block min-h-11 w-full rounded-md border px-3 py-2 text-left text-sm ${target===c.cid?'border-[var(--brand)] bg-[var(--brand-soft)]':'border-transparent hover:bg-[var(--surface-hover)]'}`}
          onClick={() => { setTarget(c.cid);request.current=crypto.randomUUID(); }}>
          <span className="block break-words font-bold">{c.otherUserName || c.otherUserId}{c.cid===sourceCid?'（当前会话）':''}</span>
          <span className="block break-words text-xs text-[var(--text-soft)]">ID：{c.otherUserId} · {stamp(c.lastMessageTime)}</span>
          {c.itemTitle && <span className="block truncate text-xs text-[var(--text-muted)]">{c.itemTitle}</span>}</button>)}
      </div>
      {!candidates.length && <p role="status" className="py-3 text-center text-sm text-[var(--text-muted)]">当前筛选下暂无可转发会话，可返回左侧调整筛选。</p>}
      {hasMore && <button type="button" disabled={busy || uncertain || loading} onClick={loadMore} className="ios-btn-secondary w-full rounded-md px-3 py-2 text-xs disabled:opacity-50">{loading?'加载中…':'加载更多会话（仍应用当前筛选）'}</button>}
      {selected && <p aria-label="转发接收方" className="break-words rounded-md bg-[var(--brand-soft)] p-3 text-sm">{accountName} → <strong>{selected.otherUserName || selected.otherUserId}</strong><span className="block text-xs text-[var(--text-muted)]">ID：{selected.otherUserId}</span></p>}
      {uncertain && <p role="status" className="text-sm text-[var(--text-muted)]">转发结果待核查，请在目标会话查看历史，勿重复发送。</p>}
    </div><div className="modal-footer flex shrink-0 justify-end gap-2"><button className="ios-btn-secondary rounded-md px-4 py-2 text-sm" disabled={busy} onClick={close}>取消</button><button disabled={!selected || busy || uncertain} className="ios-btn-primary rounded-md px-4 py-2 text-sm disabled:opacity-50" onClick={() => void submit()}>{busy?'转发中…':'确认转发'}</button></div>
  </div></div>,document.body);
}
