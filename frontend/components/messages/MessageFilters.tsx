import { formatOrderTime as formatDateTime } from '../../lib/orderTime';
import React, { useEffect, useState } from 'react';
import { ArrowLeft, Plus, Trash2, Settings2, Loader2, RefreshCw } from 'lucide-react';
import type { MessageFilter, MessageFilterType, AccountDetail } from '../../types';
import {
  getAccountDetails,
  getMessageFilters,
  batchCreateMessageFilters,
  batchDeleteMessageFilters,
  deleteMessageFilter,
  toggleMessageFilter,
} from '../../services/api';
import { confirmAction, notify } from '../../services/feedback';
import { accountDisplayName } from '../../lib/accountDisplay';
import { EmptyState, SectionHeader } from '../ui';
const filterTypeLabel: Record<MessageFilterType, string> = {
  skip_reply: '跳过自动回复',
  skip_notify: '跳过外部通知',
};
export default function MessageFilters({ onBack }: { onBack: () => void }) {
  const [accountDetails, setAccountDetails] = useState<AccountDetail[]>([]);
  const [filters, setFilters] = useState<MessageFilter[]>([]),
    [filtersLoading, setFiltersLoading] = useState(true),
    [filterAccount, setFilterAccount] = useState(''),
    [filterType, setFilterType] = useState<MessageFilterType | ''>(''),
    [newAccount, setNewAccount] = useState(''),
    [newType, setNewType] = useState<MessageFilterType>('skip_reply'),
    [newKeywords, setNewKeywords] = useState(''),
    [selectedFilterIds, setSelectedFilterIds] = useState<number[]>([]),
    [savingFilters, setSavingFilters] = useState(false);
  const selectedAllFilters =
    filters.length > 0 && filters.every((f) => selectedFilterIds.includes(f.id));
  const loadFilters = async () => {
    setFiltersLoading(true);
    try {
      const data = await getMessageFilters({
        cookie_id: filterAccount || undefined,
        filter_type: filterType || undefined,
      });
      setFilters(data);
      setSelectedFilterIds((old) => old.filter((id) => data.some((f) => f.id === id)));
    } catch {
      notify('加载过滤规则失败', 'error');
    } finally {
      setFiltersLoading(false);
    }
  };
  useEffect(() => {
    void getAccountDetails().then((data) => {
      setAccountDetails(data);
      setNewAccount(data[0]?.id || '');
    });
  }, []);
  useEffect(() => {
    void loadFilters();
  }, [filterAccount, filterType]);
  const createFilters = async () => {
    const keywords = newKeywords
      .split(/\r?\n/)
      .map((value) => value.trim())
      .filter(Boolean);
    if (!newAccount || keywords.length === 0) {
      notify('请选择账号并填写至少一个关键词', 'warning');
      return;
    }
    setSavingFilters(true);
    try {
      const result = await batchCreateMessageFilters({
        cookie_id: newAccount,
        filter_type: newType,
        keywords,
      });
      setNewKeywords('');
      await loadFilters();
      notify(`新增 ${result.created} 条，跳过重复 ${result.skipped} 条`, 'success');
    } catch (error) {
      notify(`新增规则失败：${(error as Error).message}`, 'error');
    } finally {
      setSavingFilters(false);
    }
  };

  const removeFilter = async (filter: MessageFilter) => {
    if (!(await confirmAction(`删除过滤关键词“${filter.keyword}”？`))) return;
    try {
      await deleteMessageFilter(filter.id);
      await loadFilters();
      notify('规则已删除', 'success');
    } catch (error) {
      notify(`删除失败：${(error as Error).message}`, 'error');
    }
  };

  const removeSelectedFilters = async () => {
    if (
      selectedFilterIds.length === 0 ||
      !(await confirmAction(`删除选中的 ${selectedFilterIds.length} 条规则？`))
    )
      return;
    try {
      await batchDeleteMessageFilters(selectedFilterIds);
      setSelectedFilterIds([]);
      await loadFilters();
      notify('所选规则已删除', 'success');
    } catch (error) {
      notify(`批量删除失败：${(error as Error).message}`, 'error');
    }
  };

  const renderFilters = () => (
    <div className="h-full overflow-y-auto bg-[var(--app-bg)] p-4 sm:p-6 lg:p-8">
      <div className="page-stack mx-auto max-w-[1320px]">
        <header className="page-header">
          <div>
            <h1 className="page-title">消息过滤规则</h1>
            <p className="page-description">集中管理无需自动回复或无需外部通知的消息关键词。</p>
          </div>
          <button
            type="button"
            onClick={onBack}
            className="ios-btn-secondary flex items-center gap-2 rounded-md px-4 py-2.5 text-sm"
          >
            <ArrowLeft className="h-4 w-4" />
            返回消息
          </button>
        </header>

        <section className="section-panel">
          <SectionHeader
            title="批量添加规则"
            description="每行填写一个关键词，重复内容会自动跳过。"
            icon={Plus}
          />
          <div className="grid gap-3 p-4 lg:grid-cols-[220px_180px_minmax(240px,1fr)_auto]">
            <select
              value={newAccount}
              onChange={(event) => setNewAccount(event.target.value)}
              className="ios-input rounded-md px-3 py-2.5 text-sm"
            >
              <option value="">选择账号</option>
              {accountDetails.map((account) => (
                <option key={account.id} value={account.id}>
                  {accountDisplayName(account)}
                </option>
              ))}
            </select>
            <select
              value={newType}
              onChange={(event) => setNewType(event.target.value as MessageFilterType)}
              className="ios-input rounded-md px-3 py-2.5 text-sm"
            >
              <option value="skip_reply">跳过自动回复</option>
              <option value="skip_notify">跳过外部通知</option>
            </select>
            <textarea
              value={newKeywords}
              onChange={(event) => setNewKeywords(event.target.value)}
              rows={2}
              placeholder={'每行一个关键词，例如：\n系统通知'}
              className="ios-input min-h-20 resize-y rounded-md px-3 py-2.5 text-sm"
            />
            <button
              type="button"
              onClick={() => void createFilters()}
              disabled={savingFilters}
              className="ios-btn-primary flex min-h-11 items-center justify-center gap-2 rounded-md px-4 py-2.5 text-sm disabled:opacity-50"
            >
              {savingFilters ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Plus className="h-4 w-4" />
              )}
              批量添加
            </button>
          </div>
        </section>

        <section className="section-panel">
          <SectionHeader
            title="现有规则"
            description={`当前筛选共 ${filters.length} 条`}
            icon={Settings2}
          />
          <div className="toolbar rounded-none border-x-0 border-t-0 shadow-none">
            <div className="toolbar__group">
              <select
                value={filterAccount}
                onChange={(event) => setFilterAccount(event.target.value)}
                className="ios-input rounded-md px-3 py-2.5 text-sm sm:min-w-52"
              >
                <option value="">全部账号</option>
                {accountDetails.map((account) => (
                  <option key={account.id} value={account.id}>
                    {accountDisplayName(account)}
                  </option>
                ))}
              </select>
              <select
                value={filterType}
                onChange={(event) => setFilterType(event.target.value as MessageFilterType | '')}
                className="ios-input rounded-md px-3 py-2.5 text-sm sm:min-w-44"
              >
                <option value="">全部用途</option>
                <option value="skip_reply">跳过自动回复</option>
                <option value="skip_notify">跳过外部通知</option>
              </select>
              <button
                type="button"
                onClick={() => void loadFilters()}
                className="ios-btn-secondary flex items-center justify-center gap-2 rounded-md px-4 py-2.5 text-sm"
              >
                <RefreshCw className={`h-4 w-4 ${filtersLoading ? 'animate-spin' : ''}`} />
                查询
              </button>
            </div>
            {selectedFilterIds.length > 0 && (
              <button
                type="button"
                onClick={() => void removeSelectedFilters()}
                className="ios-btn-danger flex items-center justify-center gap-2 rounded-md px-4 py-2.5 text-sm"
              >
                <Trash2 className="h-4 w-4" />
                删除所选
              </button>
            )}
          </div>

          <div className="divide-y divide-[#eeeeee] px-4">
            {filters.length > 0 && (
              <label className="flex items-center gap-3 py-3 text-xs font-bold text-[var(--text-muted)]">
                <input
                  type="checkbox"
                  checked={selectedAllFilters}
                  onChange={(event) =>
                    setSelectedFilterIds(
                      event.target.checked ? filters.map((filter) => filter.id) : []
                    )
                  }
                  className="h-4 w-4 accent-[#f5c400]"
                />
                全选当前列表
              </label>
            )}
            {filters.map((filter) => (
              <div
                key={filter.id}
                className="grid gap-3 py-4 sm:grid-cols-[24px_52px_180px_minmax(0,1fr)_44px] sm:items-center"
              >
                <input
                  type="checkbox"
                  checked={selectedFilterIds.includes(filter.id)}
                  onChange={(event) =>
                    setSelectedFilterIds((current) =>
                      event.target.checked
                        ? [...current, filter.id]
                        : current.filter((id) => id !== filter.id)
                    )
                  }
                  className="h-4 w-4 accent-[#f5c400]"
                />
                <button
                  type="button"
                  role="switch"
                  aria-checked={filter.enabled}
                  onClick={() => void toggleMessageFilter(filter.id).then(loadFilters)}
                  title={filter.enabled ? '停用规则' : '启用规则'}
                  className={`relative h-6 w-11 rounded-full transition-colors ${
                    filter.enabled ? 'bg-[#f5c800]' : 'bg-gray-300'
                  }`}
                >
                  <span
                    className={`absolute left-1 top-1 h-4 w-4 rounded-full bg-white transition-transform ${
                      filter.enabled ? 'translate-x-5' : ''
                    }`}
                  />
                </button>
                <div>
                  <p className="break-all text-sm font-bold text-[var(--text)]">
                    {filter.cookie_id}
                  </p>
                  <p className="mt-1 text-xs text-[var(--text-muted)]">
                    {filterTypeLabel[filter.filter_type]}
                  </p>
                </div>
                <div className="min-w-0">
                  <p className="break-words text-sm font-medium text-[var(--text)]">
                    {filter.keyword}
                  </p>
                  <p className="mt-1 text-xs text-[var(--text-soft)]">
                    {formatDateTime(filter.updated_at || filter.created_at)}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => void removeFilter(filter)}
                  title="删除规则"
                  className="flex h-9 w-9 items-center justify-center rounded-md bg-red-50 text-red-600 hover:bg-red-100"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            ))}
            {!filtersLoading && filters.length === 0 && (
              <EmptyState
                compact
                title="暂无消息过滤规则"
                description="添加规则后可跳过指定消息的自动回复或外部通知。"
                icon={Settings2}
              />
            )}
          </div>
        </section>
      </div>
    </div>
  );

  return renderFilters();
}
