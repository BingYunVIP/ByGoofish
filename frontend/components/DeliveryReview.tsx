import React, { useEffect, useState } from 'react';
import { get, post } from '../lib/request';
import { confirmAction, notify } from '../services/feedback';

type Attempt = { cookie_id: string; order_id: string; status: string; reserved_count: number; reviewable: boolean };

export default function DeliveryReview() {
  const [attempts, setAttempts] = useState<Attempt[]>([]);
  const [busy, setBusy] = useState('');
  const load = () => get<{ data: Attempt[] }>('/delivery-attempts').then(value => setAttempts(value.data));
  useEffect(() => { void load().catch(() => {}); }, []);
  const review = async (attempt: Attempt, action: 'delivered' | 'release') => {
    const message = action === 'delivered'
      ? '请先核对买家确实收到全部内容。此操作只记录已送达，不会再次发送。'
      : '只有确认买家没有收到任何内容，才可将预留库存归还。释放后后续事件可以重新发货，确认继续？';
    if (!await confirmAction(message)) return;
    setBusy(attempt.order_id);
    try {
      await post(`/delivery-attempts/${encodeURIComponent(attempt.cookie_id)}/${encodeURIComponent(attempt.order_id)}/review`, { action, confirmed: true });
      notify('已记录核查结果', 'success'); await load();
    } catch { notify('处理失败：发货可能仍在执行或状态已改变，请刷新核对', 'error'); }
    finally { setBusy(''); }
  };
  if (!attempts.length) return null;
  return <section className="rounded-md border border-amber-300 bg-amber-50 p-4" aria-label="发货核查">
    <h2 className="font-bold">待核查发货</h2>
    <p className="mt-1 text-sm">以下订单已暂停自动重试。请核对实际送达情况，避免重复发货。</p>
    {attempts.map(attempt => <div key={attempt.order_id} className="mt-3 flex flex-wrap items-center gap-2 text-sm">
      <span>订单 {attempt.order_id} · 预留 {attempt.reserved_count} 条库存</span>
      <button type="button" className="ios-btn-secondary px-3 py-2" disabled={!attempt.reviewable || !!busy} onClick={() => void review(attempt, 'delivered')}>已确认全部送达</button>
      <button type="button" className="ios-btn-secondary px-3 py-2" disabled={!attempt.reviewable || !!busy} onClick={() => void review(attempt, 'release')}>未送达，释放库存</button>
      {!attempt.reviewable && <span>处理中，请稍后刷新</span>}
    </div>)}
  </section>;
}
