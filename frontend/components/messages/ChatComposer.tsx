import React, { useEffect, useRef, useState } from 'react';
import { ImagePlus, Smile, Zap, Send, X, Loader2, Search } from 'lucide-react';
import type { QuickPhrase } from '../../types';
import { notify } from '../../services/feedback';
import { useQuickPhrase } from '../../services/api';
import type { ChatMessage } from '../../types';
import { EmojiIcon, MessageText, type OfficialEmoji } from './OfficialEmoji';
const COMMON = ['[微笑]','[可爱]','[哈哈]','[大笑]','[害羞]','[亲亲]','[爱心]','[握手]','[强]','[抱拳]','[谢谢]','[玫瑰]','[开心]','[调皮]','[鼓掌]','[呲牙]'];
export default function ChatComposer({
  draft,
  setDraft,
  send,
  disabled,
  sending,
  phrases,
  emojis,
  reference,
  cancelReference,
}: {
  key?: string;
  draft: string;
  setDraft: (s: string) => void;
  send: (file?: File) => Promise<{ ok: boolean; imageSent: boolean }>;
  disabled: boolean;
  sending: boolean;
  phrases: QuickPhrase[];
  emojis: OfficialEmoji[];
  reference?: ChatMessage;
  cancelReference: () => void;
}) {
  const input = useRef<HTMLTextAreaElement>(null),
    picker = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File>();
  const [url, setUrl] = useState('');
  const [panel, setPanel] = useState('');
  const [search, setSearch] = useState('');
  useEffect(() => {
    const el = input.current;
    if (el) {
      el.style.height = 'auto';
      el.style.height = `${Math.min(180, Math.max(64, el.scrollHeight))}px`;
    }
  }, [draft]);
  useEffect(() => { if (reference) input.current?.focus(); }, [reference?.messageId]);
  useEffect(() => {
    if (!file) {
      setUrl('');
      return;
    }
    const next = URL.createObjectURL(file);
    setUrl(next);
    return () => URL.revokeObjectURL(next);
  }, [file]);
  const choose = (value?: File) => {
    if (!value) return;
    if (reference) { notify('引用回复暂仅支持文字，请先取消引用再添加图片', 'warning'); return; }
    if (
      !['image/jpeg', 'image/png', 'image/gif', 'image/webp'].includes(value.type) ||
      value.size > 5 * 1024 * 1024
    ) {
      notify('请选择5MB以内的JPEG、PNG、GIF或WebP图片', 'warning');
      return;
    }
    setFile(value);
  };
  const insert = (text: string) => {
    const el = input.current;
    const start = el?.selectionStart ?? draft.length,
      end = el?.selectionEnd ?? start;
    const next = draft.slice(0, start) + text + draft.slice(end);
    if (next.length > 2000) {
      notify('消息最多2000字，请精简后发送', 'warning');
      return;
    }
    setDraft(next);
    setPanel('');
    requestAnimationFrame(() => {
      el?.focus();
      el?.setSelectionRange(start + text.length, start + text.length);
    });
  };
  const applyPhrase = async (phrase: QuickPhrase) => {
    if (phrase.content) insert(phrase.content);
    else setPanel('');
    void useQuickPhrase(phrase.id).catch(() => {});
    if (!phrase.image_url) return;
    const path = phrase.image_url.replace(/^\/+/, '');
    if (!/^static\/uploads\/images\/[A-Za-z0-9_.-]+\.(?:jpe?g|png|gif|webp)$/i.test(path)) {
      notify('快捷短语图片路径无效，请在系统设置中重新上传', 'error');
      return;
    }
    try {
      const response = await fetch(`/${path}`, { credentials: 'same-origin' });
      if (!response.ok) throw new Error('快捷短语图片读取失败');
      const blob = await response.blob();
      const mime = blob.type.toLowerCase();
      if (!['image/jpeg', 'image/png', 'image/gif', 'image/webp'].includes(mime) || blob.size > 5 * 1024 * 1024) {
        throw new Error('快捷短语图片格式无效或超过5MB');
      }
      choose(new File([blob], path.split('/').pop() || 'quick-phrase-image.jpg', { type: mime }));
    } catch (error) {
      notify((error as Error).message || '快捷短语图片读取失败', 'error');
    }
  };
  const submit = async () => {
    if (disabled || sending || (!draft.trim() && !file)) return;
    if (reference && file) { notify('引用回复暂仅支持文字，请先移除图片或取消引用', 'warning'); return; }
    const result = await send(file);
    if (result.imageSent) setFile(undefined);
  };
  return (
    <footer
      className="chat-composer"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        if (!disabled) choose(e.dataTransfer.files[0]);
      }}
    >
      {reference && <div className="mb-3 flex min-w-0 items-center gap-2 rounded-md border-l-4 border-[var(--brand)] bg-[var(--surface-subtle)] px-3 py-2" aria-label="正在引用回复">
        <div className="min-w-0 flex-1 text-xs text-[var(--text-muted)]">
          <strong>{reference.isSelf ? '我方' : reference.senderName || reference.senderId}</strong>
          <p className="truncate"><MessageText text={reference.text || '[图片]'} emojis={emojis} /></p>
        </div>
        {reference.images[0] && <img src={reference.images[0]} alt="被引用的图片" className="h-10 w-10 rounded object-cover" />}
        <button type="button" className="chat-icon" aria-label="取消引用回复" onClick={cancelReference}><X size={16} /></button>
      </div>}
      <div className="flex items-center gap-1.5 text-[var(--text-muted)]">
        <button
          type="button"
          className="chat-icon"
          aria-label="选择表情"
          aria-expanded={panel === 'emoji'}
          onClick={() => setPanel(panel === 'emoji' ? '' : 'emoji')}
        >
          <Smile size={19} />
        </button>
        <button
          type="button"
          className="chat-icon"
          aria-label="添加图片"
          disabled={disabled || sending}
          onClick={() => picker.current?.click()}
        >
          <ImagePlus size={19} />
        </button>
        <button
          type="button"
          className="chat-icon"
          aria-label="快捷短语"
          aria-expanded={panel === 'phrases'}
          onClick={() => setPanel(panel === 'phrases' ? '' : 'phrases')}
        >
          <Zap size={19} />
        </button>
        <span className="ml-auto text-[11px] text-[var(--text-soft)]">
          {file ? '发送前可移除图片' : 'Shift + Enter 换行'}
        </span>
        <input
          ref={picker}
          type="file"
          accept="image/jpeg,image/png,image/gif,image/webp"
          hidden
          onChange={(e) => {
            choose(e.target.files?.[0]);
            e.target.value = '';
          }}
        />
      </div>
      {panel && (
        <div
          className="chat-composer-popover"
          role="region"
          aria-label={panel === 'emoji' ? '表情面板' : '快捷短语面板'}
        >
          <div className="mb-2 flex items-center justify-between text-xs font-bold">
            <span>{panel === 'emoji' ? '常用表情' : '快捷短语 · 插入后可编辑'}</span>
            <button className="chat-icon" aria-label="关闭面板" onClick={() => setPanel('')}>
              <X size={15} />
            </button>
          </div>
          {panel === 'emoji' ? (
            <div className="grid grid-cols-4 gap-1">
              {(emojis.length ? emojis : COMMON.map(alias => ({ alias, name: alias.slice(1,-1), url: '' }))).map((item) => (
                <button
                  key={item.alias}
                  title={item.name}
                  aria-label={`表情 ${item.name}`}
                  className="rounded-md p-2 text-xs hover:bg-[var(--surface-hover)]"
                  onClick={() => insert(item.alias)}
                >
                  <EmojiIcon item={item} />
                </button>
              ))}
            </div>
          ) : (
            <>
              <div className="mb-2 flex items-center gap-2">
                <Search size={14} />
                <input
                  aria-label="搜索快捷短语"
                  placeholder="搜索分类、标题或内容"
                  className="ios-input min-w-0 flex-1 rounded-md px-2 py-2 text-xs"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </div>
              {phrases
                .filter((p) => [p.title, p.content, p.category].join(' ').includes(search))
                .map((p) => (
                  <button
                    className="block w-full rounded-md p-2 text-left hover:bg-[var(--surface-hover)]"
                    key={p.id}
                    onClick={() => void applyPhrase(p)}
                  >
                    <span className="block text-xs font-bold">
                      {p.category} · {p.title}
                    </span>
                    <span className="mt-1 block truncate text-xs text-[var(--text-muted)]">
                      {p.content || (p.image_url ? '插入图片' : '')}
                    </span>
                    {p.image_url && (
                      <img
                        src={`/${p.image_url.replace(/^\/+/, '')}`}
                        alt=""
                        aria-hidden="true"
                        className="mt-2 h-12 w-12 rounded border border-[var(--border)] object-cover"
                      />
                    )}
                  </button>
                ))}
              {!phrases.length && (
                <p className="p-2 text-xs text-[var(--text-muted)]">
                  暂无快捷短语，可在系统设置中添加。
                </p>
              )}
            </>
          )}
        </div>
      )}
      {file && (
        <div className="mt-2 flex items-center gap-3 rounded-lg bg-[var(--surface-subtle)] p-2">
          <img className="h-16 w-16 rounded object-contain" src={url} alt="待发送图片" />
          <span className="min-w-0 flex-1 truncate text-xs">
            {file.name}
            <small className="mt-1 block text-[var(--text-soft)]">
              {Math.ceil(file.size / 1024)}KB · 点击发送后上传
            </small>
          </span>
          <button
            aria-label="移除图片"
            className="chat-icon"
            disabled={sending}
            onClick={() => setFile(undefined)}
          >
            <X size={16} />
          </button>
        </div>
      )}
      <textarea
        ref={input}
        aria-label="消息内容"
        maxLength={2000}
        className="chat-input"
        value={draft}
        disabled={disabled || sending}
        placeholder={disabled ? '账号离线或认证暂停，暂时无法发送' : '输入消息，或粘贴／拖入图片…'}
        onChange={(e) => setDraft(e.target.value)}
        onPaste={(e) => {
          const f = Array.from(e.clipboardData.files as FileList).find((f) =>
            f.type.startsWith('image/')
          );
          if (f && !disabled) {
            e.preventDefault();
            choose(f);
          }
        }}
        onKeyDown={(e) => {
          if (
            e.key === 'Enter' &&
            !e.shiftKey &&
            !e.nativeEvent.isComposing &&
            e.nativeEvent.keyCode !== 229 &&
            window.matchMedia('(pointer:fine)').matches
          ) {
            e.preventDefault();
            void submit();
          }
        }}
      />
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] text-[var(--text-soft)]">
          草稿按账号与会话保存 · {draft.length}/2000
        </span>
        <button
          type="button"
          className="ios-btn-primary flex items-center gap-2 rounded-md px-4 py-2 text-sm"
          disabled={disabled || sending || (!draft.trim() && !file) || draft.length > 2000}
          onClick={() => void submit()}
        >
          {sending ? <Loader2 size={16} className="animate-spin" /> : <Send size={16} />}发送
        </button>
      </div>
    </footer>
  );
}
