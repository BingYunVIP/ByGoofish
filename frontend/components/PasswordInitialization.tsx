import React, { useRef, useState } from 'react';
import { Eye, EyeOff, KeyRound, Loader2, LogOut, ShieldCheck } from 'lucide-react';
import { clearAuthSession, isUnconfirmedRequest } from '../lib/authSession';
import { validatePassword } from '../lib/authValidation';
import { describeApiError } from '../lib/errorMessage';
import { changePassword, logout } from '../services/api';
import ThemeToggle from './ThemeToggle';

/** Mounted instead of the workbench, so bootstrap sessions make no business requests. */
const PasswordInitialization: React.FC = () => {
  const [form, setForm] = useState({ current: '', next: '', confirm: '' });
  const [visible, setVisible] = useState({ current: false, next: false, confirm: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const locked = useRef(false);
  const errorBox = useRef<HTMLDivElement>(null);

  const showError = (message: string) => {
    setError(message);
    requestAnimationFrame(() => errorBox.current?.focus());
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (locked.current) return;
    const [valid, reason] = validatePassword(form.next);
    if (!form.current) return showError('请填写当前密码');
    if (!valid) return showError(reason);
    if (form.next !== form.confirm) return showError('两次输入的新密码不一致');
    if (form.next === form.current) return showError('新密码不能与当前密码相同');
    const token = localStorage.getItem('auth_token');
    locked.current = true;
    setBusy(true);
    setError('');
    try {
      const result = await changePassword(form.current, form.next);
      if (localStorage.getItem('auth_token') !== token) return;
      if (result.success !== true) {
        showError(result.message || '密码修改结果未确认，请退出后使用新密码登录确认，不要重复提交');
        return;
      }
      setForm({ current: '', next: '', confirm: '' });
      clearAuthSession(token, {
        message: result.message || '密码已修改，请使用新密码重新登录',
        identifier: result.profile?.username,
        credentialsChanged: true,
      });
    } catch (failure) {
      if (localStorage.getItem('auth_token') === token) {
        showError(isUnconfirmedRequest(failure)
          ? '密码修改结果未确认，请退出后使用新密码登录确认，不要重复提交'
          : describeApiError(failure, '密码修改失败，请稍后重试'));
      }
    } finally {
      locked.current = false;
      setBusy(false);
    }
  };

  const exit = async () => {
    if (locked.current) return;
    const token = localStorage.getItem('auth_token');
    locked.current = true;
    setBusy(true);
    try {
      await logout();
      clearAuthSession(token, { message: '已退出登录，请重新登录' });
    } catch (failure) {
      showError(describeApiError(failure, '退出未完成，请检查连接后重试'));
    } finally {
      locked.current = false;
      setBusy(false);
    }
  };

  return (
    <main className="flex min-h-screen items-center justify-center bg-[var(--app-bg)] px-4 py-8 text-[var(--text)]">
      <section aria-labelledby="password-initialization-title" className="w-full max-w-md rounded-xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-sm sm:p-8">
        <div className="mb-6 flex items-center justify-between gap-3">
          <span className="flex items-center gap-2 text-sm font-bold"><ShieldCheck aria-hidden="true" className="h-5 w-5 text-[var(--brand-text)]" />闲鱼超级管家</span>
          <ThemeToggle compact />
        </div>
        <h1 id="password-initialization-title" className="text-xl font-bold">请先修改登录密码</h1>
        <p id="password-initialization-description" className="mt-2 text-sm leading-6 text-[var(--text-muted)]">当前账号仍使用初始密码。完成修改并重新登录后，即可进入工作台。</p>
        <form onSubmit={submit} aria-describedby="password-initialization-description" className="mt-6 space-y-4">
          {([
            ['current', '当前密码', 'current-password'],
            ['next', '新密码', 'new-password'],
            ['confirm', '确认新密码', 'new-password'],
          ] as const).map(([key, label, autocomplete]) => (
            <div key={key}>
              <label htmlFor={`initial-password-${key}`} className="mb-1.5 block text-sm font-semibold">{label}</label>
              <div className="relative">
                <input id={`initial-password-${key}`} required type={visible[key] ? 'text' : 'password'} autoComplete={autocomplete}
                  value={form[key]} disabled={busy}
                  placeholder={key === 'next' ? '8–64位，字母+数字，≤72字节' : `请输入${label}`}
                  title={key === 'next' ? '新密码需8–64位，同时包含字母和数字，且不超过72字节。' : undefined}
                  onChange={event => setForm(previous => ({ ...previous, [key]: event.target.value }))}
                  aria-describedby={key === 'next' ? 'password-rules' : undefined}
                  className="ios-input h-11 w-full rounded-md py-2.5 pl-3 pr-12 text-sm placeholder:text-xs disabled:opacity-60" />
                <button type="button" disabled={busy} aria-controls={`initial-password-${key}`}
                  onClick={() => setVisible(previous => ({ ...previous, [key]: !previous[key] }))}
                  aria-pressed={visible[key]} aria-label={`${visible[key] ? '隐藏' : '显示'}${label}`}
                  title={`${visible[key] ? '隐藏' : '显示'}${label}`}
                  className="absolute inset-y-0 right-0 flex w-11 items-center justify-center rounded-md text-[var(--text-muted)] hover:text-[var(--text)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--brand)] disabled:opacity-60">
                  {visible[key] ? <EyeOff aria-hidden="true" className="h-4 w-4" /> : <Eye aria-hidden="true" className="h-4 w-4" />}
                </button>
              </div>
            </div>
          ))}
          <p id="password-rules" className="sr-only">新密码需8–64位，同时包含字母和数字，且不超过72字节。</p>
          <div ref={errorBox} role={error ? 'alert' : undefined} tabIndex={-1} className={error ? 'rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm leading-6 text-red-800' : 'hidden'}>{error}</div>
          <button type="submit" disabled={busy} className="ios-btn-primary flex min-h-11 w-full items-center justify-center gap-2 rounded-md px-4 py-2.5 text-sm disabled:opacity-60">
            {busy ? <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" /> : <KeyRound aria-hidden="true" className="h-4 w-4" />}修改密码并重新登录
          </button>
          <button type="button" onClick={() => void exit()} disabled={busy} className="ios-btn-secondary flex min-h-11 w-full items-center justify-center gap-2 rounded-md px-4 py-2.5 text-sm disabled:opacity-60">
            <LogOut aria-hidden="true" className="h-4 w-4" />退出登录
          </button>
        </form>
      </section>
    </main>
  );
};

export default PasswordInitialization;
