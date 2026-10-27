/** SQLite订单时间统一为UTC；显示与日期筛选使用同一浏览器时区。 */
export const formatOrderTime = (value?: string | null): string => {
  if (!value) return '-';
  const raw = value.trim().replace(' ', 'T');
  const utc = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw) ? raw : `${raw}Z`;
  const date = new Date(utc);
  if (!Number.isFinite(date.getTime())) return value;
  return date.toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).replaceAll('/', '-');
};
