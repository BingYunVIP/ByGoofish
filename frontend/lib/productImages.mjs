/** Keep aligned with utils/product_images.py. CDN upgrades preserve signed paths/queries. */
export function normalizeImageUrl(input) {
  if (typeof input !== 'string') return '';
  let value = input.trim();
  if (!value || /[\x00-\x20\\]/.test(value)) return '';
  if (value.startsWith('/') && !value.startsWith('//')) return value;
  if (value.startsWith('//')) value = `https:${value}`;
  try {
    const url = new URL(value);
    if (url.username || url.password) return '';
    if (url.protocol === 'https:' && url.hostname) return value;
    const host = url.hostname.toLowerCase();
    // URL.port canonicalizes :80 to empty, so check the original authority as well.
    const authority = value.match(/^http:\/\/([^/?#]+)/i)?.[1] || '';
    if (url.protocol === 'http:' && (host === 'alicdn.com' || host.endsWith('.alicdn.com')) && !authority.includes(':')) {
      return `https${value.slice(4)}`;
    }
  } catch { /* Invalid and unsupported addresses render an explicit placeholder. */ }
  return '';
}
