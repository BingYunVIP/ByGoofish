import React, { useState } from 'react';
import MessageWorkspace from './messages/MessageWorkspace';
import MessageFilters from './messages/MessageFilters';
export default function MessageManagement({ isActive = true }: { isActive?: boolean }) {
  const [filters, setFilters] = useState(false);
  return (
    <>
      <div className={filters ? 'hidden' : 'h-full min-h-0'}>
        <MessageWorkspace isActive={isActive && !filters} onFilters={() => setFilters(true)} />
      </div>
      {filters && <MessageFilters onBack={() => setFilters(false)} />}
    </>
  );
}
