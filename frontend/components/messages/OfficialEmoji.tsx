import React, { useState } from 'react';
export interface OfficialEmoji { alias: string; name: string; url: string }
export function EmojiIcon({ item }: { item: OfficialEmoji; key?: number | string }) {
  const [failed, setFailed] = useState(false);
  return item.url && !failed ? <img src={item.url} alt={item.alias} title={item.name}
    className="inline-block h-6 w-6 align-middle object-contain" onError={() => setFailed(true)} /> : <span>{item.alias}</span>;
}
export function MessageText({ text, emojis }: { text: string; emojis: OfficialEmoji[] }) {
  const mapping = new Map(emojis.map(item => [item.alias, item]));
  return <>{text.split(/(\[[^\[\]]{1,38}\])/g).map((part,index) => mapping.has(part)
    ? <EmojiIcon key={index} item={mapping.get(part)!} /> : <span key={index}>{part}</span>)}</>;
}
