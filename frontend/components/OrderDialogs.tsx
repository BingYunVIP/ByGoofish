import React, { useRef } from 'react';
import { useModalFocus } from '../lib/useModalFocus';
import { createPortal } from 'react-dom';
import { X, Truck, PackageCheck, RefreshCw } from 'lucide-react';
import type { Order, OrderStatus } from '../types';
import { formatOrderTime } from '../lib/orderTime';
export const StatusBadge: React.FC<{ status: OrderStatus }> = ({ status }) => {
  const styles = {
    processing: 'bg-yellow-100 text-yellow-800',
    pending_ship: 'bg-[#ffe100] text-black',
    shipped: 'bg-blue-100 text-blue-700',
    completed: 'bg-green-100 text-green-700',
    cancelled: 'bg-gray-100 text-gray-500',
    refunding: 'bg-red-100 text-red-600',
  };

  const labels = {
    processing: '处理中',
    pending_ship: '待发货',
    shipped: '已发货',
    completed: '已完成',
    cancelled: '已取消',
    refunding: '退款中',
  };

  return (
    <span
      className={`inline-flex rounded px-2 py-1 text-xs font-bold ${styles[status] || styles.cancelled}`}
    >
      {labels[status] || status}
    </span>
  );
};

export function OrderDetailsDialog({
  selectedOrder,
  onClose,
  onShip,
  itemTitle,
}: {
  selectedOrder: Order;
  onClose: () => void;
  onShip?: (id: string) => void;
  itemTitle?: string;
}) {
  const modal = useRef<HTMLDivElement>(null);
  useModalFocus(true, onClose, modal);
  return createPortal(
    <div
      ref={modal}
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="订单操作"
    >
      <div className="modal-container">
        <div className="modal-header">
          <div className="flex w-full items-start justify-between gap-4">
            <div>
              <h3 className="text-lg font-bold text-gray-900">订单详情</h3>
              <p className="mt-1 break-all text-xs text-gray-500">{selectedOrder.order_id}</p>
            </div>
            <button
              type="button"
              onClick={onClose}
              className="rounded-md p-2 hover:bg-gray-100"
              aria-label="关闭订单详情"
            >
              <X className="w-5 h-5 text-gray-600" />
            </button>
          </div>
        </div>

        <div className="modal-body space-y-6">
          {/* Order Info */}
          <div className="space-y-4">
            <h4 className="text-lg font-bold text-gray-800">订单信息</h4>
            <div className="grid gap-4 rounded-md border border-gray-200 bg-gray-50 p-4 sm:grid-cols-2">
              <div>
                <div className="text-xs text-gray-500 mb-1">订单号</div>
                <div className="font-mono text-sm font-bold text-gray-900">
                  {selectedOrder.order_id}
                </div>
              </div>
              <div>
                <div className="text-xs text-gray-500 mb-1">状态</div>
                <StatusBadge status={selectedOrder.status} />
              </div>
              <div>
                <div className="text-xs text-gray-500 mb-1">实付金额</div>
                <div className="text-lg font-extrabold text-gray-900">¥{selectedOrder.amount}</div>
              </div>
              <div>
                <div className="text-xs text-gray-500 mb-1">数量</div>
                <div className="font-bold text-gray-900">{selectedOrder.quantity}</div>
              </div>
              <div className="sm:col-span-2">
                <div className="text-xs text-gray-500 mb-1">创建时间</div>
                <div className="text-sm font-medium text-gray-700">
                  {formatOrderTime(selectedOrder.created_at)}
                </div>
              </div>
            </div>
          </div>

          {/* Item Info */}
          <div className="space-y-4">
            <h4 className="text-lg font-bold text-gray-800">商品信息</h4>
            <div className="flex items-center gap-4 rounded-md border border-gray-200 bg-gray-50 p-4">
              {selectedOrder.item_image && (
                <img
                  src={selectedOrder.item_image}
                  alt=""
                  className="h-20 w-20 rounded-md border border-gray-200 object-cover"
                />
              )}
              <div className="flex-1">
                <div className="font-bold text-gray-900 mb-1">
                  {itemTitle || selectedOrder.item_title || selectedOrder.item_id}
                </div>
                <div className="text-sm text-gray-500">商品ID: {selectedOrder.item_id}</div>
                {selectedOrder.item_price && (
                  <div className="text-sm text-gray-500 mt-1">
                    标价: ¥{selectedOrder.item_price}
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Buyer Info */}
          <div className="space-y-4">
            <h4 className="text-lg font-bold text-gray-800">买家信息</h4>
            <div className="space-y-3 rounded-md border border-gray-200 bg-gray-50 p-4">
              <div>
                <div className="text-xs text-gray-500 mb-1">买家ID</div>
                <div className="font-bold text-gray-900">{selectedOrder.buyer_id}</div>
              </div>
              <div>
                <div className="mb-1 text-xs text-gray-500">买家昵称</div>
                <div className="break-words font-medium text-gray-700">
                  {selectedOrder.buyer_nick || '未获取'}
                </div>
              </div>
              {
                <div>
                  <div className="text-xs text-gray-500 mb-1">收货人</div>
                  <div className="break-words font-medium text-gray-700">
                    {selectedOrder.receiver_name || '未获取'}
                  </div>
                </div>
              }
              {
                <div>
                  <div className="text-xs text-gray-500 mb-1">联系电话</div>
                  <div className="break-words font-mono text-sm text-gray-700">
                    {selectedOrder.receiver_phone || '未获取'}
                  </div>
                </div>
              }
              {
                <div>
                  <div className="text-xs text-gray-500 mb-1">收货地址</div>
                  <div className="whitespace-pre-wrap break-words text-sm text-gray-700">
                    {selectedOrder.receiver_address || '未获取'}
                  </div>
                </div>
              }
            </div>
          </div>
        </div>

        <div className="modal-footer">
          <div className="flex w-full gap-2">
            <button
              type="button"
              onClick={onClose}
              className="ios-btn-secondary flex-1 rounded-md px-4 py-2.5 text-sm"
            >
              关闭
            </button>
            {selectedOrder.status === 'pending_ship' && onShip && (
              <button
                type="button"
                onClick={() => {
                  onClose();
                  onShip?.(selectedOrder.order_id);
                }}
                className="ios-btn-primary flex-1 rounded-md px-4 py-2.5 text-sm"
              >
                立即发货
              </button>
            )}
          </div>
        </div>
      </div>
    </div>,
    document.body
  );
}

export function ShipOrderDialog({
  shipLoading,
  shipDisabled = false,
  shipResult,
  onClose,
  onExecute,
}: {
  shipLoading: boolean;
  shipDisabled?: boolean;
  shipResult: { success: boolean; message: string } | null;
  onClose: () => void;
  onExecute: (mode: 'status_only' | 'full_delivery') => void;
}) {
  const modal = useRef<HTMLDivElement>(null);
  useModalFocus(true, onClose, modal);
  return createPortal(
    <div
      ref={modal}
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="订单操作"
    >
      <div className="modal-container" style={{ maxWidth: '480px' }}>
        <div className="modal-header">
          <div className="flex w-full items-start justify-between gap-4">
            <div>
              <h3 className="text-lg font-bold text-gray-900">立即发货</h3>
              <p className="mt-1 text-xs text-gray-500">选择只更新平台状态，或执行完整卡密发货。</p>
            </div>
            <button
              type="button"
              onClick={onClose}
              className="rounded-md p-2 hover:bg-gray-100"
              aria-label="关闭发货操作"
            >
              <X className="w-5 h-5 text-gray-600" />
            </button>
          </div>
        </div>

        <div className="modal-body space-y-4">
          <p className="text-sm text-gray-600">请选择发货方式：</p>

          {/* 选项A: 仅修改发货状态 */}
          <button
            type="button"
            onClick={() => onExecute('status_only')}
            disabled={shipLoading || shipDisabled}
            className="w-full rounded-md border border-gray-200 p-4 text-left transition-colors hover:border-gray-400 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <div className="flex items-start gap-3">
              <div className="mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-blue-100">
                <Truck className="w-5 h-5 text-blue-600" />
              </div>
              <div>
                <div className="font-bold text-gray-900 text-sm">仅修改闲鱼发货状态</div>
                <div className="text-xs text-gray-500 mt-1 leading-relaxed">
                  不实际扣除或发送卡券，仅在闲鱼平台将订单标记为"已发货"。
                  适用于已经给客户发过货、只是忘记在闲鱼修改状态的情况。
                </div>
              </div>
            </div>
          </button>

          {/* 选项B: 完整发货流程 */}
          <button
            type="button"
            onClick={() => onExecute('full_delivery')}
            disabled={shipLoading || shipDisabled}
            className="w-full rounded-md border border-gray-200 p-4 text-left transition-colors hover:border-amber-400 hover:bg-yellow-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <div className="flex items-start gap-3">
              <div className="mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-yellow-100">
                <PackageCheck className="w-5 h-5 text-yellow-700" />
              </div>
              <div>
                <div className="font-bold text-gray-900 text-sm">完整发货（匹配卡券并发送）</div>
                <div className="text-xs text-gray-500 mt-1 leading-relaxed">
                  自动匹配发货规则、获取卡券、发送卡券信息给买家，并修改发货状态。
                  适用于订单既没有发送卡券给买家、也没有修改发货状态的情况。
                </div>
              </div>
            </div>
          </button>

          {/* 加载状态 */}
          {shipLoading && (
            <div className="flex items-center justify-center gap-2 py-3">
              <RefreshCw className="w-4 h-4 animate-spin text-gray-500" />
              <span className="text-sm text-gray-500">正在处理中...</span>
            </div>
          )}

          {/* 结果显示 */}
          {shipResult && (
            <div
              className={`rounded-md border p-3 text-sm ${shipResult.success ? 'border-green-200 bg-green-50 text-green-800' : 'border-red-200 bg-red-50 text-red-800'}`}
            >
              {shipResult.message}
            </div>
          )}
        </div>

        <div className="modal-footer">
          <button
            type="button"
            onClick={onClose}
            className="ios-btn-secondary w-full rounded-md px-4 py-2.5 text-sm"
          >
            {shipResult?.success ? '完成' : '取消'}
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
}

export function RateOrderDialog({
  rateOrder,
  rateFeedback,
  setRateFeedback,
  rateValue,
  setRateValue,
  rateAnonymous,
  setRateAnonymous,
  rateSubmitting,
  onClose,
  handleSubmitRate,
}: {
  rateOrder: Order;
  rateFeedback: string;
  setRateFeedback: (v: string) => void;
  rateValue: 1 | 0 | -1;
  setRateValue: (v: 1 | 0 | -1) => void;
  rateAnonymous: boolean;
  setRateAnonymous: (v: boolean) => void;
  rateSubmitting: boolean;
  onClose: () => void;
  handleSubmitRate: () => void;
}) {
  const modal = useRef<HTMLDivElement>(null);
  useModalFocus(
    true,
    () => {
      if (!rateSubmitting) onClose();
    },
    modal
  );
  return createPortal(
    <div
      ref={modal}
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="给买家评价"
    >
      <div className="modal-container">
        <div className="modal-header flex items-start justify-between gap-4">
          <div>
            <h3 className="text-lg font-bold text-gray-900">给买家评价</h3>
            <p className="mt-1 text-sm text-gray-500">订单 {rateOrder.order_id}</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-2 hover:bg-gray-100"
            aria-label="关闭评价"
          >
            <X className="h-5 w-5" />
          </button>
        </div>
        <div className="modal-body space-y-4">
          <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
            评价提交后无法撤销，请确认内容后再提交。
          </div>

          <div>
            <label className="mb-1 block text-sm font-bold text-gray-700">评价类型</label>
            <div className="grid grid-cols-3 gap-2">
              {(
                [
                  [1, '好评'],
                  [0, '中评'],
                  [-1, '差评'],
                ] as Array<[1 | 0 | -1, string]>
              ).map(([value, label]) => (
                <button
                  key={label}
                  type="button"
                  onClick={() => setRateValue(value)}
                  className={`rounded-md border px-3 py-2 text-sm font-bold transition-colors ${
                    rateValue === value
                      ? 'border-[#ffe100] bg-[#ffe100] text-black'
                      : 'border-gray-200 bg-white text-gray-600 hover:bg-gray-50'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          <div>
            <label className="mb-1 block text-sm font-bold text-gray-700">评价内容</label>
            <textarea
              value={rateFeedback}
              onChange={(e) => setRateFeedback(e.target.value)}
              rows={3}
              maxLength={200}
              placeholder="例如：交易顺利，感谢支持！"
              className="w-full rounded-md border border-gray-200 px-3 py-2 text-sm focus:border-[#ffe100] focus:outline-none"
            />
            <p className="mt-1 text-right text-xs text-gray-400">{rateFeedback.length}/200</p>
          </div>

          <label className="flex items-center gap-2 text-sm text-gray-700">
            <input
              type="checkbox"
              checked={rateAnonymous}
              onChange={(e) => setRateAnonymous(e.target.checked)}
              className="h-4 w-4"
            />
            匿名评价
          </label>
        </div>
        <div className="modal-footer flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="ios-btn-secondary rounded-md px-4 py-2 text-sm"
          >
            取消
          </button>
          <button
            type="button"
            onClick={handleSubmitRate}
            disabled={rateSubmitting || !rateFeedback.trim() || rateFeedback.length > 200}
            className="ios-btn-primary rounded-md px-4 py-2 text-sm disabled:cursor-not-allowed disabled:opacity-50"
          >
            {rateSubmitting ? '提交中…' : '确认提交'}
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
}
