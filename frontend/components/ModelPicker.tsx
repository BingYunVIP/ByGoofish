import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Check, Loader2, RefreshCw, X } from 'lucide-react';

import { describeApiError } from '../lib/errorMessage';
import { normalizeApiBaseUrl } from '../lib/aiEndpoint';
import { fetchAIModels } from '../services/api';

export type ModelPickerScope = { kind: 'account'; cookieId: string } | { kind: 'system' };

interface ModelPickerProps {
  id: string;
  value: string;
  onChange: (value: string) => void;
  /** 表单里当前的接口地址、密钥：读取模型列表时直接使用，无需先保存 */
  baseUrl: string;
  apiKey: string;
  userAgent?: string;
  scope: ModelPickerScope;
  /** 读取成功后回传规范化后的接口地址（自动补 /v1），让输入框同步显示 */
  onBaseUrlResolved?: (url: string) => void;
  placeholder?: string;
}

type Status = { tone: 'info' | 'error'; text: string } | null;

/** 模型名输入框 + 右侧「获取模型」按钮：读取中转站可用的模型，点选即可填入，也可以手动输入。 */
const ModelPicker: React.FC<ModelPickerProps> = ({
  id, value, onChange, baseUrl, apiKey, userAgent, scope, onBaseUrlResolved, placeholder,
}) => {
  const [loading, setLoading] = useState(false);
  const [models, setModels] = useState<string[]>([]);
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState('');
  const [status, setStatus] = useState<Status>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const filterRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const requestSeq = useRef(0);

  // 切换账号或范围后，上一份列表不再适用
  const scopeKey = scope.kind === 'account' ? `account:${scope.cookieId}` : 'system';
  useEffect(() => {
    requestSeq.current += 1;
    setModels([]);
    setOpen(false);
    setStatus(null);
    setLoading(false);
  }, [scopeKey]);

  useEffect(() => {
    if (open) filterRef.current?.focus();
  }, [open]);

  const visible = useMemo(() => {
    const keyword = filter.trim().toLowerCase();
    return keyword ? models.filter(name => name.toLowerCase().includes(keyword)) : models;
  }, [models, filter]);

  const load = async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setStatus(null);
    try {
      const result = await fetchAIModels({
        base_url: normalizeApiBaseUrl(baseUrl),
        api_key: apiKey,
        user_agent: userAgent || undefined,
        scope: scope.kind,
        cookie_id: scope.kind === 'account' ? scope.cookieId : undefined,
      });
      if (seq !== requestSeq.current) return;
      onBaseUrlResolved?.(result.base_url);
      setModels(result.models);
      setFilter('');
      if (result.models.length === 0) {
        setOpen(false);
        setStatus({ tone: 'error', text: '接口没有返回任何模型，请确认密钥有可用的模型权限' });
        return;
      }
      const missing = value.trim() && !result.models.includes(value.trim());
      setStatus({
        tone: 'info',
        text: `已获取 ${result.models.length} 个模型，点选下方名称即可使用${missing ? `（当前填写的“${value.trim()}”不在列表中）` : ''}`,
      });
      setOpen(true);
    } catch (error) {
      if (seq !== requestSeq.current) return;
      setOpen(false);
      setStatus({ tone: 'error', text: describeApiError(error, '获取模型失败，请稍后重试') });
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  };

  const select = (name: string) => {
    onChange(name);
    setOpen(false);
    inputRef.current?.focus();
  };

  const close = () => {
    setOpen(false);
    inputRef.current?.focus();
  };

  const options = (): HTMLButtonElement[] => (listRef.current
    ? Array.from(listRef.current.querySelectorAll<HTMLButtonElement>('[role="option"]'))
    : []);

  const onFilterKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    if (event.key === 'ArrowDown') { event.preventDefault(); options()[0]?.focus(); }
  };

  const onOptionKeyDown = (event: React.KeyboardEvent) => {
    const items = options();
    const index = items.indexOf(event.target as HTMLButtonElement);
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    if (event.key === 'ArrowDown') { event.preventDefault(); items[Math.min(index + 1, items.length - 1)]?.focus(); }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      if (index <= 0) filterRef.current?.focus(); else items[index - 1]?.focus();
    }
  };

  return (
    <div>
      <div className="flex gap-2">
        <input
          ref={inputRef}
          id={id}
          type="text"
          value={value}
          onChange={event => onChange(event.target.value)}
          placeholder={placeholder}
          autoComplete="off"
          spellCheck={false}
          className="ios-input min-w-0 flex-1 rounded-md px-3 py-2.5 text-sm"
        />
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          className="ios-btn-secondary flex shrink-0 items-center gap-2 whitespace-nowrap rounded-md px-3 py-2.5 text-sm disabled:opacity-60"
        >
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          {loading ? '获取中' : '获取模型'}
        </button>
      </div>

      {status && (
        <p
          role={status.tone === 'error' ? 'alert' : 'status'}
          className={`mt-1.5 text-xs leading-5 ${status.tone === 'error' ? 'text-red-600' : 'text-gray-500'}`}
        >
          {status.text}
        </p>
      )}

      {open && (
        <div className="mt-2 overflow-hidden rounded-md border border-gray-200 bg-white shadow-sm">
          <div className="flex items-center gap-2 border-b border-gray-100 p-2">
            <input
              ref={filterRef}
              type="text"
              value={filter}
              onChange={event => setFilter(event.target.value)}
              onKeyDown={onFilterKeyDown}
              placeholder={`筛选 ${models.length} 个模型`}
              aria-label="筛选模型"
              autoComplete="off"
              className="ios-input min-w-0 flex-1 rounded-md px-3 py-1.5 text-sm"
            />
            <button
              type="button"
              onClick={close}
              aria-label="收起模型列表"
              title="收起"
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-gray-500 hover:bg-gray-100"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
          <div ref={listRef} role="listbox" aria-label="可用模型" className="max-h-56 overflow-y-auto py-1">
            {visible.length === 0 ? (
              <p className="px-3 py-3 text-center text-xs text-gray-500">没有匹配的模型</p>
            ) : visible.map(name => (
              <button
                key={name}
                type="button"
                role="option"
                aria-selected={name === value.trim()}
                onClick={() => select(name)}
                onKeyDown={onOptionKeyDown}
                className="flex min-h-9 w-full items-center justify-between gap-3 px-3 py-1.5 text-left font-mono text-sm text-gray-800 hover:bg-gray-50 focus:bg-gray-100 focus:outline-none"
              >
                <span className="truncate">{name}</span>
                {name === value.trim() && <Check className="h-4 w-4 shrink-0 text-emerald-600" />}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

export default ModelPicker;
