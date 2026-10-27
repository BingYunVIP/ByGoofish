import type { OutgoingMessage } from './workspaceTypes';

export default function MessageSendStatus({ status, onCheck }: {
  status?: OutgoingMessage['send_status'];
  onCheck: () => void;
}) {
  if (!status) return null;
  return (
    <div className="mt-1 flex items-center justify-end gap-2 text-[10px]" role="status">
      <span className={status === 'unknown' ? 'text-[var(--warning)]'
        : status === 'failed' ? 'text-[var(--danger)]' : 'text-[var(--text-muted)]'}>
        {{ sending: '发送中', sent: '已发送', failed: '发送失败', unknown: '结果待核查' }[status]}
      </span>
      {(status === 'unknown' || status === 'failed') && (
        <button type="button" className="underline" onClick={onCheck}>核查历史</button>
      )}
    </div>
  );
}
