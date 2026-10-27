import { shipOrderResult } from '../../lib/chatWorkspace.mjs';
import React, { useState } from 'react';
import { Eye, Truck, RefreshCw, Flower2, Star } from 'lucide-react';
import { OrderDetailsDialog, ShipOrderDialog, RateOrderDialog } from '../OrderDialogs';
import DeliveryReview from '../DeliveryReview';
import type { WorkspaceOrder } from './workspaceTypes';
import {
  manualShipOrder,
  syncSingleOrder,
  requireOrderFlower,
  rateOrders,
} from '../../services/api';
import { confirmAction, notify } from '../../services/feedback';
export default function OrderWorkspaceActions({
  order,
  features,
  refresh,
}: {
  key?: string;
  order: WorkspaceOrder;
  features: {
    auto_rate_enabled: boolean;
    auto_flower_enabled: boolean;
    auto_rate_template?: string;
  };
  refresh: () => Promise<void>;
}) {
  const [dialog, setDialog] = useState(''),
    [busy, setBusy] = useState(false),
    [result, setResult] = useState<{ success: boolean; message: string } | null>(null),
    [feedback, setFeedback] = useState('感谢惠顾，欢迎下次光临！'),
    [rating, setRating] = useState<1 | 0 | -1>(1),
    [anonymous, setAnonymous] = useState(false);
  const status = order.order_status || order.status;
  const blocked =
    Boolean(order.delivery_attempt && order.delivery_attempt.status !== 'released') ||
    Boolean(order.system_shipped) ||
    status !== 'pending_ship';
  const handled = (action: string) => order.interaction_attempts?.some((v) => v.action === action);
  const run = async (fn: () => Promise<any>) => {
    setBusy(true);
    try {
      const r = await fn();
      if (r.success === false) throw new Error(r.message || r.detail || '平台未确认成功');
      notify(r.message || '操作已完成', 'success');
      return r;
    } catch (e) {
      notify((e as Error).message || '结果待核查，请先刷新记录', 'error');
      return null;
    } finally {
      await refresh();
      setBusy(false);
    }
  };
  const ship = async (mode: 'status_only' | 'full_delivery') => {
    if (blocked || busy) return;
    if (
      !(await confirmAction(
        mode === 'full_delivery'
          ? '确认按当前商品绑定消耗库存并给本订单买家发送卡密？'
          : '确认仅修改闲鱼发货状态？此操作不发送卡密。'
      ))
    )
      return;
    const r = await run(async () => shipOrderResult(await manualShipOrder([order.order_id], mode)));
    setResult({
      success: !!r,
      message: r?.message || '请刷新并核查实际发货结果，不要直接重复发送。',
    });
  };
  return (
    <>
      <div className="grid grid-cols-2 gap-2 text-xs">
        <button
          className="ios-btn-secondary flex items-center justify-center gap-1 rounded-md p-2"
          onClick={() => setDialog('detail')}
        >
          <Eye size={14} />
          订单详情
        </button>
        <button
          className="ios-btn-secondary flex items-center justify-center gap-1 rounded-md p-2"
          disabled={busy}
          onClick={() => void run(() => syncSingleOrder(order.order_id))}
        >
          <RefreshCw size={14} />
          刷新官方状态
        </button>
        <button
          className="ios-btn-secondary flex items-center justify-center gap-1 rounded-md p-2"
          disabled={blocked || busy}
          onClick={() => {
            setResult(null);
            setDialog('ship');
          }}
        >
          <Truck size={14} />
          立即发货
        </button>
        <button
          className="ios-btn-secondary flex items-center justify-center gap-1 rounded-md p-2"
          disabled={
            status !== 'completed' || !features.auto_flower_enabled || handled('flower') || busy
          }
          onClick={() =>
            void (async () => {
              if (await confirmAction('确认向此订单买家索要一次小红花？'))
                await run(() => requireOrderFlower(order.order_id));
            })()
          }
        >
          <Flower2 size={14} />
          {handled('flower') ? '求花已处理' : '索要小红花'}
        </button>
        <button
          className="ios-btn-secondary col-span-2 flex items-center justify-center gap-1 rounded-md p-2"
          disabled={
            status !== 'completed' || !features.auto_rate_enabled || handled('rate') || busy
          }
          onClick={() => {
            setFeedback(features.auto_rate_template || '感谢惠顾，欢迎下次光临！');
            setRating(1);
            setAnonymous(false);
            setDialog('rate');
          }}
        >
          <Star size={14} />
          {handled('rate') ? '评价已处理' : '评价买家'}
        </button>
      </div>
      {order.delivery_attempt && order.delivery_attempt.status !== 'released' && (
        <div className="mt-3">
          <DeliveryReview orderId={order.order_id} onChanged={refresh} />
        </div>
      )}
      {dialog === 'detail' && (
        <OrderDetailsDialog
          selectedOrder={{ ...order, status: status as any }}
          onClose={() => setDialog('')}
          onShip={blocked ? undefined : () => setDialog('ship')}
        />
      )}
      {dialog === 'ship' && (
        <ShipOrderDialog
          shipLoading={busy}
          shipDisabled={blocked}
          shipResult={result}
          onClose={() => {
            if (!busy) setDialog('');
          }}
          onExecute={(mode) => void ship(mode)}
        />
      )}
      {dialog === 'rate' && (
        <RateOrderDialog
          rateOrder={{ ...order, status: status as any }}
          rateFeedback={feedback}
          setRateFeedback={setFeedback}
          rateValue={rating}
          setRateValue={setRating}
          rateAnonymous={anonymous}
          setRateAnonymous={setAnonymous}
          rateSubmitting={busy}
          onClose={() => {
            if (!busy) setDialog('');
          }}
          handleSubmitRate={() =>
            void run(() => rateOrders([order.order_id], feedback, rating, anonymous)).then((r) => {
              if (r) setDialog('');
            })
          }
        />
      )}
    </>
  );
}
