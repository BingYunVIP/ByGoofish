import { createPortal } from 'react-dom';
import { formatOrderTime as formatTime } from '../lib/orderTime';
import { useDialogFocus } from '../lib/useDialogFocus';
import BrowserDiagnostics from './BrowserDiagnostics';
import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  BellRing,
  Check,
  CircleAlert,
  ChevronLeft,
  ChevronRight,
  Edit3,
  Link2,
  Loader2,
  Plus,
  RefreshCw,
  Save,
  Send,
  ShieldAlert,
  Trash2,
  X,
} from 'lucide-react';

import {
  AccountDetail,
  MessageNotification,
  NotificationChannel,
  NotificationChannelType,
  NotificationEventDefinition,
  NotificationPriority,
  NotificationPriorityDefinition,
  RiskControlLog,
  SystemLog,
} from '../types';
import {
  createNotificationChannel,
  deleteMessageNotification,
  deleteNotificationChannel,
  deleteRiskControlLog,
  getAccountDetails,
  getMessageNotifications,
  getNotificationChannels,
  getNotificationEvents,
  getRiskControlLogs,
  getSystemLogs,
  setMessageNotification,
  testEmailChannel,
  testMessageNotification,
  updateMessageNotificationRule,
  acknowledgeNotification,
  updateNotificationChannel,
} from '../services/api';
import { confirmAction, notify } from '../services/feedback';
import { describeApiError } from '../lib/errorMessage';
import { accountDisplayName } from '../lib/accountDisplay';
import { EmptyState, PageHeader, PageTabs, SectionHeader } from './ui';
import NotificationTemplateEditor from './NotificationTemplateEditor';

type PageTab = 'channels' | 'preview' | 'bindings' | 'risk' | 'system';

interface NotificationsAndLogsProps {
  isAdmin: boolean;
}

interface ChannelDefinition {
  label: string;
  fields: Array<{
    key: string;
    label: string;
    placeholder?: string;
    type?: 'text' | 'password' | 'number' | 'textarea' | 'select';
    options?: string[];
    optional?: boolean;
    maxLength?: number;
  }>;
  defaults: Record<string, unknown>;
}

type ChannelField = ChannelDefinition['fields'][number];

const CHANNEL_DEFINITIONS: Record<NotificationChannelType, ChannelDefinition> = {
  dingtalk: {
    label: '钉钉',
    fields: [
      { key: 'webhook_url', label: 'Webhook 地址', placeholder: 'https://oapi.dingtalk.com/robot/send?...' },
      { key: 'secret', label: '加签密钥', type: 'password', optional: true },
    ],
    defaults: {},
  },
  feishu: {
    label: '飞书',
    fields: [
      { key: 'webhook_url', label: 'Webhook 地址', placeholder: 'https://open.feishu.cn/open-apis/bot/v2/hook/...' },
      { key: 'secret', label: '签名密钥', type: 'password', optional: true },
    ],
    defaults: {},
  },
  bark: {
    label: 'Bark',
    fields: [
      { key: 'device_key', label: 'Device Key', type: 'password' },
      { key: 'server_url', label: '服务器地址', placeholder: 'https://api.day.app', optional: true },
    ],
    defaults: { server_url: 'https://api.day.app' },
  },
  email: {
    label: '邮件',
    fields: [
      { key: 'smtp_server', label: 'SMTP 服务器', placeholder: 'smtp.qq.com' },
      { key: 'smtp_port', label: 'SMTP 端口', type: 'number' },
      { key: 'email_user', label: '发件邮箱' },
      { key: 'email_password', label: '邮箱密码或授权码', type: 'password' },
      { key: 'recipient_email', label: '接收邮箱' },
      { key: 'smtp_from', label: '发件人显示名', placeholder: '留空则只显示发件邮箱', maxLength: 80 },
    ],
    // QQ 邮箱：smtp.qq.com，465 端口（SSL）
    defaults: { smtp_server: 'smtp.qq.com', smtp_port: 465 },
  },
  webhook: {
    label: 'Webhook',
    fields: [
      { key: 'webhook_url', label: 'Webhook 地址' },
      { key: 'http_method', label: '请求方法', type: 'select', options: ['POST', 'PUT'] },
      { key: 'headers', label: '请求头 JSON', type: 'textarea', placeholder: '{"Authorization":"Bearer ..."}', optional: true },
    ],
    defaults: { http_method: 'POST', headers: '{}' },
  },
  wechat: {
    label: '企业微信',
    fields: [
      { key: 'webhook_url', label: '机器人 Webhook 地址' },
    ],
    defaults: {},
  },
  telegram: {
    label: 'Telegram',
    fields: [
      { key: 'bot_token', label: 'Bot Token', type: 'password' },
      { key: 'chat_id', label: 'Chat ID' },
    ],
    defaults: {},
  },
};

const PAGE_SIZE = 20;
const DEFAULT_TEST_EVENT = 'login_verification_required';

type NotificationTestState = {
  status: 'sending' | 'skipped' | 'error';
  message: string;
  requestId?: string;
};

const getNotificationTestErrorMessage = (error: unknown): string => {
  const detail = (error as {
    response?: { data?: { detail?: unknown } };
  } | undefined)?.response?.data?.detail;
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === 'string' && message.trim()) return message.trim();
  }
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  return error instanceof Error && error.message ? error.message : '测试发送失败，请稍后重试';
};

const accountLabel = (account: AccountDetail) => accountDisplayName(account);

const NotificationsAndLogs: React.FC<NotificationsAndLogsProps> = ({ isAdmin }) => {
  const [diagnosticLog, setDiagnosticLog] = useState<RiskControlLog | null>(null);
  const diagnosticDialog = useDialogFocus(Boolean(diagnosticLog), () => setDiagnosticLog(null));
  const [activeTab, setActiveTab] = useState<PageTab>('channels');
  const [accounts, setAccounts] = useState<AccountDetail[]>([]);
  const [channels, setChannels] = useState<NotificationChannel[]>([]);
  const [bindings, setBindings] = useState<MessageNotification[]>([]);
  const [notificationTestStates, setNotificationTestStates] = useState<Record<string, NotificationTestState>>({});
  const [notificationTestEvents, setNotificationTestEvents] = useState<Record<string, string>>({});
  const testingRules = useRef(new Set<string>());
  const [eventDefinitions, setEventDefinitions] = useState<NotificationEventDefinition[]>([]);
  const [priorityDefinitions, setPriorityDefinitions] = useState<NotificationPriorityDefinition[]>([]);
  const [loadingBase, setLoadingBase] = useState(true);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editingChannel, setEditingChannel] = useState<NotificationChannel | null>(null);
  const [channelType, setChannelType] = useState<NotificationChannelType>('dingtalk');
  const [channelName, setChannelName] = useState('');
  const [channelConfig, setChannelConfig] = useState<Record<string, unknown>>({});
  const [savingChannel, setSavingChannel] = useState(false);
  const [emailTesting, setEmailTesting] = useState(false);
  const emailTestingRef = useRef(false);
  const [emailTestStatus, setEmailTestStatus] = useState<{ tone: 'success' | 'error'; text: string } | null>(null);
  const [ruleEditorOpen, setRuleEditorOpen] = useState(false);
  const ruleDialog = useDialogFocus(ruleEditorOpen, () => setRuleEditorOpen(false));
  const [editingRule, setEditingRule] = useState<MessageNotification | null>(null);
  const [ruleAccount, setRuleAccount] = useState('');
  const [ruleChannel, setRuleChannel] = useState('');
  const [ruleName, setRuleName] = useState('');
  const [ruleEventTypes, setRuleEventTypes] = useState<string[]>([]);
  const [ruleReminders,setRuleReminders] = useState(2);
  const [ruleInterval,setRuleInterval] = useState(5);
  const [acknowledging,setAcknowledging] = useState<number | null>(null);
  const [savingRule, setSavingRule] = useState(false);
  const [riskLogs, setRiskLogs] = useState<RiskControlLog[]>([]);
  const [riskTotal, setRiskTotal] = useState(0);
  const [riskAccount, setRiskAccount] = useState('');
  const [riskStatus, setRiskStatus] = useState('');
  const [riskSeverity, setRiskSeverity] = useState('');
  const [riskPage, setRiskPage] = useState(0);
  const [riskLoading, setRiskLoading] = useState(false);
  const [systemLogs, setSystemLogs] = useState<SystemLog[]>([]);
  const [systemLevel, setSystemLevel] = useState('');
  const [systemSource, setSystemSource] = useState('');
  const [systemLoading, setSystemLoading] = useState(false);

  const loadBaseData = async () => {
    setLoadingBase(true);
    try {
      const [accountData, channelData, bindingData, eventData] = await Promise.all([
        getAccountDetails(),
        getNotificationChannels(),
        getMessageNotifications(),
        getNotificationEvents(),
      ]);
      setAccounts(accountData);
      setChannels(channelData.data);
      setBindings(bindingData.data);
      setNotificationTestStates((current) => {
        const existingIds = new Set(bindingData.data.map((item) => String(item.id)));
        return Object.fromEntries(
          Object.entries(current).filter(([ruleId]) => existingIds.has(ruleId)),
        );
      });
      setEventDefinitions(eventData.events);
      setPriorityDefinitions(eventData.priorities);
    } catch (error) {
      notify(`加载通知配置失败：${(error as Error).message}`);
    } finally {
      setLoadingBase(false);
    }
  };

  useEffect(() => {
    void loadBaseData();
  }, []);

  const openCreateEditor = () => {
    const type: NotificationChannelType = 'dingtalk';
    setEditingChannel(null);
    setChannelType(type);
    setChannelName('');
    setChannelConfig({ ...CHANNEL_DEFINITIONS[type].defaults });
    setEmailTestStatus(null);
    setEditorOpen(true);
  };

  const openEditEditor = (channel: NotificationChannel) => {
    setEditingChannel(channel);
    setChannelType(channel.type);
    setChannelName(channel.name);
    setChannelConfig({ ...CHANNEL_DEFINITIONS[channel.type].defaults, ...channel.config });
    setEmailTestStatus(null);
    setEditorOpen(true);
  };

  const changeChannelType = (type: NotificationChannelType) => {
    setChannelType(type);
    setChannelConfig({ ...CHANNEL_DEFINITIONS[type].defaults });
    setEmailTestStatus(null);
  };

  // 用编辑窗口里当前填写的内容发一封测试邮件，不用先保存；授权码留空则沿用已保存的
  const runEmailTest = async () => {
    if (emailTestingRef.current) return;
    emailTestingRef.current = true;
    setEmailTesting(true);
    setEmailTestStatus(null);
    try {
      const result = await testEmailChannel({
        channel_id: editingChannel ? Number(editingChannel.id) : undefined,
        smtp_server: String(channelConfig.smtp_server ?? ''),
        smtp_port: Number(channelConfig.smtp_port) || 0,
        email_user: String(channelConfig.email_user ?? ''),
        email_password: String(channelConfig.email_password ?? ''),
        smtp_from: String(channelConfig.smtp_from ?? ''),
        recipient_email: String(channelConfig.recipient_email ?? ''),
      });
      setEmailTestStatus({
        tone: 'success',
        text: `${result.message}${result.request_id ? `（追踪 ID：${result.request_id}）` : ''}`,
      });
    } catch (error) {
      setEmailTestStatus({ tone: 'error', text: describeApiError(error, '测试发信失败，请稍后重试') });
    } finally {
      emailTestingRef.current = false;
      setEmailTesting(false);
    }
  };

  const renderChannelField = (field: ChannelField, className = '') => (
    <label key={field.key} className={`block text-sm font-bold text-gray-700 ${className}`}>
      {field.label}{field.optional ? '（可选）' : ''}
      {field.type === 'textarea' ? (
        <textarea
          value={String(channelConfig[field.key] ?? '')}
          onChange={(event) => setChannelConfig({ ...channelConfig, [field.key]: event.target.value })}
          placeholder={editingChannel?.configured_fields?.includes(field.key) ? '已配置，留空保留原值' : field.placeholder}
          rows={4}
          className="ios-input mt-2 w-full resize-y rounded-md px-3 py-2.5 font-mono text-sm font-normal"
        />
      ) : field.type === 'select' ? (
        <select
          value={String(channelConfig[field.key] ?? '')}
          onChange={(event) => setChannelConfig({ ...channelConfig, [field.key]: event.target.value })}
          className="ios-input mt-2 w-full rounded-md px-3 py-2.5 font-normal"
        >
          {field.options?.map((option) => <option key={option}>{option}</option>)}
        </select>
      ) : (
        <input
          type={field.type || 'text'}
          value={String(channelConfig[field.key] ?? '')}
          maxLength={field.maxLength}
          onChange={(event) => setChannelConfig({
            ...channelConfig,
            [field.key]: field.type === 'number' ? Number(event.target.value) : event.target.value,
          })}
          placeholder={editingChannel?.configured_fields?.includes(field.key) ? '已配置，留空保留原值' : field.placeholder}
          className="ios-input mt-2 w-full rounded-md px-3 py-2.5 font-normal"
        />
      )}
    </label>
  );

  const saveChannel = async () => {
    setSavingChannel(true);
    try {
      if (editingChannel) {
        await updateNotificationChannel(editingChannel.id, {
          name: channelName,
          config: channelConfig,
          enabled: editingChannel.enabled,
        });
      } else {
        await createNotificationChannel({
          name: channelName,
          type: channelType,
          config: channelConfig,
        });
      }
      setEditorOpen(false);
      await loadBaseData();
    } catch (error) {
      notify(`保存通知渠道失败：${(error as Error).message}`);
    } finally {
      setSavingChannel(false);
    }
  };

  const toggleChannel = async (channel: NotificationChannel) => {
    try {
      await updateNotificationChannel(channel.id, { enabled: !channel.enabled });
      setChannels((current) => current.map((item) => (
        item.id === channel.id ? { ...item, enabled: !item.enabled } : item
      )));
      if (channel.enabled) {
        setBindings((current) => current.filter((item) => item.channel_id !== Number(channel.id)));
      } else {
        await loadBaseData();
      }
    } catch (error) {
      notify(`更新渠道状态失败：${(error as Error).message}`);
    }
  };

  const removeChannel = async (channel: NotificationChannel) => {
    if (!await confirmAction(`确认删除通知渠道“${channel.name}”？相关账号绑定也会被删除。`)) return;
    try {
      await deleteNotificationChannel(channel.id);
      await loadBaseData();
    } catch (error) {
      notify(`删除通知渠道失败：${(error as Error).message}`);
    }
  };

  const eventLabel = (eventId: string) =>
    eventDefinitions.find((item) => item.id === eventId)?.label || eventId;

  const ruleEventSummary = (binding: MessageNotification) => {
    if (binding.event_types.length === 0) return '未订阅任何事件';
    return binding.event_types.map(eventLabel).join('、');
  };

  const priorityGroups: NotificationPriorityDefinition[] = priorityDefinitions.length > 0
    ? priorityDefinitions
    : Array.from(new Set(eventDefinitions.map((item) => item.priority))).map((priority) => ({
      id: priority as NotificationPriority,
      label: priority === 'error' ? '错误' : priority === 'warning' ? '警告' : '通知',
    }));

  const openCreateRuleEditor = () => {
    setEditingRule(null);
    setRuleAccount(accounts[0]?.id || '');
    setRuleChannel('');
    setRuleName('');
    setRuleReminders(2);setRuleInterval(5);
    setRuleEventTypes(eventDefinitions.filter((item) => item.default_subscribed).map((item) => item.id));
    setRuleEditorOpen(true);
  };

  const openEditRuleEditor = (binding: MessageNotification) => {
    setEditingRule(binding);
    setRuleAccount(binding.cookie_id);
    setRuleChannel(String(binding.channel_id));
    setRuleName(binding.name || '');
    setRuleReminders(binding.reminder_limit ?? 2);setRuleInterval(binding.reminder_interval_minutes ?? 5);
    setRuleEventTypes(binding.event_types);
    setRuleEditorOpen(true);
  };

  const toggleRuleEvent = (eventId: string) => {
    setRuleEventTypes((current) => (
      current.includes(eventId)
        ? current.filter((item) => item !== eventId)
        : [...current, eventId]
    ));
  };

  const applyRulePreset = (preset: NotificationPriority | 'all' | 'none') => {
    if (preset === 'all') {
      setRuleEventTypes(eventDefinitions.map((item) => item.id));
      return;
    }
    if (preset === 'none') {
      setRuleEventTypes([]);
      return;
    }
    setRuleEventTypes(
      eventDefinitions.filter((item) => item.priority === preset).map((item) => item.id),
    );
  };

  const saveRule = async () => {
    if (!Number.isInteger(ruleReminders) || ruleReminders < 1 || ruleReminders > 10 || !Number.isInteger(ruleInterval) || ruleInterval < 5 || ruleInterval > 1440) {
      notify('提醒次数应为1–10次，间隔应为5–1440分钟');return;
    }
    if (editingRule) {
      setSavingRule(true);
      try {
        await updateMessageNotificationRule(editingRule.id, {
          name: ruleName.trim(),
          eventTypes: ruleEventTypes,
          reminderLimit: ruleReminders, reminderInterval: ruleInterval,
        });
        setRuleEditorOpen(false);
        await loadBaseData();
      } catch (error) {
        notify(`保存通知规则失败：${(error as Error).message}`);
      } finally {
        setSavingRule(false);
      }
      return;
    }

    if (!ruleAccount || !ruleChannel) {
      notify('请选择账号和通知渠道');
      return;
    }
    setSavingRule(true);
    try {
      await setMessageNotification(ruleAccount, Number(ruleChannel), true, {
        name: ruleName.trim(),
        eventTypes: ruleEventTypes,
        reminderLimit: ruleReminders, reminderInterval: ruleInterval,
      });
      setRuleEditorOpen(false);
      await loadBaseData();
    } catch (error) {
      notify(`创建通知规则失败：${(error as Error).message}`);
    } finally {
      setSavingRule(false);
    }
  };

  const toggleBinding = async (binding: MessageNotification) => {
    try {
      await updateMessageNotificationRule(binding.id, { enabled: !binding.enabled });
      await loadBaseData();
    } catch (error) {
      notify(`更新通知规则失败：${(error as Error).message}`);
    }
  };

  const testBinding = async (binding: MessageNotification, eventType: string) => {
    const ruleId = String(binding.id);
    if (testingRules.current.has(ruleId)) return;
    const event = eventDefinitions.find((item) => item.id === eventType);
    if (!event) {
      notify('请选择有效的通知内容');
      return;
    }
    testingRules.current.add(ruleId);
    try {
      if (!await confirmAction(
        `将按“${event.label}”测试“${binding.channel_name}”规则。只有该规则已启用并订阅此事件时才会投递；测试通知不代表账号发生异常。`,
        { title: '确认测试通知内容', confirmLabel: '发送测试通知', danger: false },
      )) return;
      setNotificationTestStates((current) => ({
        ...current,
        [ruleId]: { status: 'sending', message: '检查并发送中' },
      }));
      const result = await testMessageNotification(binding.id, {
        mode: 'event',
        event_type: eventType,
      });
      if (result?.success !== true) {
        throw new Error(result?.message?.trim() || '通知测试未成功，请检查渠道配置后重试');
      }
      if (result.skipped || result.sent === false) {
        const message = result.message || '未订阅该事件，未发送任何通知';
        setNotificationTestStates((current) => ({
          ...current, [ruleId]: { status: 'skipped', message, requestId: result.request_id },
        }));
        notify(message, 'info');
        return;
      }
      setNotificationTestStates((current) => {
        const next = { ...current };
        delete next[ruleId];
        return next;
      });
      const destination = binding.channel_type === 'email' ? '请检查邮箱' : '请检查接收端';
      notify(`“${event.label}”测试通知已提交，${destination}。`, 'success');
    } catch (error) {
      const message = getNotificationTestErrorMessage(error);
      setNotificationTestStates((current) => ({
        ...current,
        [ruleId]: { status: 'error', message },
      }));
      notify(`测试发送失败：${message}`, 'error');
    } finally {
      testingRules.current.delete(ruleId);
    }
  };

  const removeBinding = async (binding: MessageNotification) => {
    const ruleLabel = binding.name || binding.channel_name;
    if (!await confirmAction(`确认删除通知规则“${ruleLabel}”？`)) return;
    try {
      await deleteMessageNotification(binding.id);
      setBindings((current) => current.filter((item) => item.id !== binding.id));
      setNotificationTestStates((current) => {
        const next = { ...current };
        delete next[String(binding.id)];
        return next;
      });
    } catch (error) {
      notify(`删除通知规则失败：${(error as Error).message}`);
    }
  };

  const loadRiskLogs = async (page = riskPage) => {
    setRiskLoading(true);
    try {
      const result = await getRiskControlLogs({
        cookie_id: riskAccount || undefined,
        processing_status: riskStatus || undefined,
        severity: riskSeverity || undefined,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      });
      setRiskLogs(result.data || []);
      setRiskTotal(result.total || 0);
    } catch (error) {
      notify(`加载风控日志失败：${(error as Error).message}`);
    } finally {
      setRiskLoading(false);
    }
  };

  useEffect(() => {
    if (activeTab === 'risk') void loadRiskLogs();
  }, [activeTab, riskAccount, riskStatus, riskSeverity, riskPage]);

  const removeRiskLog = async (log: RiskControlLog) => {
    if (!await confirmAction('确认删除这条风控日志？')) return;
    try {
      const result = await deleteRiskControlLog(log.id);
      if (result.success === false) throw new Error(result.message || '删除失败');
      await loadRiskLogs();
    } catch (error) {
      notify(`删除风控日志失败：${(error as Error).message}`);
    }
  };

  const loadSystemLogs = async () => {
    if (!isAdmin) return;
    setSystemLoading(true);
    try {
      const result = await getSystemLogs({
        lines: 300,
        level: systemLevel || undefined,
        source: systemSource.trim() || undefined,
      });
      if (!result.success) throw new Error(result.message || '加载失败');
      setSystemLogs(result.logs || []);
    } catch (error) {
      notify(`加载系统日志失败：${(error as Error).message}`);
    } finally {
      setSystemLoading(false);
    }
  };

  useEffect(() => {
    if (activeTab === 'system' && isAdmin) void loadSystemLogs();
  }, [activeTab]);

  const riskPageCount = Math.max(1, Math.ceil(riskTotal / PAGE_SIZE));

  const tabs: Array<{ id: PageTab; label: string; icon: typeof BellRing }> = [
    { id: 'channels', label: '通知渠道', icon: BellRing },
    { id: 'preview', label: '通知预览', icon: BellRing },
    { id: 'bindings', label: '账号通知', icon: Link2 },
    { id: 'risk', label: '风控日志', icon: ShieldAlert },
    ...(isAdmin ? [{ id: 'system' as PageTab, label: '系统日志', icon: Activity }] : []),
  ];

  return (
    <div className="page-stack animate-fade-in">
      <PageHeader
        title="通知与日志"
        description="统一管理外部通知渠道、账号绑定、风控事件和系统运行日志。"
        icon={BellRing}
        actions={(
          <button
            type="button"
            onClick={() => void loadBaseData()}
            disabled={loadingBase}
            className="ios-btn-secondary flex items-center justify-center gap-2 rounded-md px-4 py-2.5 text-sm disabled:opacity-50"
          >
            <RefreshCw className={`h-4 w-4 ${loadingBase ? 'animate-spin' : ''}`} />
            刷新配置
          </button>
        )}
      />

      <PageTabs
        value={activeTab}
        onChange={setActiveTab}
        items={tabs}
        ariaLabel="通知与日志功能"
      />

      {activeTab === 'preview' && <NotificationTemplateEditor events={eventDefinitions} />}

      {activeTab === 'channels' && (
        <section className="section-panel">
          <SectionHeader
            title="通知渠道"
            description="密钥仅用于服务端发送通知，请避免在日志或截图中公开。"
            icon={BellRing}
            actions={(
              <button
                type="button"
                onClick={openCreateEditor}
                className="ios-btn-primary flex items-center gap-2 rounded-md px-4 py-2.5 text-sm"
              >
                <Plus className="h-4 w-4" />
                新建渠道
              </button>
            )}
          />
          <div className="divide-y divide-gray-100 px-4">
            {channels.map((channel) => (
              <div key={channel.id} className="flex flex-col gap-3 py-4 sm:flex-row sm:items-center">
                <button
                  type="button"
                  role="switch"
                  aria-checked={channel.enabled}
                  onClick={() => void toggleChannel(channel)}
                  className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${channel.enabled ? 'bg-[#ffe100]' : 'bg-gray-300'}`}
                  title={channel.enabled ? '停用渠道' : '启用渠道'}
                >
                  <span className={`absolute left-1 top-1 h-4 w-4 rounded-full bg-white transition-transform ${channel.enabled ? 'translate-x-5' : ''}`} />
                </button>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-bold text-gray-900">{channel.name}</span>
                    <span className="rounded bg-gray-100 px-2 py-0.5 text-xs font-bold text-gray-600">
                      {CHANNEL_DEFINITIONS[channel.type]?.label || channel.type}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-gray-500">
                    {channel.enabled ? '可用于账号通知' : '已停用'}
                    {channel.updated_at ? ` · 更新于 ${formatTime(channel.updated_at)}` : ''}
                  </p>
                </div>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => openEditEditor(channel)}
                    title="编辑渠道"
                    className="flex h-9 w-9 items-center justify-center rounded-md bg-gray-100 text-gray-700 hover:bg-gray-200"
                  >
                    <Edit3 className="h-4 w-4" />
                  </button>
                  <button
                    type="button"
                    onClick={() => void removeChannel(channel)}
                    title="删除渠道"
                    className="flex h-9 w-9 items-center justify-center rounded-md bg-red-50 text-red-600 hover:bg-red-100"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              </div>
            ))}
            {!loadingBase && channels.length === 0 && (
              <EmptyState compact title="暂无通知渠道" description="新建渠道后可绑定到指定闲鱼账号。" icon={BellRing} />
            )}
          </div>
        </section>
      )}

      {activeTab === 'bindings' && (
        <section className="section-panel">
          <SectionHeader
            title="账号通知规则"
            description="选择通知内容后按当前规则测试；只有规则已启用且订阅了该事件时才会投递。测试不代表账号发生异常。"
            icon={Link2}
            actions={(
              <button
                type="button"
                onClick={openCreateRuleEditor}
                className="ios-btn-primary flex items-center gap-2 rounded-md px-4 py-2.5 text-sm"
              >
                <Plus className="h-4 w-4" />
                新建规则
              </button>
            )}
          />
          <div className="divide-y divide-gray-100 px-4">
            {bindings.map((binding) => {
              const account = accounts.find((item) => item.id === binding.cookie_id);
              const testState = notificationTestStates[String(binding.id)];
              const isTesting = testState?.status === 'sending';
              return (
                <div
                  key={binding.id}
                  className="flex flex-col gap-3 py-4 sm:flex-row sm:items-center"
                  aria-busy={isTesting || undefined}
                >
                  <button
                    type="button"
                    role="switch"
                    aria-checked={binding.enabled}
                    aria-label={`${binding.name || binding.channel_name}通知规则开关`}
                    onClick={() => void toggleBinding(binding)}
                    className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${binding.enabled ? 'bg-[#ffe100]' : 'bg-gray-300'}`}
                    title={binding.enabled ? '暂停规则' : '启用规则'}
                  >
                    <span className={`absolute left-1 top-1 h-4 w-4 rounded-full bg-white transition-transform ${binding.enabled ? 'translate-x-5' : ''}`} />
                  </button>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="font-bold text-gray-900">{binding.name || '默认规则'}</p>
                      <span className="rounded bg-gray-100 px-2 py-0.5 text-xs font-bold text-gray-600">
                        {binding.channel_name}
                      </span>
                    </div>
                    <p className="mt-1 text-xs text-gray-500">
                      {account ? accountLabel(account) : binding.cookie_id} · {
                        binding.channel_enabled === false
                          ? '渠道已停用'
                          : binding.enabled ? '接收通知' : '已暂停'
                      }
                    </p>
                    <p className="mt-1 break-words text-xs text-gray-500">
                      通知内容：{ruleEventSummary(binding)}
                    </p>
                    <p className="mt-1 text-xs text-gray-500">最多提醒{binding.reminder_limit ?? 2}次 · 间隔{binding.reminder_interval_minutes ?? 5}分钟
                    </p>
                  </div>
                  <div className="flex min-w-0 flex-col gap-2 sm:w-full sm:max-w-[30rem] sm:shrink-0">
                    <div className="flex min-w-0 flex-wrap items-center justify-end gap-2">
                    <select
                      aria-label={`通知内容${binding.name || binding.channel_name}`}
                      value={notificationTestEvents[String(binding.id)] || DEFAULT_TEST_EVENT}
                      onChange={(event) => {
                        const key = String(binding.id);
                        setNotificationTestEvents((current) => ({ ...current, [key]: event.target.value }));
                        setNotificationTestStates((current) => {
                          const next = { ...current };
                          delete next[key];
                          return next;
                        });
                      }}
                      disabled={isTesting}
                      className="ios-input min-w-0 max-w-full flex-1 rounded-md px-2 py-2 text-xs sm:flex-none"
                    >
                      {eventDefinitions.map((item) => (
                        <option key={item.id} value={item.id}>
                          {eventDefinitions.find((definition) => definition.id === item.id)?.label || item.label}
                        </option>
                      ))}
                    </select>
                    <button
                      type="button"
                      onClick={() => void testBinding(binding, notificationTestEvents[String(binding.id)] || DEFAULT_TEST_EVENT)}
                      disabled={isTesting}
                      aria-label={`事件投递测试${binding.name || binding.channel_name}`}
                      className="ios-btn-secondary flex min-h-11 items-center justify-center gap-1.5 rounded-md px-3 py-2 text-xs disabled:cursor-wait disabled:opacity-60"
                    >
                      {isTesting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
                      {isTesting ? '处理中' : '事件投递测试'}
                    </button>
                    <button
                      type="button"
                      onClick={() => openEditRuleEditor(binding)}
                      title="编辑规则"
                      className="flex h-9 w-9 items-center justify-center rounded-md bg-gray-100 text-gray-700 hover:bg-gray-200"
                    >
                      <Edit3 className="h-4 w-4" />
                    </button>
                    <button
                      type="button"
                      onClick={() => void removeBinding(binding)}
                      title="删除规则"
                      className="flex h-9 w-9 items-center justify-center rounded-md bg-red-50 text-red-600 hover:bg-red-100"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                    </div>
                    {testState && testState.status !== 'sending' && (
                      <div
                        role="status"
                        className={`flex min-w-0 items-start gap-2 rounded-md border px-3 py-2 text-xs leading-5 ${
                          testState.status === 'skipped' ? 'border-gray-200 bg-gray-50 text-gray-600'
                            : 'border-red-200 bg-red-50 text-red-700'
                        }`}
                        title={testState.requestId ? `请求 ID：${testState.requestId}` : undefined}
                      >
                        <CircleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                        <span className="min-w-0 break-words">
                          {testState.message}
                        </span>
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
            {!loadingBase && bindings.length === 0 && (
              <EmptyState compact title="暂无账号通知规则" description="先创建并启用通知渠道，再为账号添加通知规则。" icon={Link2} />
            )}
          </div>
        </section>
      )}

      {activeTab === 'risk' && (
        <section className="section-panel">
          <SectionHeader
            title="事件与风控日志"
            description="按级别查看账号认证、消息连接和业务事件，核对处理状态与建议操作。"
            icon={ShieldAlert}
          />
          <div className="toolbar rounded-none border-x-0 border-t-0 shadow-none">
            <div className="toolbar__group">
            <select value={riskSeverity} aria-label="事件级别" onChange={(event) => {
              setRiskSeverity(event.target.value); setRiskPage(0);
            }} className="ios-input rounded-md px-3 py-2.5 text-sm">
              <option value="">全部级别</option>
              <option value="error">Error · 错误</option>
              <option value="warning">Warn · 警告</option>
              <option value="info">Info · 通知</option>
            </select>
            <select
              value={riskAccount}
              onChange={(event) => {
                setRiskAccount(event.target.value);
                setRiskPage(0);
              }}
              className="ios-input rounded-md px-3 py-2.5 text-sm sm:min-w-56"
            >
              <option value="">全部账号</option>
              {accounts.map((account) => (
                <option key={account.id} value={account.id}>{accountLabel(account)}</option>
              ))}
            </select>
            <select
              value={riskStatus}
              onChange={(event) => {
                setRiskStatus(event.target.value);
                setRiskPage(0);
              }}
              className="ios-input rounded-md px-3 py-2.5 text-sm sm:min-w-40"
            >
              <option value="">全部状态</option>
              <option value="processing">处理中</option>
              <option value="success">成功</option>
              <option value="failed">失败</option>
              <option value="cancelled">已取消</option>
            </select>
            <button
              type="button"
              onClick={() => void loadRiskLogs()}
              disabled={riskLoading}
              className="ios-btn-secondary flex items-center justify-center gap-2 rounded-md px-4 py-2.5 text-sm"
            >
              <RefreshCw className={`h-4 w-4 ${riskLoading ? 'animate-spin' : ''}`} />
              刷新
            </button>
            </div>
          </div>
          <div className="divide-y divide-gray-100 px-4">
            {riskLogs.map((log) => (
              <div key={log.id} className="grid min-w-0 gap-3 py-4 lg:grid-cols-[180px_fit-content(18rem)_minmax(0,1fr)_44px] lg:items-start">
                <div>
                  <p className="break-all text-sm font-bold text-gray-900">{log.cookie_id}</p>
                  <p className="mt-1 text-xs text-gray-500">{formatTime(log.created_at)}</p>
                </div>
                <div className="min-w-0">
                  <span className={`mb-1 inline-flex rounded px-2 py-1 text-[10px] font-bold ${log.severity === 'error' ? 'bg-red-50 text-red-700' : log.severity === 'warning' ? 'bg-amber-50 text-amber-800' : 'bg-gray-100 text-gray-600'}`}>
                    {log.severity === 'error' ? 'Error' : log.severity === 'warning' ? 'Warn' : 'Info'}
                  </span>
                  <span className={`inline-flex rounded px-2 py-1 text-xs font-bold ${
                    log.processing_status === 'success'
                      ? 'bg-emerald-50 text-emerald-700'
                      : log.processing_status === 'failed'
                        ? 'bg-red-50 text-red-700'
                        : log.processing_status === 'cancelled' ? 'bg-gray-100 text-gray-600' : 'bg-amber-50 text-amber-700'
                  }`}>
                    {log.processing_status === 'success' ? '成功' : log.processing_status === 'failed' ? '失败' : log.processing_status === 'cancelled' ? '已取消' : '处理中'}
                  </span>
                  <p className="mt-2 text-xs text-gray-500 [overflow-wrap:anywhere]">{eventLabel(log.event_type)}</p>
                  {(log.occurrences || 0) > 1 && <p className="mt-1 text-xs text-gray-500">同一故障累计 {log.occurrences} 次</p>}
                </div>
                <div className="min-w-0 text-sm leading-6 text-gray-700">
                  <p className="[overflow-wrap:anywhere]">{log.event_description || log.processing_result || '无详细描述'}</p>
                  {log.processing_result && log.event_description && (
                    <p className="mt-1 text-xs text-gray-500 [overflow-wrap:anywhere]">{log.processing_result}</p>
                  )}
                  {log.error_message && log.processing_status !== 'cancelled' && (
                    <p className="mt-1 break-words text-xs text-red-600">{log.error_message}</p>
                  )}
                  {log.recommended_action && <p className="mt-2 rounded-md bg-gray-50 px-3 py-2 text-xs leading-5 text-gray-700">建议操作：{log.recommended_action}</p>}
                  {log.notification_incident && <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-gray-500">
                    <span>各规则合计尝试{log.notification_incident.attempts}次 · {log.notification_incident.resolved ? '已解决' : log.notification_incident.acknowledged ? '本轮提醒已停止' : '按规则提醒'}</span>
                    {!log.notification_incident.resolved && !log.notification_incident.acknowledged && <button type="button" disabled={acknowledging===log.id}
                      onClick={async()=>{setAcknowledging(log.id);try { await acknowledgeNotification(log.id);await loadRiskLogs();notify('已停止本轮后续提醒'); } catch { notify('操作失败，请重试'); } finally { setAcknowledging(null); }}}
                      className="ios-btn-secondary rounded-md px-3 py-2 text-xs disabled:opacity-50">标记已处理</button>}
                  </div>}
                  <button type="button" onClick={() => setDiagnosticLog(log)}
                    className="ios-btn-secondary mt-2 rounded-md px-3 py-2 text-xs">查看浏览器日志</button>
                </div>
                <button
                  type="button"
                  onClick={() => void removeRiskLog(log)}
                  title="删除日志"
                  className="flex h-9 w-9 items-center justify-center rounded-md bg-red-50 text-red-600 hover:bg-red-100"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            ))}
            {!riskLoading && riskLogs.length === 0 && (
              <EmptyState compact title="没有符合条件的风控日志" description="调整账号或状态筛选条件后重新查询。" icon={ShieldAlert} />
            )}
          </div>
          <div className="flex items-center justify-between border-t border-gray-200 px-4 py-3">
            <p className="text-xs text-gray-500">共 {riskTotal} 条</p>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => setRiskPage((page) => Math.max(0, page - 1))}
                disabled={riskPage === 0}
                title="上一页"
                className="flex h-9 w-9 items-center justify-center rounded-md bg-gray-100 disabled:opacity-40"
              >
                <ChevronLeft className="h-4 w-4" />
              </button>
              <span className="min-w-20 text-center text-sm font-bold">{riskPage + 1} / {riskPageCount}</span>
              <button
                type="button"
                onClick={() => setRiskPage((page) => Math.min(riskPageCount - 1, page + 1))}
                disabled={riskPage + 1 >= riskPageCount}
                title="下一页"
                className="flex h-9 w-9 items-center justify-center rounded-md bg-gray-100 disabled:opacity-40"
              >
                <ChevronRight className="h-4 w-4" />
              </button>
            </div>
          </div>
        </section>
      )}

      {activeTab === 'system' && isAdmin && (
        <section className="section-panel">
          <SectionHeader
            title="系统运行日志"
            description="最多读取最近 300 行，可按级别和来源快速定位运行异常。"
            icon={Activity}
          />
          <div className="grid gap-3 border-b border-gray-200 bg-gray-50/60 p-4 sm:grid-cols-[160px_minmax(0,1fr)_auto_auto]">
            <select
              value={systemLevel}
              onChange={(event) => setSystemLevel(event.target.value)}
              className="ios-input rounded-md px-3 py-2.5 text-sm"
            >
              <option value="">全部级别</option>
              <option value="DEBUG">DEBUG</option>
              <option value="INFO">INFO</option>
              <option value="WARNING">WARNING</option>
              <option value="ERROR">ERROR</option>
            </select>
            <input
              value={systemSource}
              onChange={(event) => setSystemSource(event.target.value)}
              placeholder="按日志来源筛选"
              className="ios-input rounded-md px-3 py-2.5 text-sm"
            />
            <button
              type="button"
              aria-pressed={systemSource === 'logistics_quote'}
              onClick={() => setSystemSource((value) => value === 'logistics_quote' ? '' : 'logistics_quote')}
              className={`rounded-md px-3 py-2.5 text-sm font-bold ${
                systemSource === 'logistics_quote'
                  ? 'bg-[#ffe100] text-gray-900'
                  : 'bg-white text-gray-700 ring-1 ring-gray-200 hover:bg-gray-100'
              }`}
            >
              物流报价
            </button>
            <button
              type="button"
              onClick={() => void loadSystemLogs()}
              disabled={systemLoading}
              className="ios-btn-secondary flex items-center justify-center gap-2 rounded-md px-4 py-2.5 text-sm"
            >
              <RefreshCw className={`h-4 w-4 ${systemLoading ? 'animate-spin' : ''}`} />
              查询
            </button>
          </div>
          <div className="max-h-[620px] divide-y divide-gray-100 overflow-y-auto px-4 font-mono text-xs">
            {[...systemLogs].reverse().map((log, index) => (
              <div key={`${log.timestamp}-${index}`} className="grid gap-2 py-3 lg:grid-cols-[165px_80px_180px_minmax(0,1fr)]">
                <span className="text-gray-500">{formatTime(log.timestamp)}</span>
                <span className={`font-bold ${
                  log.level === 'ERROR' ? 'text-red-600' : log.level === 'WARNING' ? 'text-amber-700' : 'text-gray-700'
                }`}>{log.level}</span>
                <span className="truncate text-gray-500" title={log.source}>{log.source}</span>
                <span className="break-words text-gray-800">{log.message}</span>
              </div>
            ))}
            {!systemLoading && systemLogs.length === 0 && (
              <EmptyState compact title="暂无系统日志" description="当前筛选条件没有返回运行记录。" icon={Activity} />
            )}
          </div>
        </section>
      )}

      {editorOpen && (
        <div className="modal-overlay">
          <div className={`modal-container ${channelType === 'email' ? 'max-w-2xl' : 'max-w-xl'}`}>
            <div className="modal-header flex items-start justify-between gap-4">
              <div>
                <h3 className="text-lg font-bold text-gray-900">{editingChannel ? '编辑通知渠道' : '新建通知渠道'}</h3>
                <p className="mt-1 text-sm text-gray-500">配置服务端发送通知所需的连接信息。</p>
              </div>
              <button
                type="button"
                onClick={() => setEditorOpen(false)}
                title="关闭"
                className="flex h-9 w-9 items-center justify-center rounded-md hover:bg-gray-100"
              >
                <X className="h-5 w-5" />
              </button>
            </div>
            <div className="modal-body space-y-4">
              <label className="block text-sm font-bold text-gray-700">
                渠道名称
                <input
                  value={channelName}
                  onChange={(event) => setChannelName(event.target.value)}
                  placeholder="例如：订单告警群"
                  className="ios-input mt-2 w-full rounded-md px-3 py-2.5 font-normal"
                />
              </label>
              <label className="block text-sm font-bold text-gray-700">
                渠道类型
                <select
                  value={channelType}
                  disabled={Boolean(editingChannel)}
                  onChange={(event) => changeChannelType(event.target.value as NotificationChannelType)}
                  className="ios-input mt-2 w-full rounded-md px-3 py-2.5 font-normal disabled:bg-gray-100"
                >
                  {Object.entries(CHANNEL_DEFINITIONS).map(([type, definition]) => (
                    <option key={type} value={type}>{definition.label}</option>
                  ))}
                </select>
              </label>
              {channelType === 'email' ? (
                <div className="grid gap-4 sm:grid-cols-2">
                  {CHANNEL_DEFINITIONS.email.fields
                    .filter((field) => field.key !== 'smtp_from')
                    .map((field) => renderChannelField(field, field.key === 'recipient_email' ? 'sm:col-span-2' : ''))}
                  {/* 发件人显示名与测试发信放在同一行 */}
                  <div className="flex flex-wrap items-end gap-3 sm:col-span-2">
                    {renderChannelField(
                      CHANNEL_DEFINITIONS.email.fields.find((field) => field.key === 'smtp_from') as ChannelField,
                      'min-w-[12rem] flex-1',
                    )}
                    <div>
                      <span className="block text-sm font-bold text-gray-700">测试发信</span>
                      <button
                        type="button"
                        onClick={() => void runEmailTest()}
                        disabled={emailTesting}
                        className="ios-btn-secondary mt-2 flex items-center gap-2 whitespace-nowrap rounded-md px-4 py-2.5 text-sm disabled:opacity-50"
                      >
                        {emailTesting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                        {emailTesting ? '发送中' : '发送测试邮件'}
                      </button>
                    </div>
                  </div>
                  {emailTestStatus && (
                    <p
                      role={emailTestStatus.tone === 'error' ? 'alert' : 'status'}
                      className={`text-xs leading-5 sm:col-span-2 ${emailTestStatus.tone === 'error' ? 'text-red-600' : 'text-emerald-700'}`}
                    >
                      {emailTestStatus.text}
                    </p>
                  )}
                </div>
              ) : CHANNEL_DEFINITIONS[channelType].fields.map((field) => renderChannelField(field))}
            </div>
            <div className="modal-footer flex justify-end gap-3">
              <button
                type="button"
                onClick={() => setEditorOpen(false)}
                className="ios-btn-secondary rounded-md px-4 py-2.5 text-sm"
              >
                取消
              </button>
              <button
                type="button"
                onClick={() => void saveChannel()}
                disabled={savingChannel}
                className="ios-btn-primary flex items-center gap-2 rounded-md px-4 py-2.5 text-sm disabled:opacity-50"
              >
                {savingChannel ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                保存
              </button>
            </div>
          </div>
        </div>
      )}

      {ruleEditorOpen && (
        <div className="modal-overlay">
          <div ref={ruleDialog} role="dialog" aria-modal="true" aria-labelledby="notification-rule-title" className="modal-container max-w-2xl">
            <div className="modal-header flex items-start justify-between gap-4">
              <div>
                <h3 id="notification-rule-title" className="text-lg font-bold text-gray-900">{editingRule ? '编辑通知规则' : '新建通知规则'}</h3>
                <p className="mt-1 text-sm text-gray-500">只发送已勾选的事件；全部取消后不再接收自动通知。</p>
              </div>
              <button
                type="button"
                onClick={() => setRuleEditorOpen(false)}
                title="关闭"
                className="flex h-9 w-9 items-center justify-center rounded-md hover:bg-gray-100"
              >
                <X className="h-5 w-5" />
              </button>
            </div>
            <div className="modal-body space-y-4">
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="block text-sm font-bold text-gray-700">
                  账号
                  <select
                    value={ruleAccount}
                    disabled={Boolean(editingRule)}
                    onChange={(event) => setRuleAccount(event.target.value)}
                    className="ios-input mt-2 w-full rounded-md px-3 py-2.5 font-normal disabled:bg-gray-100"
                  >
                    <option value="">选择账号</option>
                    {accounts.map((account) => (
                      <option key={account.id} value={account.id}>{accountLabel(account)}</option>
                    ))}
                  </select>
                </label>
                <label className="block text-sm font-bold text-gray-700">
                  通知渠道
                  <select
                    value={ruleChannel}
                    disabled={Boolean(editingRule)}
                    onChange={(event) => setRuleChannel(event.target.value)}
                    className="ios-input mt-2 w-full rounded-md px-3 py-2.5 font-normal disabled:bg-gray-100"
                  >
                    <option value="">选择已启用渠道</option>
                    {channels.filter((channel) => channel.enabled).map((channel) => (
                      <option key={channel.id} value={channel.id}>{channel.name}</option>
                    ))}
                  </select>
                </label>
              </div>
              <label className="block text-sm font-bold text-gray-700">
                规则名称（可选）
                <input
                  value={ruleName}
                  onChange={(event) => setRuleName(event.target.value)}
                  placeholder="例如：仅错误提醒"
                  className="ios-input mt-2 w-full rounded-md px-3 py-2.5 font-normal"
                />
              </label>
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="block text-sm font-bold text-gray-700">提醒次数（含首次）
                  <input type="number" min={1} max={10} step={1} value={ruleReminders} disabled={savingRule}
                    onChange={event=>setRuleReminders(Number(event.target.value))}
                    className="ios-input mt-2 w-full rounded-md px-3 py-2.5 font-normal" />
                </label>
                <label className="block text-sm font-bold text-gray-700">提醒间隔（分钟）
                  <input type="number" min={5} max={1440} step={1} value={ruleInterval} disabled={savingRule}
                    onChange={event=>setRuleInterval(Number(event.target.value))}
                    className="ios-input mt-2 w-full rounded-md px-3 py-2.5 font-normal" />
                </label>
              </div>
              <p className="text-xs leading-5 text-gray-500">每个事件独立计数，默认共2次、间隔5分钟。实际恢复或标记已处理后停止本轮提醒；新事件重新计数。发送结果不确定时不自动重发。</p>
              <div>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-sm font-bold text-gray-700">通知内容</span>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => applyRulePreset('all')}
                      className="rounded-md bg-gray-100 px-3 py-1.5 text-xs font-bold text-gray-700 hover:bg-gray-200"
                    >
                      全选
                    </button>
                    <button
                      type="button"
                      onClick={() => applyRulePreset('error')}
                      className="rounded-md bg-red-50 px-3 py-1.5 text-xs font-bold text-red-700 hover:bg-red-100"
                    >
                      仅错误
                    </button>
                    <button
                      type="button"
                      onClick={() => applyRulePreset('none')}
                      className="rounded-md bg-gray-100 px-3 py-1.5 text-xs font-bold text-gray-700 hover:bg-gray-200"
                    >
                      清空
                    </button>
                  </div>
                </div>
                <div className="mt-3 space-y-3">
                  {priorityGroups.map((group) => {
                    const groupEvents = eventDefinitions.filter((item) => item.priority === group.id);
                    if (groupEvents.length === 0) return null;
                    const groupColor = group.id === 'error'
                      ? 'text-red-700'
                      : group.id === 'warning'
                        ? 'text-amber-700'
                        : 'text-gray-600';
                    return (
                      <div key={group.id}>
                        <p className={`text-xs font-bold ${groupColor}`}>{group.label}</p>
                        <div className="mt-2 grid gap-2 sm:grid-cols-2">
                          {groupEvents.map((event) => {
                            const selected = ruleEventTypes.includes(event.id);
                            return (
                              <button
                                type="button"
                                key={event.id}
                                aria-pressed={selected}
                                onClick={() => toggleRuleEvent(event.id)}
                                className={`flex items-start gap-2 rounded-md border p-3 text-left transition-colors ${
                                  selected ? 'border-[var(--brand)] bg-[var(--brand-soft)]' : 'border-[var(--border)] bg-[var(--surface)] hover:bg-[var(--surface-hover)]'
                                }`}
                              >
                                <span className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border ${
                                  selected ? 'border-[#ffe100] bg-[#ffe100]' : 'border-gray-300 bg-white'
                                }`}>
                                  {selected && <Check className="h-3 w-3 text-gray-900" />}
                                </span>
                                <span className="min-w-0">
                                  <span className="block text-sm font-bold text-[var(--text)]">{event.label}</span>
                                  <span className="mt-0.5 block text-xs text-[var(--text-muted)]">{event.description}</span>
                                </span>
                              </button>
                            );
                          })}
                        </div>
                      </div>
                    );
                  })}
                </div>
                <p className="mt-3 text-xs text-gray-500">错误事件默认勾选；警告和通知默认关闭。未勾选的事件不会发邮件，短暂波动只在日志中记录。</p>
              </div>
            </div>
            <div className="modal-footer flex justify-end gap-3">
              <button
                type="button"
                onClick={() => setRuleEditorOpen(false)}
                className="ios-btn-secondary rounded-md px-4 py-2.5 text-sm"
              >
                取消
              </button>
              <button
                type="button"
                onClick={() => void saveRule()}
                disabled={savingRule}
                className="ios-btn-primary flex items-center gap-2 rounded-md px-4 py-2.5 text-sm disabled:opacity-50"
              >
                {savingRule ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                保存
              </button>
            </div>
          </div>
        </div>
      )}
      {diagnosticLog && createPortal(
        <div className="modal-overlay">
          <div ref={diagnosticDialog} role="dialog" aria-modal="true" aria-labelledby="browser-diagnostics-title"
            tabIndex={-1} className="modal-container modal-container-lg">
            <div className="modal-header flex items-center justify-between gap-4">
              <h3 id="browser-diagnostics-title" className="text-lg font-bold text-gray-900">浏览器诊断详情</h3>
              <button type="button" aria-label="关闭" onClick={() => setDiagnosticLog(null)} className="rounded-md p-2 hover:bg-gray-100"><X className="h-5 w-5" /></button>
            </div>
            <div className="modal-body"><BrowserDiagnostics diagnosticId={diagnosticLog.diagnostic_id} defaultOpen /></div>
            <div className="modal-footer flex justify-end"><button type="button" onClick={() => setDiagnosticLog(null)}
              className="ios-btn-secondary rounded-md px-4 py-2 text-sm">关闭</button></div>
          </div>
        </div>, document.body)}
    </div>
  );
};

export default NotificationsAndLogs;
