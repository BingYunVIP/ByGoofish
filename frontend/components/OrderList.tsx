import {shipOrderResult} from '../lib/chatWorkspace.mjs';
import { StatusBadge, OrderDetailsDialog, ShipOrderDialog, RateOrderDialog } from './OrderDialogs';
import DeliveryReview from './DeliveryReview';
import { formatOrderTime } from '../lib/orderTime';
import DateRangeFilter, { defaultDateRange, dateRangeBounds } from './DateRangeFilter';
import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Order, OrderStatus, Item, AccountDetail } from '../types';
import { getOrders, syncOrders, syncSingleOrder, manualShipOrder, updateOrder, deleteOrder, importOrders, getItems, syncSoldOrders, getAccountDetails, requireOrderFlower, rateOrders, getSellerFeatureFlags } from '../services/api';
import { confirmAction, notify } from '../services/feedback';
import { Search, RefreshCw, ChevronLeft, ChevronRight, PackageCheck, Edit, Eye, Plus, Save, X, ExternalLink, Trash2, ClipboardList, Flower2, Star } from 'lucide-react';
import { EmptyState, PageHeader, PageTabs } from './ui';
import { accountDisplayName } from '../lib/accountDisplay';

const OrderList: React.FC = () => {
  const [orders, setOrders] = useState<Order[]>([]);
  const [allOrders, setAllOrders] = useState<Order[]>([]); // 保存所有订单用于搜索
  const [items, setItems] = useState<Item[]>([]);
  const [itemNames, setItemNames] = useState<Record<string, string>>({});
  const [filter, setFilter] = useState('all');
  const [dateRange, setDateRange] = useState(defaultDateRange);
  const bounds = dateRangeBounds(dateRange);
  const startTime = bounds?.startTime;
  const endTime = bounds?.endTime;
  const [searchText, setSearchText] = useState(''); // 搜索文本
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  // 每页条数可调，订单量大时避免频繁翻页
  const [pageSize, setPageSize] = useState(20);
  const [loading, setLoading] = useState(false);
  const orderRequestRef = useRef(0);
  const [syncSoldLoading, setSyncSoldLoading] = useState(false);
  // 各状态的全量条数，由后端统计，避免只按当前页计算
  const [statusCounts, setStatusCounts] = useState<Record<string, number>>({});
  // 账号维度：多账号时需要区分订单归属
  const [accounts, setAccounts] = useState<AccountDetail[]>([]);
  const [accountFilter, setAccountFilter] = useState('');
  const [showDetailModal, setShowDetailModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showImportModal, setShowImportModal] = useState(false);
  const [selectedOrder, setSelectedOrder] = useState<Order | null>(null);
  const [editingOrder, setEditingOrder] = useState<Partial<Order> | null>(null);
  const [importText, setImportText] = useState('');
  const [showShipModal, setShowShipModal] = useState(false);
  const [shipOrderId, setShipOrderId] = useState<string>('');
  const [shipLoading, setShipLoading] = useState(false);
  const [shipResult, setShipResult] = useState<{success: boolean; message: string} | null>(null);
  const [syncingOrderId, setSyncingOrderId] = useState<string | null>(null);
  const [deletingOrderId, setDeletingOrderId] = useState<string | null>(null);
  // 买家互动：这两项对买家有不可撤销的实际动作，按设置页开关决定是否展示入口
  const [sellerFeatures, setSellerFeatures] = useState<{
    auto_rate_enabled: boolean;
    auto_flower_enabled: boolean;
    auto_rate_template?: string;
  }>({
    auto_rate_enabled: false,
    auto_flower_enabled: false,
  });
  const [interactingOrderId, setInteractingOrderId] = useState<string | null>(null);
  const [rateOrder, setRateOrder] = useState<Order | null>(null);
  const [rateFeedback, setRateFeedback] = useState('');
  const [rateValue, setRateValue] = useState<1 | 0 | -1>(1);
  const [rateAnonymous, setRateAnonymous] = useState(false);
  const [rateSubmitting, setRateSubmitting] = useState(false);

  // 搜索过滤订单
  const filterOrders = (ordersToFilter: Order[]): Order[] => {
    if (!searchText.trim()) {
      return ordersToFilter;
    }

    const searchLower = searchText.toLowerCase().trim();
    return ordersToFilter.filter(order =>
      order.order_id?.toLowerCase().includes(searchLower) ||
      order.item_id?.toLowerCase().includes(searchLower) ||
      order.buyer_id?.toLowerCase().includes(searchLower) ||
      order.buyer_nick?.toLowerCase().includes(searchLower) ||
      order.item_title?.toLowerCase().includes(searchLower) ||
      order.receiver_name?.toLowerCase().includes(searchLower) ||
      order.receiver_phone?.toLowerCase().includes(searchLower) ||
      order.receiver_address?.toLowerCase().includes(searchLower)
    );
  };

  const loadOrders = async (options?: { silent?: boolean }) => {
      const requestId = ++orderRequestRef.current;
      if (!startTime || !endTime) {
        setLoading(false);
        setAllOrders([]);
        setOrders([]);
        setStatusCounts({});
        setTotalPages(1);
        return;
      }
      // 轮询刷新走静默模式，避免每 20 秒把整个列表闪成加载态
      if (!options?.silent) setLoading(true);

      try {
          // 如果有搜索文本，加载所有页的数据；否则只加载当前页
          if (searchText.trim()) {
              // 搜索模式：循环加载所有页
              let allOrdersData: Order[] = [];
              let currentPage = 1;
              let hasMore = true;

              while (hasMore) {
                  const res = await getOrders(accountFilter || undefined, filter, currentPage, 100, { startTime, endTime });
                  if (requestId !== orderRequestRef.current) return;
                  if (res.status_counts) setStatusCounts(res.status_counts);
                  allOrdersData = [...allOrdersData, ...res.data];
                  hasMore = currentPage < res.total_pages;
                  currentPage++;
              }

              setAllOrders(allOrdersData);
              setOrders(filterOrders(allOrdersData));
              setTotalPages(1); // 搜索时不分页
          } else {
              // 普通模式：只加载当前页
              const res = await getOrders(accountFilter || undefined, filter, page, pageSize, { startTime, endTime });
              if (requestId !== orderRequestRef.current) return;
              setAllOrders(res.data);
              setOrders(filterOrders(res.data));
              setTotalPages(res.total_pages);
              // 标签计数取后端的全量统计，当前页数据不足以代表全部订单
              if (res.status_counts) setStatusCounts(res.status_counts);
          }
      } catch (e) {
          console.error('加载订单失败:', e);
      } finally {
          if (requestId === orderRequestRef.current) setLoading(false);
      }
  };

  // 当订单数据改变时，重新过滤订单
  useEffect(() => {
    setOrders(filterOrders(allOrders));
  }, [allOrders, searchText]);

  // 从订单的 item_id 查找对应的商品名称（通过标题匹配）
  const getItemNameById = (orderId: string, orderItemTitle?: string): string => {
      // 如果订单有 item_title，优先使用
      if (orderItemTitle && orderItemTitle.trim()) {
          return orderItemTitle;
      }

      // 尝试通过 item_id 直接匹配
      if (itemNames[orderId]) {
          return itemNames[orderId];
      }

      // 尝试在商品列表中查找相似标题的商品
      const matchingItem = items.find(item => {
          // 如果订单有标题，尝试匹配商品标题
          if (orderItemTitle && item.item_title) {
              // 检查是否包含关键词
              const orderTitleLower = orderItemTitle.toLowerCase();
              const itemTitleLower = item.item_title.toLowerCase();
              return itemTitleLower.includes(orderTitleLower) || orderTitleLower.includes(itemTitleLower);
          }
          return false;
      });

      if (matchingItem?.item_title) {
          return matchingItem.item_title;
      }

      return '未知商品';
  };

  // 从商品列表构建商品ID到商品名的映射
  const buildItemNamesMap = (sourceItems: Item[]) => {
      const namesMap: Record<string, string> = {};
      sourceItems.forEach(item => {
          // 使用 item_id 作为键，商品标题作为值
          if (item.item_id) {
              namesMap[item.item_id] = item.item_title || item.item_id;
          }
      });
      setItemNames(namesMap);
  };

  // 账号列表独立同步：App 用 hidden 切换页面，组件挂载后不卸载，
  // 不定时拉取的话新增账号不会出现在筛选下拉里，只能刷新浏览器才看得到。
  // 单独成一个 effect 是因为下面那个 effect 依赖筛选条件会反复重建，
  // 把定时器放进去会累积泄漏。
  useEffect(() => {
    const syncAccounts = () => {
      getAccountDetails()
        .then(setAccounts)
        .catch(() => setAccounts([]));
    };
    syncAccounts();
    const timer = setInterval(syncAccounts, 30000);
    return () => clearInterval(timer);
  }, []);

  // 买家互动开关：在设置页改动后需要反映到订单页，随账号同步一起刷新
  useEffect(() => {
    const syncFlags = () => {
      getSellerFeatureFlags()
        .then(setSellerFeatures)
        .catch(() => undefined);
    };
    syncFlags();
    const timer = setInterval(syncFlags, 30000);
    return () => clearInterval(timer);
  }, []);

  // 向买家索要小红花：会真实发出一条消息，需二次确认
  const handleRequireFlower = async (order: Order) => {
    const ok = await confirmAction(
      `将向订单 ${order.order_id} 的买家发送一条求花消息，确定继续吗？`
    );
    if (!ok) return;

    setInteractingOrderId(order.order_id);
    try {
      const res = await requireOrderFlower(order.order_id);
      notify(res?.message || '求花消息已发送', res?.success === false ? 'error' : 'success');
    } catch (error) {
      notify(error instanceof Error ? error.message : '索要小红花失败', 'error');
    } finally {
      setInteractingOrderId(null);
    }
  };

  // 提交评价：不可撤销，走弹窗让用户确认内容
  const handleSubmitRate = async () => {
    if (!rateOrder) return;
    if (!rateFeedback.trim()) {
      notify('请填写评价内容', 'error');
      return;
    }

    setRateSubmitting(true);
    try {
      const res = await rateOrders([rateOrder.order_id], rateFeedback.trim(), rateValue, rateAnonymous);
      if (res?.success === false) {
        notify(res?.message || '评价提交失败', 'error');
      } else {
        notify(res?.message || '评价已提交');
        setRateOrder(null);
        setRateFeedback('');
      }
    } catch (error) {
      notify(error instanceof Error ? error.message : '评价提交失败', 'error');
    } finally {
      setRateSubmitting(false);
    }
  };

  // 订单状态会被后端持续改写（拉取详情补齐商品名/规格、发货后转状态），
  // 只在筛选变化时加载会让列表停在「未知商品 / unknown_user」这类刚建单时的快照。
  useEffect(() => {
    const timer = setInterval(() => {
      loadOrders({ silent: true });
    }, 20000);
    return () => clearInterval(timer);
  }, [filter, page, pageSize, searchText, accountFilter, startTime, endTime]);

  useEffect(() => {
    loadOrders();

    // 加载商品列表
    getItems().then((itemsList) => {
      setItems(itemsList);
      buildItemNamesMap(itemsList);
    }).catch((e) => {
      console.error('加载商品列表失败:', e);
    });
  }, [filter, page, pageSize, searchText, accountFilter, startTime, endTime]);

  const handleSync = async () => {
      setLoading(true);
      await syncOrders();
      loadOrders();
  };

  // 从卖家端接口拉取真实成交数据，可补齐监听离线期间产生的订单
  const handleSyncSold = async () => {
      setSyncSoldLoading(true);
      try {
          const res = await syncSoldOrders(undefined, 30);
          notify(res?.message || '拉取完成');
          loadOrders();
      } catch (error) {
          console.error('拉取卖出订单失败:', error);
          notify('拉取卖出订单失败，请检查账号 Cookie 是否有效');
      } finally {
          setSyncSoldLoading(false);
      }
  };

  const handleShip = (id: string) => {
      setShipOrderId(id);
      setShipResult(null);
      setShowShipModal(true);
  };

  const executeShip = async (mode: 'status_only' | 'full_delivery') => {
      setShipLoading(true);
      setShipResult(null);
      try {
          const res = await manualShipOrder([shipOrderId], mode);
          const result = shipOrderResult(res);
          if (result?.success) {
              setShipResult({ success: true, message: result.message });
              loadOrders();
          } else {
              setShipResult({ success: false, message: result?.message || '发货失败' });
          }
      } catch (e: any) {
          setShipResult({ success: false, message: e?.message || '请求失败' });
      } finally {
          setShipLoading(false);
      }
  };

  const handleViewDetail = (order: Order) => {
    setSelectedOrder(order);
    setShowDetailModal(true);
  };

  const handleEdit = (order: Order) => {
    setEditingOrder({ ...order });
    setShowEditModal(true);
  };

  const handleSaveEdit = async () => {
    if (!editingOrder || !editingOrder.order_id) return;
    try {
      // 映射前端字段到后端期望的字段名
      const updateData: Record<string, any> = {};

      if (editingOrder.status !== undefined) {
        updateData.order_status = editingOrder.status;
      }
      if (editingOrder.buyer_id !== undefined) {
        updateData.buyer_id = editingOrder.buyer_id;
      }
      if (editingOrder.buyer_nick !== undefined) {
        updateData.buyer_nick = editingOrder.buyer_nick;
      }
      if (editingOrder.amount !== undefined) {
        updateData.amount = editingOrder.amount;
      }
      if (editingOrder.receiver_name !== undefined) {
        updateData.receiver_name = editingOrder.receiver_name;
      }
      if (editingOrder.receiver_phone !== undefined) {
        updateData.receiver_phone = editingOrder.receiver_phone;
      }
      if (editingOrder.receiver_address !== undefined) {
        updateData.receiver_address = editingOrder.receiver_address;
      }
      if (editingOrder.item_id !== undefined) {
        updateData.item_id = editingOrder.item_id;
      }
      if (editingOrder.quantity !== undefined) {
        updateData.quantity = editingOrder.quantity;
      }

      await updateOrder(editingOrder.order_id, updateData);
      setShowEditModal(false);
      setEditingOrder(null);
      loadOrders();
    } catch (error) {
      console.error('更新订单失败:', error);
      notify('更新失败，请重试');
    }
  };

  const handleImportOrders = async () => {
    try {
      const orders = JSON.parse(importText);
      await importOrders(Array.isArray(orders) ? orders : [orders]);
      setShowImportModal(false);
      setImportText('');
      loadOrders();
      notify('订单导入成功');
    } catch (error) {
      notify('导入失败，请检查JSON格式');
    }
  };

  const handleSyncSingle = async (orderId: string) => {
    setSyncingOrderId(orderId);
    try {
      const result = await syncSingleOrder(orderId);
      if (result.success) {
        await loadOrders();
      } else {
        notify(result.message || '同步失败');
      }
    } catch (error: any) {
      console.error('同步订单失败:', error);
      notify(error?.message || '同步失败，请重试');
    } finally {
      setSyncingOrderId(null);
    }
  };

  const handleDelete = async (orderId: string) => {
    if (!await confirmAction('确认删除该订单吗？删除后无法恢复。')) return;
    setDeletingOrderId(orderId);
    try {
      await deleteOrder(orderId);
      setAllOrders(prev => prev.filter(o => o.order_id !== orderId));
    } catch (error: any) {
      console.error('删除订单失败:', error);
      notify(error?.message || '删除失败，请重试');
      await loadOrders();
    } finally {
      setDeletingOrderId(null);
    }
  };

  const accountMap = React.useMemo(
    () => new Map(accounts.map(a => [a.id, a])),
    [accounts],
  );

  const renderAccountCell = (cookieId?: string) => {
    const account = cookieId ? accountMap.get(cookieId) : undefined;
    const name = account ? accountDisplayName(account, cookieId || '未知账号') : (cookieId || '未知账号');
    return (
      <div className="flex items-center gap-2">
        {account?.avatar_url ? (
          <img
            src={account.avatar_url}
            alt=""
            className="h-6 w-6 flex-shrink-0 rounded-full object-cover"
            referrerPolicy="no-referrer"
          />
        ) : (
          <span className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full bg-gray-200 text-[10px] text-gray-500">
            {name.slice(0, 1)}
          </span>
        )}
        <span className="truncate text-xs text-gray-700" title={name}>{name}</span>
      </div>
    );
  };

  // 计数一律取后端的全量统计；搜索模式下已加载全部数据，可直接本地统计
  const countOf = (status: OrderStatus) =>
    searchText.trim()
      ? allOrders.filter(order => order.status === status).length
      : (statusCounts[status] ?? 0);

  const totalCount = searchText.trim() ? allOrders.length : (statusCounts.all ?? 0);
  const pendingCount = countOf('pending_ship');
  const shippedCount = countOf('shipped');
  const cancelledCount = countOf('cancelled');
  const refundingCount = countOf('refunding');
  const completedCount = countOf('completed');
  const exceptionCount = cancelledCount + refundingCount;

  return (
    <div className="page-stack animate-fade-in">
      <DeliveryReview />
      <PageHeader
        title="订单管理"
        description="同步闲鱼交易状态，核对买家与商品信息，并处理人工或完整自动发货。"
        icon={ClipboardList}
        actions={(
          <>
            <button
              onClick={() => setShowImportModal(true)}
              className="ios-btn-secondary flex items-center justify-center gap-2 rounded-md px-4 py-2.5 text-sm"
            >
              <Plus className="w-4 h-4" />
              插入订单
            </button>
            <button onClick={handleSync} className="ios-btn-secondary flex items-center justify-center gap-2 rounded-md px-4 py-2.5 text-sm">
                <RefreshCw className="h-4 w-4" />
                刷新状态
            </button>
            <button
              onClick={handleSyncSold}
              disabled={syncSoldLoading}
              className="ios-btn-primary flex items-center justify-center gap-2 rounded-md px-4 py-2.5 text-sm disabled:opacity-60"
            >
                <RefreshCw className={`h-4 w-4 ${syncSoldLoading ? 'animate-spin' : ''}`} />
                拉取卖出订单
            </button>
          </>
        )}
      />

      <div className="toolbar">
        <DateRangeFilter value={dateRange} onChange={value => { setDateRange(value); setPage(1); }} label="订单日期范围" />
      </div>

      <div className="metric-grid metric-grid--compact">
        <div className="metric-card">
          <p className="metric-card__label">订单总数</p>
          <p className="metric-card__value">{totalCount}</p>
          <p className="metric-card__meta">所选日期范围内</p>
        </div>
        <div className="metric-card">
          <p className="metric-card__label">待发货</p>
          <p className="metric-card__value">{pendingCount}</p>
          <p className="metric-card__meta">需要优先处理</p>
        </div>
        <div className="metric-card">
          <p className="metric-card__label">已发货</p>
          <p className="metric-card__value">{shippedCount}</p>
          <p className="metric-card__meta">等待确认或完成</p>
        </div>
        <div className="metric-card">
          <p className="metric-card__label">交易成功</p>
          <p className="metric-card__value">{completedCount}</p>
          <p className="metric-card__meta">已完成交易</p>
        </div>
        <div className="metric-card">
          <p className="metric-card__label">异常订单</p>
          <p className="metric-card__value">{exceptionCount}</p>
          <p className="metric-card__meta">取消 {cancelledCount} / 退款 {refundingCount}</p>
        </div>
      </div>

      <section className="section-panel">
        {/* Toolbar */}
        <div className="toolbar rounded-none border-0 border-b shadow-none">
          {/* 账号筛选放在最左：它是比状态更上层的过滤维度，
              先选看哪个账号，再看该账号下的订单状态，阅读顺序才顺。 */}
          {accounts.length > 1 && (
            <select
              value={accountFilter}
              onChange={(e) => { setAccountFilter(e.target.value); setPage(1); }}
              className="ios-input w-full rounded-md bg-white py-2.5 px-3 md:w-44"
              aria-label="按账号筛选订单"
            >
              <option value="">全部账号</option>
              {accounts.map(account => (
                <option key={account.id} value={account.id}>
                  {accountDisplayName(account)}
                </option>
              ))}
            </select>
          )}
          <PageTabs
            value={filter}
            onChange={(value) => { setFilter(value); setPage(1); setSearchText(''); }}
            ariaLabel="订单状态筛选"
            items={[
              { id: 'all', label: '全部', count: totalCount },
              { id: 'pending_ship', label: '待发货', count: pendingCount },
              { id: 'shipped', label: '已发货', count: shippedCount },
              { id: 'completed', label: '交易成功', count: completedCount },
              { id: 'cancelled', label: '已取消', count: cancelledCount },
              { id: 'refunding', label: '退款中', count: refundingCount },
            ]}
          />
          <div className="flex w-full flex-col gap-2 md:w-auto md:flex-row md:items-center">
            <div className="group relative w-full md:w-auto">
               <Search className="w-4 h-4 absolute left-4 top-1/2 -translate-y-1/2 text-gray-400 group-focus-within:text-[#ffe100] transition-colors" />
               <input
                   type="text"
                   placeholder="搜索订单号/商品/买家..."
                   value={searchText}
                   onChange={(e) => { setSearchText(e.target.value); setPage(1); }}
                   className="ios-input w-full rounded-md bg-white py-2.5 pl-10 pr-4 md:w-72"
               />
            </div>
          </div>
        </div>

        {/* Table */}
        <div className="min-h-0 overflow-x-auto md:min-h-[360px]">
          <table className="data-table responsive-data-table min-w-[1040px] table-fixed">
            <thead>
              <tr>
                <th style={{width: '25%'}}>订单信息</th>
                <th style={{width: '12%'}}>所属账号</th>
                <th style={{width: '21%'}}>买家信息</th>
                <th style={{width: '9%'}}>实付金额</th>
                <th style={{width: '10%'}}>当前状态</th>
                <th className="text-right" style={{width: '23%'}}>操作</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((order) => (
                <tr key={order.id} className="group">
                  <td data-label="订单信息">
                    <div className="flex items-center gap-3">
                      <div className="h-12 w-12 flex-shrink-0 overflow-hidden rounded-md border border-gray-200 bg-gray-100">
                        {order.item_image ? (
                            <img src={order.item_image} alt="" className="w-full h-full object-cover" />
                        ) : (
                            <div className="w-full h-full flex items-center justify-center text-gray-400"><PackageCheck /></div>
                        )}
                      </div>
                      <div className="min-w-0">
                        <div className="font-bold text-gray-900 line-clamp-1 text-sm">
                          {getItemNameById(order.item_id, order.item_title)}
                        </div>
                        <div className="text-xs text-gray-500 mt-1 font-medium">订单ID: {order.order_id}</div>
                        <div className="text-xs text-gray-400 mt-0.5">数量: {order.quantity} • {formatOrderTime(order.created_at)}</div>
                      </div>
                    </div>
                  </td>
                  <td data-label="所属账号">
                    {renderAccountCell(order.cookie_id)}
                  </td>
                  <td data-label="买家信息">
                      <div className="flex min-w-0 flex-col gap-1 [overflow-wrap:anywhere]">
                          <div className="text-sm font-bold text-gray-800">买家昵称：{order.buyer_nick || '未获取'}</div>
                          <div className="font-mono text-xs text-gray-600">买家ID：{order.buyer_id || '未获取'}</div>
                          <div className="text-xs text-gray-600">收货人：{order.receiver_name || '未获取'}</div>
                          <div className="font-mono text-xs text-gray-600">联系电话：{order.receiver_phone || '未获取'}</div>
                          <div className="whitespace-pre-wrap text-xs text-gray-500">收货地址：{order.receiver_address || '未获取'}</div>
                          {order.buyer_info_private && <div className="text-xs text-gray-500">平台隐私保护，部分信息可能脱敏</div>}
                      </div>
                  </td>
                  <td data-label="实付金额" className="font-feature-settings-tnum text-sm font-extrabold text-gray-900">¥{order.amount}</td>
                  <td data-label="当前状态">
                    <StatusBadge status={order.status} />
                  </td>
                  <td data-label="操作" className="text-right">
                    <div className="flex items-center justify-end gap-1">
                    {order.status === 'pending_ship' && (
                        <button
                            onClick={() => handleShip(order.order_id)}
                            className="ios-btn-primary mr-1 rounded-md px-3 py-2 text-xs"
                        >
                            立即发货
                        </button>
                    )}
                    <a
                      href={`https://www.goofish.com/order-detail?orderId=${order.order_id}&role=seller`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex rounded-md p-2 text-gray-500 transition-colors hover:bg-amber-50 hover:text-amber-700"
                      title="查看闲鱼详情"
                    >
                      <ExternalLink className="w-4 h-4" />
                    </a>
                    {/* 买家互动入口：仅在设置中开启、且订单已进入发货后阶段时出现，
                        避免对未成交订单误操作。两者都会对买家产生不可撤销的动作。 */}
                    {sellerFeatures.auto_flower_enabled
                      && (order.status === 'shipped' || order.status === 'completed') && (
                      <button
                        onClick={() => handleRequireFlower(order)}
                        disabled={interactingOrderId === order.order_id}
                        className="rounded-md p-2 text-gray-500 transition-colors hover:bg-pink-50 hover:text-pink-600 disabled:cursor-wait disabled:opacity-50"
                        title="向买家索要小红花"
                      >
                        <Flower2 className="w-4 h-4" />
                      </button>
                    )}
                    {sellerFeatures.auto_rate_enabled
                      && (order.status === 'shipped' || order.status === 'completed') && (
                      <button
                        onClick={() => {
                          setRateOrder(order);
                          // 预填「买家互动」页配置的默认文案，仍可在弹窗里逐单改
                          setRateFeedback(sellerFeatures.auto_rate_template || '');
                          setRateValue(1);
                          setRateAnonymous(false);
                        }}
                        className="rounded-md p-2 text-gray-500 transition-colors hover:bg-yellow-50 hover:text-yellow-600"
                        title="给买家评价"
                      >
                        <Star className="w-4 h-4" />
                      </button>
                    )}
                    <button
                      onClick={() => handleViewDetail(order)}
                      className="rounded-md p-2 text-gray-500 transition-colors hover:bg-blue-50 hover:text-blue-600"
                      title="查看详情"
                    >
                      <Eye className="w-4 h-4" />
                    </button>
                    <button
                      onClick={() => handleEdit(order)}
                      className="rounded-md p-2 text-gray-500 transition-colors hover:bg-gray-100 hover:text-black"
                      title="编辑订单"
                    >
                      <Edit className="w-4 h-4" />
                    </button>
                    <button
                      onClick={() => handleSyncSingle(order.order_id)}
                      disabled={syncingOrderId === order.order_id}
                      className="rounded-md p-2 text-gray-500 transition-colors hover:bg-green-50 hover:text-green-700 disabled:opacity-50"
                      title="同步订单"
                    >
                      <RefreshCw className={`w-4 h-4 ${syncingOrderId === order.order_id ? 'animate-spin' : ''}`} />
                    </button>
                    <button
                      onClick={() => handleDelete(order.order_id)}
                      disabled={deletingOrderId === order.order_id}
                      className="rounded-md p-2 text-gray-500 transition-colors hover:bg-red-50 hover:text-red-600 disabled:opacity-50"
                      title="删除订单"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!loading && orders.length === 0 && (
            <div className="p-4">
              <EmptyState
                compact
                icon={PackageCheck}
                title="没有匹配的订单"
                description={searchText ? '请调整搜索条件，或清空搜索后查看全部订单。' : '可先同步闲鱼订单，或手动插入用于补录的订单数据。'}
              />
            </div>
          )}
        </div>

        {/* Pagination */}
        <div className="p-4 border-t border-gray-50 flex flex-wrap items-center justify-between gap-3 bg-white">
            <div className="flex items-center gap-3 pl-2">
                <span className="text-sm font-medium text-gray-500">
                    第 {page} 页 / 共 {totalPages} 页
                </span>
                <label className="flex items-center gap-1.5 text-sm text-gray-500">
                    每页
                    <select
                        value={pageSize}
                        onChange={e => { setPageSize(Number(e.target.value)); setPage(1); }}
                        className="ios-input rounded-md px-2 py-1 text-sm"
                        aria-label="每页显示数量"
                    >
                        {[20, 50, 100, 200].map(n => (
                            <option key={n} value={n}>{n}</option>
                        ))}
                    </select>
                    条
                </label>
            </div>
            <div className="flex gap-2">
                <button
                    disabled={page <= 1}
                    onClick={() => setPage(p => p - 1)}
                    className="rounded-md bg-gray-50 p-2.5 text-gray-600 transition-colors hover:bg-gray-100 disabled:cursor-not-allowed disabled:opacity-50"
                >
                    <ChevronLeft className="w-5 h-5" />
                </button>
                <button
                    disabled={page >= totalPages}
                    onClick={() => setPage(p => p + 1)}
                    className="rounded-md bg-gray-50 p-2.5 text-gray-600 transition-colors hover:bg-gray-100 disabled:cursor-not-allowed disabled:opacity-50"
                >
                    <ChevronRight className="w-5 h-5" />
                </button>
            </div>
        </div>
      </section>

      {/* 订单详情弹窗 - 使用 Portal */}
      {showDetailModal && selectedOrder && <OrderDetailsDialog selectedOrder={selectedOrder} onClose={()=>setShowDetailModal(false)} onShip={handleShip} itemTitle={getItemNameById(selectedOrder.item_id,selectedOrder.item_title)}/>}

      {/* Import Modal - 使用 Portal */}
      {showImportModal && createPortal(
        <div className="modal-overlay">
          <div className="modal-container">
            <div className="modal-header">
              <div className="flex items-center justify-between w-full">
                <div>
                  <h3 className="text-lg font-bold text-gray-900">插入订单</h3>
                  <p className="mt-1 text-xs text-gray-500">粘贴单条订单对象或订单数组，用于补录和测试。</p>
                </div>
                <button
                  onClick={() => setShowImportModal(false)}
                  className="rounded-md p-2 hover:bg-gray-100"
                  aria-label="关闭插入订单"
                >
                  <X className="w-5 h-5 text-gray-600" />
                </button>
              </div>
            </div>

            <div className="modal-body space-y-5">
              <div>
                <label className="field-label">订单 JSON</label>
                <textarea
                  value={importText}
                  onChange={(event) => setImportText(event.target.value)}
                  className="ios-input min-h-64 w-full resize-y rounded-md px-4 py-3 font-mono text-sm"
                  placeholder={'[\n  {\n    "order_id": "订单号",\n    "item_id": "商品ID",\n    "buyer_id": "买家ID",\n    "status": "pending_ship",\n    "quantity": 1,\n    "amount": 0.01\n  }\n]'}
                />
                <p className="mt-2 text-xs text-gray-500">状态可使用 processing、pending_ship、shipped、completed、cancelled 或 refunding。</p>
              </div>
            </div>

            <div className="modal-footer">
              <div className="flex w-full gap-2">
                <button
                  type="button"
                  onClick={() => setShowImportModal(false)}
                  className="ios-btn-secondary flex-1 rounded-md px-4 py-2.5 text-sm"
                >
                  取消
                </button>
                <button
                  onClick={handleImportOrders}
                  disabled={!importText.trim()}
                  className="ios-btn-primary flex-1 rounded-md px-6 py-2.5 text-sm disabled:opacity-50"
                >
                  导入订单
                </button>
              </div>
            </div>
          </div>
        </div>,
        document.body
      )}

      {/* Ship Modal - 发货方式选择 */}
      {showShipModal && <ShipOrderDialog shipLoading={shipLoading} shipResult={shipResult} onClose={()=>{setShowShipModal(false);setShipResult(null);}} onExecute={executeShip}/>}

      {/* Edit Modal - 使用 Portal */}
      {showEditModal && editingOrder && createPortal(
        <div className="modal-overlay">
          <div className="modal-container">
            <div className="modal-header">
              <div className="flex w-full items-start justify-between gap-4">
                <div>
                  <h3 className="text-lg font-bold text-gray-900">编辑订单</h3>
                  <p className="mt-1 break-all text-xs text-gray-500">{editingOrder.order_id}</p>
                </div>
                <button
                  type="button"
                  onClick={() => setShowEditModal(false)}
                  className="rounded-md p-2 hover:bg-gray-100"
                  aria-label="关闭编辑订单"
                >
                  <X className="w-5 h-5 text-gray-600" />
                </button>
              </div>
            </div>

            <div className="modal-body space-y-5">
              <div className="grid gap-4 sm:grid-cols-2">
                <div>
                  <label className="block text-sm font-bold text-gray-700 mb-2">订单号</label>
                  <input
                    type="text"
                    value={editingOrder.order_id}
                    disabled
                    className="ios-input w-full rounded-md bg-gray-50 px-3 py-2.5 text-gray-500"
                  />
                </div>
                <div>
                  <label className="block text-sm font-bold text-gray-700 mb-2">订单状态</label>
                  <select
                    value={editingOrder.status}
                    onChange={(e) => setEditingOrder({ ...editingOrder, status: e.target.value as OrderStatus })}
                    className="ios-input w-full rounded-md px-3 py-2.5"
                  >
                    <option value="processing">处理中</option>
                    <option value="pending_ship">待发货</option>
                    <option value="shipped">已发货</option>
                    <option value="completed">已完成</option>
                    <option value="cancelled">已取消</option>
                    <option value="refunding">退款中</option>
                  </select>
                </div>
              </div>

              <div className="grid gap-4 sm:grid-cols-2">
                <div>
                  <label className="block text-sm font-bold text-gray-700 mb-2">买家ID</label>
                  <input
                    type="text"
                    value={editingOrder.buyer_id}
                    onChange={(e) => setEditingOrder({ ...editingOrder, buyer_id: e.target.value })}
                    className="ios-input w-full rounded-md px-3 py-2.5"
                  />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-bold text-gray-700">买家昵称</label>
                  <input type="text" value={editingOrder.buyer_nick || ''}
                    onChange={(e) => setEditingOrder({ ...editingOrder, buyer_nick: e.target.value })}
                    className="ios-input w-full rounded-md px-3 py-2.5" />
                </div>
                <div>
                  <label className="block text-sm font-bold text-gray-700 mb-2">实付金额</label>
                  <input
                    type="number"
                    value={editingOrder.amount}
                    onChange={(e) => setEditingOrder({ ...editingOrder, amount: parseFloat(e.target.value) })}
                    className="ios-input w-full rounded-md px-3 py-2.5"
                  />
                </div>
              </div>

              <div className="grid gap-4 sm:grid-cols-2">
                <div>
                  <label className="block text-sm font-bold text-gray-700 mb-2">收货人</label>
                  <input
                    type="text"
                    value={editingOrder.receiver_name || ''}
                    onChange={(e) => setEditingOrder({ ...editingOrder, receiver_name: e.target.value })}
                    className="ios-input w-full rounded-md px-3 py-2.5"
                  />
                </div>
                <div>
                  <label className="block text-sm font-bold text-gray-700 mb-2">联系电话</label>
                  <input
                    type="text"
                    value={editingOrder.receiver_phone || ''}
                    onChange={(e) => setEditingOrder({ ...editingOrder, receiver_phone: e.target.value })}
                    className="ios-input w-full rounded-md px-3 py-2.5"
                  />
                </div>
              </div>

              <div>
                <label className="block text-sm font-bold text-gray-700 mb-2">收货地址</label>
                <textarea
                  value={editingOrder.receiver_address || ''}
                  onChange={(e) => setEditingOrder({ ...editingOrder, receiver_address: e.target.value })}
                  rows={2}
                  className="ios-input w-full resize-y rounded-md px-3 py-2.5"
                />
              </div>

              <div>
                <label className="block text-sm font-bold text-gray-700 mb-2">商品标题</label>
                <input
                  type="text"
                  value={editingOrder.item_title || ''}
                  onChange={(e) => setEditingOrder({ ...editingOrder, item_title: e.target.value })}
                  className="ios-input w-full rounded-md px-3 py-2.5"
                />
              </div>
            </div>

            <div className="modal-footer">
              <div className="flex w-full gap-2">
                <button
                  type="button"
                  onClick={() => setShowEditModal(false)}
                  className="ios-btn-secondary flex-1 rounded-md px-4 py-2.5 text-sm"
                >
                  取消
                </button>
                <button
                  type="button"
                  onClick={handleSaveEdit}
                  className="ios-btn-primary flex flex-1 items-center justify-center gap-2 rounded-md px-4 py-2.5 text-sm"
                >
                  <Save className="w-4 h-4" />
                  保存更改
                </button>
              </div>
            </div>
          </div>
        </div>,
        document.body
      )}

      {rateOrder && <RateOrderDialog rateOrder={rateOrder} rateFeedback={rateFeedback} setRateFeedback={setRateFeedback} rateValue={rateValue} setRateValue={setRateValue} rateAnonymous={rateAnonymous} setRateAnonymous={setRateAnonymous} rateSubmitting={rateSubmitting} onClose={()=>setRateOrder(null)} handleSubmitRate={handleSubmitRate}/>}
    </div>
  );
};

export default OrderList;
