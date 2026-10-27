import React, { useEffect, useState } from 'react';
import { PauseCircle, ShieldCheck } from 'lucide-react';
import type { ConversationContext } from './workspaceTypes';
export default function HandoverBanner({
  context,
  onChange,
}: {
  context?: ConversationContext;
  onChange: (action: 'pause' | 'resume') => Promise<void>;
}) {
  const [now, setNow] = useState(Date.now()),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!context?.handover.paused) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [context?.handover.paused, context?.handover.expires_at]);
  const seconds = context?.handover.paused
    ? Math.max(0, Math.ceil(context.handover.expires_at - now / 1000))
    : 0;
  const change = async () => {
    setBusy(true);
    try {
      await onChange(seconds > 0 ? 'resume' : 'pause');
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-[var(--border)] bg-[var(--surface-subtle)] px-4 py-2 text-xs">
      <span className="flex items-center gap-1.5 text-[var(--text-muted)]">
        {seconds > 0 ? <PauseCircle size={14} /> : <ShieldCheck size={14} />}{' '}
        {seconds > 0
          ? `人工接管 · ${Math.floor(seconds / 60)}分${seconds % 60}秒后恢复`
          : '自动回复按账号规则运行'}
      </span>
      <button
        className="font-bold text-[var(--brand-text)] disabled:text-[var(--text-soft)]"
        disabled={busy || !context || (!seconds && !context.handover.duration_minutes)}
        onClick={() => void change()}
      >
        {busy
          ? '处理中…'
          : seconds > 0
            ? '恢复自动回复'
            : context?.handover.duration_minutes === 0
              ? '暂停时长为0，请先设置'
              : '人工接管'}
      </button>
    </div>
  );
}
