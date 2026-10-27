import React, { useEffect, useRef, useState } from 'react';
import { Search } from 'lucide-react';
export default function ConversationSearch({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  const input = useRef<HTMLInputElement>(null),
    touched = useRef(false);
  const [unlocked, setUnlocked] = useState(false),
    [name] = useState(() => `conversation-filter-${crypto.randomUUID()}`);
  useEffect(() => {
    const clear = () => {
      if (!touched.current && input.current) input.current.value = '';
    };
    clear();
    const timers = [100, 500, 1500].map((delay) => setTimeout(clear, delay));
    return () => timers.forEach(clearTimeout);
  }, []);
  return (
    <div className="relative">
      <Search className="absolute left-3 top-3 text-[var(--text-soft)]" size={15} />
      <input
        ref={input}
        aria-label="搜索联系人、商品或消息"
        autoComplete="new-password"
        data-lpignore="true"
        data-1p-ignore="true"
        data-bwignore="true"
        readOnly={!unlocked}
        name={name}
        type="search"
        placeholder="搜索已加载会话"
        className="ios-input w-full rounded-md py-2.5 pl-9 pr-2 text-xs"
        value={value}
        onFocus={() => {
          touched.current = true;
          setUnlocked(true);
        }}
        onBlur={() => setUnlocked(false)}
        onChange={(e) => {
          if (touched.current) onChange(e.target.value);
        }}
      />
    </div>
  );
}
