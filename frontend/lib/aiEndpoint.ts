/** 默认 AI 接口地址（服务根地址，含 /v1）。与后端 DEFAULT_AI_BASE_URL 保持一致。 */
export const DEFAULT_AI_BASE_URL = 'https://api.bingyun.vip/v1';

const ENDPOINT_SUFFIXES = ['/chat/completions', '/completions', '/responses', '/messages', '/models'];
// v1、v4、v1beta 这类版本段；路径里已经有就不再追加 /v1。
const VERSION_SEGMENT = /^v\d+(?:alpha|beta)?\d*$/i;

/**
 * 整理用户填写的接口地址，规则与后端 normalize_base_url 一致：
 * 补协议、去掉误粘贴的接口路径（如 /chat/completions），路径里没有版本段时自动追加 /v1。
 * 留空返回空串；无法识别为网址时原样返回，由后端给出明确的错误提示。
 */
export const normalizeApiBaseUrl = (raw: string): string => {
  const text = raw.trim();
  if (!text) return '';
  let url: URL;
  try {
    url = new URL(text.includes('://') ? text : `https://${text}`);
  } catch {
    return text;
  }
  if ((url.protocol !== 'http:' && url.protocol !== 'https:') || url.username || url.password) return text;
  let path = url.pathname.replace(/\/+$/, '');
  const suffix = ENDPOINT_SUFFIXES.find(item => path.toLowerCase().endsWith(item));
  if (suffix) path = path.slice(0, -suffix.length).replace(/\/+$/, '');
  if (!path.split('/').some(segment => segment && VERSION_SEGMENT.test(segment))) path += '/v1';
  return `${url.protocol}//${url.host}${path}`;
};
