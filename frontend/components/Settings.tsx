import React, { useEffect, useRef, useState } from 'react';
import {
  Database,
  Eye,
  EyeOff,
  KeyRound,
  Mail,
  RefreshCw,
  Save,
  Settings as SettingsIcon,
  ShieldCheck,
  Sparkles,
  UserRound,
  Zap,
  ImagePlus,
  X,
  Loader2,
} from 'lucide-react';

import { validateEmail, validatePassword } from '../lib/authValidation';
import { DEFAULT_AI_BASE_URL, normalizeApiBaseUrl } from '../lib/aiEndpoint';

import {
  changeEmail,
  changePassword,
  createQuickPhrase,
  deleteQuickPhrase,
  getAccountProfile,
  getQuickPhrases,
  sendChangeEmailCode,
  getSystemSettings,
  updateQuickPhrase,
  uploadImage,
  updateSystemSettings,
} from '../services/api';
import { notify } from '../services/feedback';
import { describeApiError } from '../lib/errorMessage';
import { QuickPhrase, SystemSettings } from '../types';
import ModelPicker from './ModelPicker';
import {
  PageHeader,
  PageLoading,
  PageTabs,
  SectionHeader,
} from './ui';

type SettingsSection = 'general' | 'ai' | 'phrases';

/**
 * 把后端的开关值转成布尔。
 *
 * 后端统一把开关存成 'true' / 'false' 字符串。直接拿来当布尔用会踩坑：
 * 'false' 本身是 truthy，关掉的开关在界面上仍显示开启；再点一次取反得到的
 * 还是 false，于是开关一旦关闭就再也打不开。
 */
const toBool = (value: unknown, fallback = false): boolean => {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (!normalized) return fallback;
    return !['false', '0', 'no'].includes(normalized);
  }
  if (value === undefined || value === null) return fallback;
  return Boolean(value);
};

interface SettingToggleProps {
  title: string;
  description: string;
  checked: boolean;
  onChange: () => void;
}

const SettingToggle: React.FC<SettingToggleProps> = ({
  title,
  description,
  checked,
  onChange,
}) => (
  <div className="flex items-center justify-between gap-4 border-b border-gray-100 px-4 py-3 last:border-b-0">
    <div className="min-w-0">
      <p className="text-sm font-bold text-gray-900">{title}</p>
      <p className="mt-1 text-xs leading-5 text-gray-500">{description}</p>
    </div>
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={title}
      onClick={onChange}
      className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${
        checked ? 'bg-[#ffe100]' : 'bg-gray-300'
      }`}
    >
      <span
        className={`absolute left-1 top-1 h-4 w-4 rounded-full bg-white transition-transform ${
          checked ? 'translate-x-5' : ''
        }`}
      />
    </button>
  </div>
);

const Settings: React.FC = () => {
  const [settings, setSettings] = useState<SystemSettings | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [activeSection, setActiveSection] = useState<SettingsSection>('general');
  // 快捷短语：人工客服常用话术
  const [phrases, setPhrases] = useState<QuickPhrase[]>([]);
  const [phraseForm, setPhraseForm] = useState({ category: '默认', title: '', content: '', image_url: '' });
  const [phraseImageUploading, setPhraseImageUploading] = useState(false);
  const phraseImageInput = useRef<HTMLInputElement>(null);

  const loadPhrases = () => {
    getQuickPhrases(true).then(setPhrases).catch(() => setPhrases([]));
  };

  useEffect(() => { loadPhrases(); }, []);

  const handleAddPhrase = async () => {
    if (!phraseForm.title.trim() || (!phraseForm.content.trim() && !phraseForm.image_url) || phraseImageUploading) return;
    await createQuickPhrase(
      phraseForm.title.trim(), phraseForm.content.trim(), phraseForm.category.trim() || '默认',
      0, phraseForm.image_url,
    );
    setPhraseForm({ category: phraseForm.category, title: '', content: '', image_url: '' });
    loadPhrases();
  };

  const handlePhraseImage = async (file?: File) => {
    if (!file) return;
    if (!['image/jpeg', 'image/png', 'image/gif', 'image/webp'].includes(file.type) || file.size > 5 * 1024 * 1024) {
      notify('请选择5MB以内的JPEG、PNG、GIF或WebP图片', 'warning');
      return;
    }
    setPhraseImageUploading(true);
    try {
      const result = await uploadImage(file);
      setPhraseForm((current) => ({ ...current, image_url: result.image_url }));
      notify('快捷短语图片已上传', 'success');
    } catch (error) {
      notify(describeApiError(error, '上传快捷短语图片失败'), 'error');
    } finally {
      setPhraseImageUploading(false);
    }
  };

  const handleTogglePhrase = async (phrase: QuickPhrase) => {
    await updateQuickPhrase(phrase.id, { enabled: !phrase.enabled });
    loadPhrases();
  };

  const handleDeletePhrase = async (id: number) => {
    await deleteQuickPhrase(id);
    loadPhrases();
  };

  const [showApiKey, setShowApiKey] = useState(false);

  // 修改登录密码。与页面顶部的「保存设置」互不影响：这里改的是当前账号的凭据，
  // 走的是独立接口，成功后立刻生效。
  const [passwordForm, setPasswordForm] = useState({ current: '', next: '', confirm: '' });
  const [showPasswordFields, setShowPasswordFields] = useState(false);
  const [changingPassword, setChangingPassword] = useState(false);
  const [currentEmail, setCurrentEmail] = useState('');
  const [emailForm, setEmailForm] = useState({ currentPassword: '', newEmail: '', verificationCode: '' });
  const [sendingEmailCode, setSendingEmailCode] = useState(false);
  const [changingEmail, setChangingEmail] = useState(false);

  const handleChangePassword = async () => {
    const { current, next, confirm } = passwordForm;
    if (!current || !next) {
      notify('请填写当前密码和新密码');
      return;
    }
    // 与后端同一套规则/文案（lib/authValidation.ts）
    const [pwOk, pwReason] = validatePassword(next);
    if (!pwOk) {
      notify(pwReason);
      return;
    }
    if (next !== confirm) {
      notify('两次输入的新密码不一致');
      return;
    }
    if (next === current) {
      notify('新密码不能与当前密码相同');
      return;
    }

    setChangingPassword(true);
    try {
      const result = await changePassword(current, next);
      // 后端对「当前密码错误」这类校验失败也返回 200，要看 success 字段
      if (result?.success === false) {
        notify(result.message || '密码修改失败');
        return;
      }
      setPasswordForm({ current: '', next: '', confirm: '' });
      notify('密码已修改，请重新登录', 'success');
      localStorage.removeItem('auth_token');
      window.dispatchEvent(new Event('auth:logout'));
    } catch (error) {
      notify(`密码修改失败：${(error as Error).message}`);
    } finally {
      setChangingPassword(false);
    }
  };

  const handleSendEmailCode = async () => {
    const email = emailForm.newEmail.trim();
    const [emailOk, emailReason] = validateEmail(email);
    if (!emailOk) {
      notify(emailReason);
      return;
    }
    if (!emailForm.currentPassword) {
      notify('请填写当前密码');
      return;
    }
    setSendingEmailCode(true);
    try {
      const result = await sendChangeEmailCode(email, emailForm.currentPassword);
      if (result?.success === false) {
        notify(result.message || '验证码发送失败');
        return;
      }
      notify(result.message || '验证码已发送到新邮箱', 'success');
    } catch (error) {
      notify(`验证码发送失败：${describeApiError(error)}`, 'error');
    } finally {
      setSendingEmailCode(false);
    }
  };

  const handleChangeEmail = async () => {
    const email = emailForm.newEmail.trim();
    const [emailOk, emailReason] = validateEmail(email);
    if (!emailOk) {
      notify(emailReason);
      return;
    }
    if (!emailForm.currentPassword || !emailForm.verificationCode.trim()) {
      notify('请填写当前密码和新邮箱验证码');
      return;
    }
    setChangingEmail(true);
    try {
      const result = await changeEmail(emailForm.currentPassword, email, emailForm.verificationCode.trim());
      if (result?.success === false) {
        notify(result.message || '登录邮箱修改失败');
        return;
      }
      setEmailForm({ currentPassword: '', newEmail: '', verificationCode: '' });
      notify(result.message || '登录邮箱已修改，请重新登录', 'success');
      localStorage.removeItem('auth_token');
      window.dispatchEvent(new Event('auth:logout'));
    } catch (error) {
      notify(`登录邮箱修改失败：${describeApiError(error)}`, 'error');
    } finally {
      setChangingEmail(false);
    }
  };

  useEffect(() => {
    loadSettings();
    getAccountProfile().then(profile => setCurrentEmail(profile.email || '')).catch(() => setCurrentEmail(''));
  }, []);

  const loadSettings = () => {
    setLoading(true);
    getSystemSettings()
      // 接口地址没配过时直接显示默认地址：它是输入框里的真实内容，可以直接修改
      .then(data => setSettings({ ...data, ai_api_url: data.ai_api_url || DEFAULT_AI_BASE_URL }))
      .finally(() => setLoading(false));
  };

  const handleSave = async () => {
    if (!settings) return;
    setSaving(true);
    try {
      // 接口地址自动补 /v1；后端会再校验一次并保存规范化后的值
      const apiUrl = normalizeApiBaseUrl(settings.ai_api_url || '') || DEFAULT_AI_BASE_URL;
      await updateSystemSettings({ ...settings, ai_api_url: apiUrl });
      setSettings({ ...settings, ai_api_url: apiUrl });
      notify('系统配置已保存');
    } catch (error) {
      notify(`保存失败：${describeApiError(error)}`);
    } finally {
      setSaving(false);
    }
  };

  if (!settings) return <PageLoading label="正在加载系统设置" />;

  return (
    <div className="page-stack animate-fade-in">
      <PageHeader
        title="系统设置"
        description="配置管理端访问、商品同步和默认 AI 参数。"
        icon={SettingsIcon}
        actions={(
          <>
            <button
              type="button"
              onClick={loadSettings}
              disabled={loading}
              className="ios-btn-secondary flex items-center justify-center gap-2 rounded-md px-4 py-2 text-sm"
            >
              <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
              刷新
            </button>
            <button
              type="button"
              onClick={() => void handleSave()}
              disabled={saving}
              className="ios-btn-primary flex items-center justify-center gap-2 rounded-md px-4 py-2 text-sm"
            >
              <Save className="h-4 w-4" />
              {saving ? '保存中' : '保存设置'}
            </button>
          </>
        )}
      />

      <PageTabs
        value={activeSection}
        onChange={setActiveSection}
        ariaLabel="系统设置分区"
        items={[
          { id: 'general', label: '账号与同步', icon: UserRound },
          { id: 'ai', label: '默认 AI 配置', icon: Sparkles },
          { id: 'phrases', label: '快捷短语', icon: Zap },
        ]}
      />

      {activeSection === 'general' && (
        <div className="grid gap-4 xl:grid-cols-2">
          <section className="section-panel">
            <SectionHeader
              title="访问与安全"
              description="控制后台注册入口、登录提示和验证码策略。"
              icon={ShieldCheck}
            />
            <SettingToggle
              title="允许用户注册"
              description="开启后允许新用户从登录页创建管理账号。"
              checked={toBool(settings.registration_enabled, false)}
              onChange={() => setSettings({
                ...settings,
                registration_enabled: !toBool(settings.registration_enabled, false),
              })}
            />
            <SettingToggle
              title="注册邮箱验证"
              description="要求注册时填写邮箱验证码，验证码通过「通知与日志」里已启用的邮件渠道发出。没有邮件渠道时请关闭，否则用户收不到验证码、无法完成注册。"
              checked={toBool(settings.email_verification_enabled, true)}
              onChange={() => setSettings({
                ...settings,
                email_verification_enabled: !toBool(settings.email_verification_enabled, true),
              })}
            />
            <SettingToggle
              title="显示默认登录信息"
              description="仅建议在本地调试环境显示默认账号提示。"
              checked={toBool(settings.show_default_login_info, false)}
              onChange={() => setSettings({
                ...settings,
                show_default_login_info: !toBool(settings.show_default_login_info, false),
              })}
            />
            <SettingToggle
              title="登录防爆破验证码"
              description="连续登录失败达阈值后强制图形验证码，防止密码被暴力尝试。"
              checked={toBool(settings.login_captcha_enabled, true)}
              onChange={() => setSettings({
                ...settings,
                login_captcha_enabled: !toBool(settings.login_captcha_enabled, true),
              })}
            />
          </section>

          <section className="section-panel order-3">
            <SectionHeader
              title="修改登录邮箱"
              description="验证当前密码和新邮箱验证码后生效；修改后需要使用新邮箱重新登录。"
              icon={Mail}
            />
            <div className="grid gap-4 p-4">
              <label>
                <span className="field-label">当前登录邮箱</span>
                <input
                  type="email"
                  value={currentEmail}
                  readOnly
                  className="ios-input w-full rounded-md px-3 py-2.5 text-sm"
                  aria-label="当前登录邮箱"
                />
              </label>
              <label>
                <span className="field-label">当前密码</span>
                <input
                  type="password"
                  value={emailForm.currentPassword}
                  onChange={event => setEmailForm({ ...emailForm, currentPassword: event.target.value })}
                  autoComplete="current-password"
                  className="ios-input w-full rounded-md px-3 py-2.5 text-sm"
                  placeholder="验证本人操作"
                />
              </label>
              <label>
                <span className="field-label">新登录邮箱</span>
                <input
                  type="email"
                  value={emailForm.newEmail}
                  onChange={event => setEmailForm({ ...emailForm, newEmail: event.target.value })}
                  autoComplete="email"
                  className="ios-input w-full rounded-md px-3 py-2.5 text-sm"
                  placeholder="name@example.com"
                />
              </label>
              <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
                <label>
                  <span className="field-label">新邮箱验证码</span>
                  <input
                    inputMode="numeric"
                    value={emailForm.verificationCode}
                    onChange={event => setEmailForm({ ...emailForm, verificationCode: event.target.value })}
                    autoComplete="one-time-code"
                    className="ios-input w-full rounded-md px-3 py-2.5 text-sm"
                    placeholder="输入邮件中的验证码"
                  />
                </label>
                <button
                  type="button"
                  onClick={() => void handleSendEmailCode()}
                  disabled={sendingEmailCode || !emailForm.currentPassword || !emailForm.newEmail.trim()}
                  className="ios-btn-secondary flex min-h-11 items-center justify-center gap-2 rounded-md px-4 py-2 text-sm disabled:opacity-50"
                >
                  {sendingEmailCode ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Mail className="h-4 w-4" />}
                  {sendingEmailCode ? '发送中' : '发送验证码'}
                </button>
              </div>
              <div className="flex justify-end">
                <button
                  type="button"
                  onClick={() => void handleChangeEmail()}
                  disabled={changingEmail || !emailForm.currentPassword || !emailForm.verificationCode.trim()}
                  className="ios-btn-primary flex items-center justify-center gap-2 rounded-md px-4 py-2 text-sm disabled:opacity-60"
                >
                  <Mail className="h-4 w-4" />
                  {changingEmail ? '修改中' : '修改登录邮箱'}
                </button>
              </div>
            </div>
          </section>

          <section className="section-panel order-2">
            <SectionHeader
              title="修改登录密码"
              description="修改当前登录账号的密码，保存后立即生效。"
              icon={KeyRound}
            />
            <div className="grid gap-4 p-4">
              <label>
                <span className="field-label">当前密码</span>
                <div className="relative">
                  <input
                    type={showPasswordFields ? 'text' : 'password'}
                    value={passwordForm.current}
                    onChange={e => setPasswordForm({ ...passwordForm, current: e.target.value })}
                    autoComplete="current-password"
                    placeholder="请输入当前密码"
                    className="ios-input w-full rounded-md px-3 py-2.5 pr-11 text-sm"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPasswordFields(!showPasswordFields)}
                    className="absolute right-2 top-1/2 -translate-y-1/2 rounded-md p-2 text-gray-400 hover:bg-gray-100 hover:text-gray-600"
                    aria-label={showPasswordFields ? '隐藏密码' : '显示密码'}
                  >
                    {showPasswordFields ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                  </button>
                </div>
              </label>

              <div className="grid gap-4 sm:grid-cols-2">
                <label>
                  <span className="field-label">新密码</span>
                  <input
                    type={showPasswordFields ? 'text' : 'password'}
                    value={passwordForm.next}
                    onChange={e => setPasswordForm({ ...passwordForm, next: e.target.value })}
                    autoComplete="new-password"
                    placeholder="至少 6 位"
                    className="ios-input w-full rounded-md px-3 py-2.5 text-sm"
                  />
                </label>
                <label>
                  <span className="field-label">确认新密码</span>
                  <input
                    type={showPasswordFields ? 'text' : 'password'}
                    value={passwordForm.confirm}
                    onChange={e => setPasswordForm({ ...passwordForm, confirm: e.target.value })}
                    autoComplete="new-password"
                    placeholder="再次输入新密码"
                    className="ios-input w-full rounded-md px-3 py-2.5 text-sm"
                  />
                </label>
              </div>

              <div className="flex justify-end">
                <button
                  type="button"
                  onClick={() => void handleChangePassword()}
                  disabled={changingPassword}
                  className="ios-btn-primary flex items-center justify-center gap-2 rounded-md px-4 py-2 text-sm disabled:opacity-60"
                >
                  <KeyRound className="h-4 w-4" />
                  {changingPassword ? '修改中' : '修改密码'}
                </button>
              </div>
            </div>
          </section>

          <section className="section-panel">
            <SectionHeader
              title="商品同步"
              description="设置后台定时获取闲鱼商品的频率和单次范围。"
              icon={Database}
            />
            <SettingToggle
              title="启用商品自动同步"
              description="定时将账号商品更新到本地商品库。"
              checked={toBool(settings.item_sync_enabled, true)}
              onChange={() => setSettings({
                ...settings,
                item_sync_enabled: !toBool(settings.item_sync_enabled, true),
              })}
            />
            <div className="grid gap-4 p-4 sm:grid-cols-2">
              <label>
                <span className="field-label">同步间隔（分钟）</span>
                <input
                  type="number"
                  value={Math.round((settings.item_sync_interval || 600) / 60)}
                  onChange={(event) => {
                    const minutes = parseInt(event.target.value, 10) || 10;
                    setSettings({ ...settings, item_sync_interval: minutes * 60 });
                  }}
                  className="ios-input w-full rounded-md px-3 py-2.5"
                  min="1"
                  max="1440"
                />
                <span className="mt-1 block text-xs text-gray-500">建议 10 至 60 分钟。</span>
              </label>
              <label>
                <span className="field-label">每次最多同步页数</span>
                <input
                  type="number"
                  value={settings.item_sync_max_pages || 5}
                  onChange={(event) => setSettings({
                    ...settings,
                    item_sync_max_pages: parseInt(event.target.value, 10) || 5,
                  })}
                  className="ios-input w-full rounded-md px-3 py-2.5"
                  min="1"
                  max="50"
                />
                <span className="mt-1 block text-xs text-gray-500">闲鱼接口通常每页返回 20 件商品。</span>
              </label>
            </div>
          </section>

          <section className="section-panel">
            <SectionHeader
              title="订单同步"
              description="定时从卖家端拉取订单，补齐监听离线期间产生的订单。"
              icon={Database}
            />
            <SettingToggle
              title="启用订单自动同步"
              description="关闭后只能在订单页手动点「拉取卖出订单」。"
              checked={settings.order_sync_enabled !== false && settings.order_sync_enabled !== 'false'}
              onChange={() => setSettings({
                ...settings,
                order_sync_enabled: settings.order_sync_enabled === false || settings.order_sync_enabled === 'false',
              })}
            />
            <div className="grid gap-4 p-4 sm:grid-cols-2">
              <label>
                <span className="field-label">同步间隔（分钟）</span>
                <input
                  type="number"
                  value={Math.round((settings.order_sync_interval || 1800) / 60)}
                  onChange={(event) => {
                    const minutes = parseInt(event.target.value, 10) || 30;
                    setSettings({ ...settings, order_sync_interval: minutes * 60 });
                  }}
                  className="ios-input w-full rounded-md px-3 py-2.5"
                  min="5"
                  max="1440"
                />
                <span className="mt-1 block text-xs text-gray-500">最低 5 分钟，建议 30 分钟。</span>
              </label>
            </div>
          </section>

          <section className="section-panel">
            <SectionHeader
              title="商品擦亮"
              description="定时擦亮商品重新获取搜索曝光，平台对每日次数有限制。"
              icon={Database}
            />
            <SettingToggle
              title="启用自动擦亮"
              description="开启后按下方间隔自动擦亮全部商品。也可在商品页手动触发。"
              checked={settings.auto_polish_enabled === true || settings.auto_polish_enabled === 'true'}
              onChange={() => setSettings({
                ...settings,
                auto_polish_enabled: !(settings.auto_polish_enabled === true || settings.auto_polish_enabled === 'true'),
              })}
            />
            <div className="grid gap-4 p-4 sm:grid-cols-2">
              <label>
                <span className="field-label">擦亮间隔（小时）</span>
                <input
                  type="number"
                  value={Math.round((settings.auto_polish_interval || 21600) / 3600)}
                  onChange={(event) => {
                    const hours = parseInt(event.target.value, 10) || 6;
                    setSettings({ ...settings, auto_polish_interval: hours * 3600 });
                  }}
                  className="ios-input w-full rounded-md px-3 py-2.5"
                  min="1"
                  max="24"
                />
                <span className="mt-1 block text-xs text-gray-500">最短 1 小时，建议 6 小时。</span>
              </label>
            </div>
          </section>

        </div>
      )}

      {activeSection === 'phrases' && (
        <section className="section-panel">
          <SectionHeader
            title="快捷短语"
            description="人工客服常用话术，在消息管理页可一键插入到输入框。"
            icon={Zap}
          />
          <div className="grid min-w-0 gap-3 p-4 sm:grid-cols-2 xl:grid-cols-[120px_150px_minmax(0,1fr)_auto]">
            <input
              value={phraseForm.category}
              onChange={(e) => setPhraseForm({ ...phraseForm, category: e.target.value })}
              placeholder="分类"
              className="ios-input rounded-md px-3 py-2.5"
            />
            <input
              value={phraseForm.title}
              onChange={(e) => setPhraseForm({ ...phraseForm, title: e.target.value })}
              placeholder="标题"
              className="ios-input rounded-md px-3 py-2.5"
            />
            <div className="flex min-w-0 items-center gap-2 sm:col-span-2 xl:col-span-1">
              <input
                value={phraseForm.content}
                onChange={(e) => setPhraseForm({ ...phraseForm, content: e.target.value })}
                placeholder="话术内容"
                aria-label="话术内容"
                className="ios-input min-w-0 flex-1 rounded-md px-3 py-2.5"
              />
              <input
                ref={phraseImageInput}
                type="file"
                accept="image/jpeg,image/png,image/gif,image/webp"
                className="sr-only"
                aria-label="上传快捷短语图片"
                onChange={(event) => {
                  void handlePhraseImage(event.target.files?.[0]);
                  event.target.value = '';
                }}
              />
              <button
                type="button"
                onClick={() => phraseImageInput.current?.click()}
                disabled={phraseImageUploading}
                className="ios-btn-secondary flex min-h-11 shrink-0 items-center gap-2 whitespace-nowrap rounded-md px-3 py-2 text-sm disabled:opacity-60"
              >
                {phraseImageUploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <ImagePlus className="h-4 w-4" />}
                {phraseImageUploading ? '上传中…' : phraseForm.image_url ? '更换图片' : '添加图片（可选）'}
              </button>
            </div>
            {phraseForm.image_url && (
              <div className="order-last flex min-w-0 items-center gap-2 sm:col-span-2 xl:col-span-4">
                <>
                  <img
                    src={`/${phraseForm.image_url.replace(/^\/+/, '')}`}
                    alt="快捷短语图片预览"
                    className="h-10 w-10 rounded border border-[var(--border)] object-cover"
                  />
                  <button
                    type="button"
                    aria-label="移除快捷短语图片"
                    onClick={() => setPhraseForm((current) => ({ ...current, image_url: '' }))}
                    className="chat-icon"
                  >
                    <X className="h-4 w-4" />
                  </button>
                  <span className="truncate text-xs text-gray-500">图片已上传，保存短语后可在消息中心发送</span>
                </>
              </div>
            )}
            <button
              type="button"
              onClick={() => void handleAddPhrase()}
              disabled={!phraseForm.title.trim() || (!phraseForm.content.trim() && !phraseForm.image_url) || phraseImageUploading}
              className="ios-btn-primary rounded-md px-4 py-2.5 text-sm disabled:opacity-60"
            >
              添加
            </button>
          </div>
          <div className="divide-y divide-gray-100 border-t border-gray-100">
            {phrases.length === 0 ? (
              <p className="px-4 py-6 text-center text-sm text-gray-500">还没有快捷短语</p>
            ) : (
              phrases.map((phrase) => (
                <div key={phrase.id} className="flex items-center gap-3 px-4 py-3">
                  <span className="w-20 shrink-0 text-xs text-gray-500">{phrase.category}</span>
                  {phrase.image_url && (
                    <img
                      src={`/${phrase.image_url.replace(/^\/+/, '')}`}
                      alt={`${phrase.title}快捷短语图片`}
                      className="h-10 w-10 shrink-0 rounded border border-[var(--border)] object-cover"
                    />
                  )}
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-semibold text-gray-800">{phrase.title}</p>
                    <p className="truncate text-xs text-gray-500">{phrase.content || '仅图片'}</p>
                  </div>
                  <span className="shrink-0 text-xs text-gray-400">用了 {phrase.use_count} 次</span>
                  <button
                    type="button"
                    onClick={() => void handleTogglePhrase(phrase)}
                    className="shrink-0 text-xs text-blue-600 hover:underline"
                  >
                    {phrase.enabled ? '停用' : '启用'}
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleDeletePhrase(phrase.id)}
                    className="shrink-0 text-xs text-red-500 hover:underline"
                  >
                    删除
                  </button>
                </div>
              ))
            )}
          </div>
        </section>
      )}

      {activeSection === 'ai' && (
        <section className="section-panel">
          <SectionHeader
            title="默认 AI 配置"
            description="作为账号未单独配置时使用的全局模型与回复内容。"
            icon={Sparkles}
          />
          <div className="grid gap-4 p-4 lg:grid-cols-2">
            <label>
              <span className="field-label">API 地址</span>
              <input
                type="text"
                value={settings.ai_api_url ?? ''}
                onChange={(event) => setSettings({ ...settings, ai_api_url: event.target.value })}
                onBlur={() => setSettings(current => current && ({
                  ...current,
                  ai_api_url: normalizeApiBaseUrl(current.ai_api_url || '') || DEFAULT_AI_BASE_URL,
                }))}
                className="ios-input w-full rounded-md px-3 py-2.5 text-sm"
                placeholder={DEFAULT_AI_BASE_URL}
                autoComplete="off"
                spellCheck={false}
              />
            </label>

            <label>
              <span className="field-label">API Key</span>
              <div className="relative">
                <input
                  type={showApiKey ? 'text' : 'password'}
                  value={settings.ai_api_key || ''}
                  onChange={(event) => setSettings({ ...settings, ai_api_key: event.target.value })}
                  className="ios-input w-full rounded-md px-3 py-2.5 pr-11 font-mono text-sm"
                  placeholder={settings.ai_api_key_configured ? '已配置，留空保留原密钥' : 'sk-...'}
                  autoComplete="new-password"
                />
                <button
                  type="button"
                  onClick={() => setShowApiKey(!showApiKey)}
                  className="absolute right-2 top-1/2 -translate-y-1/2 rounded-md p-2 text-gray-400 hover:bg-gray-100 hover:text-gray-600"
                  aria-label={showApiKey ? '隐藏 API Key' : '显示 API Key'}
                >
                  {showApiKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
            </label>

            <div>
              <label htmlFor="default-ai-model" className="field-label">默认模型</label>
              <ModelPicker
                id="default-ai-model"
                value={settings.ai_model ?? ''}
                onChange={(model) => setSettings({ ...settings, ai_model: model })}
                baseUrl={settings.ai_api_url ?? ''}
                apiKey={settings.ai_api_key ?? ''}
                scope={{ kind: 'system' }}
                onBaseUrlResolved={(url) => setSettings(current => current && ({ ...current, ai_api_url: url }))}
                placeholder="例如 gpt-6-sol"
              />
            </div>

            <label className="lg:col-span-2">
              <span className="field-label">默认自动回复内容</span>
              <textarea
                className="ios-input min-h-28 w-full resize-y rounded-md px-3 py-2.5 text-sm"
                value={settings.default_reply || ''}
                onChange={(event) => setSettings({ ...settings, default_reply: event.target.value })}
                placeholder="设置默认的自动回复内容..."
              />
            </label>

          </div>
        </section>
      )}
    </div>
  );
};

export default Settings;
