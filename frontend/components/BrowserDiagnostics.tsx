import React, { useEffect, useState } from 'react';
import { Copy, Loader2 } from 'lucide-react';
import type { CaptchaDiagnostic, CaptchaDiagnosticEntry } from '../types';
import { getCaptchaDiagnostics } from '../services/api';
import { notify } from '../services/feedback';

const statusLabel = { processing: '采集中', completed: '验证页面已结束', failed: '验证未完成', cancelled: '已取消' };

/** 仅轮询服务器已采集的脱敏记录，不请求验证平台。 */
export default function BrowserDiagnostics({ diagnosticId, defaultOpen = false, emptyText = '当时未采集浏览器日志' }: {
  diagnosticId?: string | null; defaultOpen?: boolean; emptyText?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [meta, setMeta] = useState<CaptchaDiagnostic | null>(null);
  const [entries, setEntries] = useState<CaptchaDiagnosticEntry[]>([]);
  const [error, setError] = useState('');
  const [copying, setCopying] = useState(false);
  useEffect(() => {
    setMeta(null); setEntries([]); setError('');
    if (!open || !diagnosticId) return;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let cursor = 0;
    const poll = async () => {
      try {
        const { data } = await getCaptchaDiagnostics(diagnosticId, cursor, abort.signal);
        if (abort.signal.aborted) return;
        cursor = data.next_seq;
        setMeta(data); setError('');
        setEntries(previous => [...previous, ...data.entries].slice(-500));
        // 展开时每秒增量读取；关闭后停止轮询。
        timer = setTimeout(poll, data.has_more ? 0 : 1000);
      } catch {
        if (abort.signal.aborted) return;
        setError('日志暂不可用：可能已超过7天保留期或无权访问。');
      }
    };
    void poll();
    return () => { abort.abort(); if (timer) clearTimeout(timer); };
  }, [open, diagnosticId]);

  const copy = async () => {
    if (!diagnosticId || copying) return;
    setCopying(true);
    try {
      // 从鉴权接口重新读取所有分页，复制内容与界面使用同一脱敏数据源。
      let cursor = 0;
      const all: CaptchaDiagnosticEntry[] = [];
      let data: CaptchaDiagnostic;
      do {
        ({ data } = await getCaptchaDiagnostics(diagnosticId, cursor));
        all.push(...data.entries);
        cursor = data.next_seq;
      } while (data.has_more && all.length < 500);
      const content = JSON.stringify({ ...data, entries: all, next_seq: undefined, has_more: undefined }, null, 2);
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(content);
      else {
        const textarea = document.createElement('textarea');
        textarea.value = content;
        textarea.style.position = 'fixed'; textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        try { textarea.select(); if (!document.execCommand('copy')) throw new Error('copy'); }
        finally { textarea.remove(); }
      }
      notify('脱敏日志已复制', 'success');
    } catch { notify('复制失败，请确认日志仍在保留期内及浏览器剪贴板权限', 'error'); }
    finally { setCopying(false); }
  };

  return <details open={open} onToggle={event => setOpen(event.currentTarget.open)}
    className="min-w-0 rounded-md border border-[var(--border)] bg-[var(--surface)] text-sm">
    <summary className="cursor-pointer rounded-md px-3 py-3 font-semibold text-gray-900">浏览器日志</summary>
    {open && <div className="min-w-0 space-y-3 border-t border-[var(--border)] p-3">
      <p className="text-xs leading-5 text-gray-500">保留7天，每轮最多500条，每条最多512字符。仅查询本地日志，不会额外请求闲鱼。</p>
      {!diagnosticId ? <p className="text-xs text-gray-500">{emptyText}</p> : <>
        <p className="break-all font-mono text-xs text-gray-500">诊断 ID：{diagnosticId}</p>
        {meta && <div className="flex flex-wrap items-center gap-2 text-xs text-gray-700">
          <span className="status-badge">{statusLabel[meta.status] || '会话已结束'}</span>
          {meta.error_code && <span>页面错误码：{meta.error_code}</span>}
          {meta.close_reason === 'cancelled' && meta.failure_category && <span>结束操作：已取消；保留首次错误</span>}
          <span>已记录 {meta.event_count} 条，超限截断 {meta.dropped_count} 条</span>
        </div>}
        {error && <p role="status" className="text-xs text-red-600">{error}</p>}
        <ol aria-label="脱敏浏览器日志" className="max-h-64 space-y-2 overflow-y-auto rounded-md bg-gray-50 p-3 font-mono text-xs leading-5 text-gray-700">
          {entries.map(entry => <li key={entry.seq} className="break-all">
            <span className="text-gray-500">{entry.seq}. {new Date(entry.created_at * 1000).toLocaleTimeString('zh-CN', { hour12: false })} [{entry.level}] {entry.kind} </span>
            {entry.message}
          </li>)}
          {!entries.length && !error && <li>等待诊断记录…</li>}
        </ol>
        <button type="button" onClick={() => void copy()} disabled={copying || !meta}
          className="ios-btn-secondary inline-flex items-center gap-2 rounded-md px-3 py-2 text-sm disabled:opacity-50">
          {copying ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : <Copy className="h-4 w-4" aria-hidden="true" />}
          复制脱敏日志
        </button>
      </>}
    </div>}
  </details>;
}
