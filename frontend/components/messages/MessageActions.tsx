import { createPortal } from 'react-dom';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Copy, Forward, Reply, X, Search } from 'lucide-react';
import { post } from '../../lib/request';
import { notify } from '../../services/feedback';
import type { ChatMessage } from '../../types';
import type { OutgoingMessage, WorkspaceConversation } from './workspaceTypes';
import { MessageText, type OfficialEmoji } from './OfficialEmoji';
import { useDialogFocus } from '../../lib/useDialogFocus';

export type MenuTarget = { message: OutgoingMessage; x: number; y: number; selection: string; anchor: HTMLElement };
export function actionable(message: OutgoingMessage) {
  return Boolean(message.canReference && message.messageId && !message.client_request_id &&
    (!message.send_status || message.send_status === 'sent') && !message.event_type && ['text','image'].includes(message.type));
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
  const canForward = Boolean(capabilities[target.message.type==='image'?'forward_image':'forward_text']?.supported && target.message.canForward && !target.message.client_request_id && (!target.message.send_status || target.message.send_status==='sent'));
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
export function ForwardDialog({ owner, account, sourceCid, message, conversations, emojis, close, sent }: {
  owner: number; account: string; sourceCid: string; message: ChatMessage; conversations: WorkspaceConversation[];
  emojis: OfficialEmoji[]; close: () => void; sent: (cid: string) => void;
}) {
  const [search,setSearch] = useState(''), [target,setTarget] = useState(''), [busy,setBusy] = useState(false), [uncertain,setUncertain] = useState(false);
  const request = useRef(crypto.randomUUID());
  const dialog = useDialogFocus(true,() => { if (!busy) close(); });
  const selected = conversations.find(c => c.cid === target);
  return createPortal(<div className="modal-overlay"><div ref={dialog} role="dialog" aria-modal="true" aria-label="转发消息" className="modal-container max-w-lg">
    <div className="modal-header flex items-center justify-between"><h3 className="font-bold">转发消息</h3><button className="chat-icon" aria-label="关闭转发" disabled={busy} onClick={close}><X size={18} /></button></div>
    <div className="modal-body space-y-4">
      <p className="text-xs text-[var(--text-muted)]">仅转发到当前账号内的一个会话</p>
      <div className="rounded-md border border-[var(--border)] p-3 text-sm"><MessageText text={message.text} emojis={emojis} />{message.images.map(url => <img key={url} src={url} alt="转发图片预览" className="mt-2 max-h-40 max-w-full rounded" />)}</div>
      <label className="flex items-center gap-2"><Search size={16} /><input aria-label="搜索转发会话" disabled={busy || uncertain} className="ios-input min-w-0 w-full rounded-md p-2" placeholder="搜索已加载联系人或商品" value={search} onChange={e => setSearch(e.target.value)} /></label>
      <div className="max-h-56 space-y-1 overflow-auto" role="listbox" aria-label="选择转发目标">
        {conversations.filter(c => [c.otherUserName,c.otherUserId,c.itemTitle].join(' ').includes(search)).map(c => <button type="button" role="option" aria-selected={target===c.cid} disabled={busy || uncertain} key={c.cid}
          className={`block w-full rounded-md px-3 py-2 text-left text-sm ${target===c.cid?'bg-[var(--brand-soft)]':'hover:bg-[var(--surface-hover)]'}`}
          onClick={() => { setTarget(c.cid);request.current=crypto.randomUUID(); }}>{c.otherUserName || c.otherUserId}{c.cid===sourceCid?'（当前会话）':''}<span className="block truncate text-xs text-[var(--text-soft)]">{c.itemTitle}</span></button>)}
      </div>
      {selected && <p className="text-sm">发送给：<strong>{selected.otherUserName || selected.otherUserId}</strong></p>}
      {uncertain && <p role="status" className="text-sm text-[var(--text-muted)]">转发结果待核查，请在目标会话查看历史，勿重复发送。</p>}
    </div><div className="modal-footer flex justify-end gap-2"><button className="ios-btn-secondary rounded-md px-4 py-2" disabled={busy} onClick={close}>取消</button><button disabled={!target || busy || uncertain} className="ios-btn-primary rounded-md px-4 py-2 disabled:opacity-50" onClick={async () => {
      const operationKey = `chat-forward:${owner}:${account}:${sourceCid}:${message.messageId}:${target}`;
      try { const existing=localStorage.getItem(operationKey);if (existing) request.current=existing;else localStorage.setItem(operationKey,request.current); } catch {}
      setBusy(true);
      try {
        const result = await post<any>(`/chat/forward/${encodeURIComponent(account)}`, { source_cid:sourceCid,message_id:message.messageId,target_cid:target,client_request_id:request.current });
        if (result.data?.status !== 'sent') { setUncertain(true);return; }
        try { localStorage.removeItem(operationKey); } catch {}
        notify('消息已转发');sent(target);close();
      } catch (error: any) {
        if (!error.response || ![400,403,404,409,422].includes(error.response.status)) setUncertain(true);
        notify(error.response?.data?.detail || '转发结果未确认，请检查目标会话', 'error');
      } finally { setBusy(false); }
    }}>{busy?'转发中…':'确认转发'}</button></div>
  </div></div>,document.body);
}
