/** Keep credential-change logout and late network responses scoped to their session. */
export interface LogoutNotice { message?: string; identifier?: string; credentialsChanged?: boolean }

export function clearAuthSession(expectedToken?: string | null, notice: LogoutNotice = {}): boolean {
  const current = localStorage.getItem('auth_token');
  if (expectedToken !== undefined && current !== expectedToken && !(current === null && notice.credentialsChanged)) return false;
  localStorage.removeItem('auth_token');
  window.dispatchEvent(new CustomEvent<LogoutNotice>('auth:logout', { detail: notice }));
  return true;
}

export function loginIdentity(identifier: string): { email: string } | { username: string } {
  const value = identifier.trim();
  return value.includes('@') ? { email: value.toLowerCase() } : { username: value };
}

export function isUnconfirmedRequest(error: unknown): boolean {
  const value = error as { code?: string; response?: unknown };
  return !value?.response && ['ECONNABORTED', 'ETIMEDOUT', 'ERR_NETWORK'].includes(value?.code || '');
}
