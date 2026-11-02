import React, { useState } from 'react';
import { Box, ImageOff, Loader2 } from 'lucide-react';
import { normalizeImageUrl } from '../lib/productImages.mjs';

const ImageState: React.FC<{ src: string; title: string; missing: string }> = ({ src, title, missing }) => {
  const [status, setStatus] = useState<'loading' | 'ready' | 'failed'>('loading');
  if (!src || status === 'failed') {
    const label = src ? '图片加载失败' : missing;
    const Icon = src || missing !== '暂无图片' ? ImageOff : Box;
    return <div role="img" aria-label={label} title={label} className="flex h-full w-full flex-col items-center justify-center gap-1 px-1 text-[var(--text-soft)]">
      <Icon aria-hidden="true" className="h-5 w-5" /><span aria-hidden="true" className="max-w-full truncate text-[10px] leading-3">{src ? '加载失败' : missing === '暂无图片' ? '暂无图片' : '地址不可用'}</span>
    </div>;
  }
  return <div className="relative h-full w-full">
    {status === 'loading' && <span role="status" aria-label="图片加载中" className="absolute inset-0 flex items-center justify-center text-[var(--text-soft)]"><Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" /></span>}
    <img src={src} alt={title} loading="lazy" decoding="async" referrerPolicy="no-referrer"
      className={`h-full w-full object-cover ${status === 'ready' ? '' : 'opacity-0'}`}
      onLoad={() => setStatus('ready')} onError={() => setStatus('failed')} />
  </div>;
};

export default function ProductImage({ src, title = '商品图片', className = '' }: { src?: string; title?: string; className?: string }) {
  const normalized = normalizeImageUrl(src);
  return <div className={`h-full w-full ${className}`}><ImageState key={normalized || src || ''} src={normalized} title={title}
    missing={src?.trim() ? '图片地址不可用，请使用HTTPS图片' : '暂无图片'} /></div>;
}
