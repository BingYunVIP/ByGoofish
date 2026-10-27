import React, { useEffect, useState } from 'react';
import { PageTabs } from './ui';

export type DateRangeValue = { preset: string; start: string; end: string };
export const defaultDateRange: DateRangeValue = { preset: '7', start: '', end: '' };

// 按浏览器所在时区的自然日计算；结束边界为次日零点（不含）。
export const dateRangeBounds = (value: DateRangeValue, now = new Date()) => {
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const end = new Date(start);
  end.setDate(end.getDate() + 1);
  if (value.preset === 'custom') {
    if (!value.start || !value.end || value.start > value.end) return null;
    const customStart = new Date(`${value.start}T00:00:00`);
    const customEnd = new Date(`${value.end}T00:00:00`);
    customEnd.setDate(customEnd.getDate() + 1);
    return { startTime: customStart.toISOString(), endTime: customEnd.toISOString() };
  }
  if (value.preset === 'yesterday') {
    end.setTime(start.getTime());
    start.setDate(start.getDate() - 1);
  } else {
    start.setDate(start.getDate() - (Number(value.preset) - 1));
  }
  return { startTime: start.toISOString(), endTime: end.toISOString() };
};

export const timestampInRange = (timestamp: number, bounds: ReturnType<typeof dateRangeBounds>) => {
  if (!bounds || !Number.isFinite(timestamp) || timestamp <= 0) return false;
  const milliseconds = timestamp < 1_000_000_000_000 ? timestamp * 1000 : timestamp;
  return milliseconds >= Date.parse(bounds.startTime) && milliseconds < Date.parse(bounds.endTime);
};

const DateRangeFilter: React.FC<{
  value: DateRangeValue;
  onChange: (value: DateRangeValue) => void;
  label: string;
}> = ({ value, onChange, label }) => {
  const [selectedPreset, setSelectedPreset] = useState(value.preset);
  const [start, setStart] = useState(value.start);
  const [end, setEnd] = useState(value.end);
  useEffect(() => { setSelectedPreset(value.preset); }, [value.preset]);
  const valid = Boolean(start && end && start <= end);
  return <>
    <PageTabs value={selectedPreset} ariaLabel={label}
      onChange={preset => {
        setSelectedPreset(preset);
        if (preset !== 'custom') onChange({ preset, start: '', end: '' });
      }}
      items={[
        { id: 'yesterday', label: '昨天' }, { id: '1', label: '今天' },
        { id: '3', label: '3天内' }, { id: '7', label: '7天内' },
        { id: '30', label: '1个月内' }, { id: 'custom', label: '自定义' },
      ]} />
    {selectedPreset === 'custom' && <>
      <div className="toolbar__group">
        <input type="date" aria-label={`${label}开始日期`} value={start} max={end || undefined}
          onChange={event => setStart(event.target.value)}
          className="ios-input rounded-md px-3 py-2 text-sm" />
        <span className="self-center text-gray-400">-</span>
        <input type="date" aria-label={`${label}结束日期`} value={end} min={start || undefined}
          onChange={event => setEnd(event.target.value)}
          className="ios-input rounded-md px-3 py-2 text-sm" />
        <button type="button" disabled={!valid}
          onClick={() => onChange({ preset: 'custom', start, end })}
          className="ios-btn-secondary rounded-md px-4 py-2 text-sm">应用</button>
      </div>
      {(start || end) && !valid && <p role="status" className="text-xs text-red-600">请选择有效的起止日期</p>}
    </>}
  </>;
};

export default DateRangeFilter;
