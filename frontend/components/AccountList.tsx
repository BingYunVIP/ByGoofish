import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { AccountDetail, AIReplySettings, AccountRecoveryStatus } from '../types';
import {
  getAccountDetails,
  updateAccountStatus,
  deleteAccount,
  generateQRLogin,
  checkQRLoginStatus,
  updateAccountRemark,
  updateAccountAutoConfirm,
  updateAccountPauseDuration,
  updateAccountCookie,
  updateAccountLoginInfo,
  updateAccountAISettings,
  getAllAISettings,
  getAccountAISettings,
  refreshAccountProfile,
  getRiskControlStatus,
  startManualCaptchaSession,
  importVerifiedWebSession,
  cancelManualCaptchaSession,
} from '../services/api';
import { confirmAction, notify } from '../services/feedback';
import BrowserDiagnostics from './BrowserDiagnostics';
import { useDialogFocus } from '../lib/useDialogFocus';
import { DEFAULT_AI_BASE_URL } from '../lib/aiEndpoint';
import { rotateAccountApiKey } from '../services/api';
import {Power, Edit2, Trash2, QrCode, X, Check, Loader2, MessageSquare, RefreshCw, Save, User, Clock, Key, Eye, EyeOff, Bot, Settings, MapPin, Users, ShieldCheck} from 'lucide-react';
import { EmptyState, PageHeader, PageLoading } from './ui';

type ModalType = 'edit' | 'ai-settings' | null;

const AccountList: React.FC = () => {
  const [accounts, setAccounts] = useState<AccountDetail[]>([]);
  const [loading, setLoading] = useState(true);
  // 风控熔断状态：命中后账号会暂停请求，需要让用户看到而不是只报 409
  const [riskStates, setRiskStates] = useState<AccountRecoveryStatus[]>([]);
  const [diagnosticAccount, setDiagnosticAccount] = useState<AccountRecoveryStatus | null>(null);
  const diagnosticDialog = useDialogFocus(Boolean(diagnosticAccount), () => setDiagnosticAccount(null));
  const riskBlocked = riskStates.filter(item => item.blocked || item.auth_paused || item.requires_action);
  const captchaRequestRef = useRef<{ accountId: string; requestId: string } | null>(null);
  const [showQRModal, setShowQRModal] = useState(false);
  const [qrCodeUrl, setQrCodeUrl] = useState<string>('');
  const [qrStatus, setQrStatus] = useState<string>('pending');
  const [qrRecoveryAccountId, setQrRecoveryAccountId] = useState('');
  const [qrRecoveryAction, setQrRecoveryAction] = useState('');
  const [captchaCheckOnly, setCaptchaCheckOnly] = useState(false);
  const [webSessionAccount, setWebSessionAccount] = useState<AccountDetail | null>(null);
  const [webSessionRequest, setWebSessionRequest] = useState('');
  const [importingWebSession, setImportingWebSession] = useState(false);
  const webSessionDialog = useDialogFocus(Boolean(webSessionAccount), () => { setWebSessionRequest(''); setWebSessionAccount(null); });
  const [qrMessage, setQrMessage] = useState<string>('');
  const [verificationQrUrl, setVerificationQrUrl] = useState<string>('');
  const [verificationUrl, setVerificationUrl] = useState<string>('');
  const qrPollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const qrSessionRef = useRef<string>('');
  const [activeModal, setActiveModal] = useState<ModalType>(null);
  const [oneTimeApiKey, setOneTimeApiKey] = useState('');
  useEffect(() => { if (activeModal !== 'edit') setOneTimeApiKey(''); }, [activeModal]);
  const editDialog = useDialogFocus(activeModal === 'edit', () => { setActiveModal(null); setOneTimeApiKey(''); });
  const [editingAccount, setEditingAccount] = useState<AccountDetail | null>(null);
  const [refreshingProfileId, setRefreshingProfileId] = useState<string | null>(null);
  // 正在取新验证链接的账号
  const [manualCaptchaId, setManualCaptchaId] = useState<string | null>(null);
  // 人工滑块验证弹窗：把服务器端浏览器的画面镜像到本页
  const [captchaAccount, setCaptchaAccount] = useState<AccountDetail | null>(null);
  const [captchaShot, setCaptchaShot] = useState('');
  const [captchaDiagnosticId, setCaptchaDiagnosticId] = useState<string | null>(null);
  const captchaTerminalRef = useRef(false);
  const [captchaStage, setCaptchaStage] = useState<'starting' | 'ready' | 'checking' | 'done' | 'failed'>('starting');
  const [captchaMessage, setCaptchaMessage] = useState('');
  const captchaWsRef = useRef<WebSocket | null>(null);
  const captchaImgRef = useRef<HTMLImageElement>(null);
  const [failedAvatars, setFailedAvatars] = useState<Set<string>>(new Set());

  // 编辑表单状态
  const [editForm, setEditForm] = useState({
    remark: '',
    cookie: '',
    auto_confirm: false,
    pause_duration: 0,
    username: '',
    login_password: '',
    show_browser: false,
    showLoginPassword: false,
  });

  // AI设置表单状态（字段必须齐全：PUT 是整行 INSERT OR REPLACE，
  // 少一个字段保存时就会被后端默认值静默覆盖）
  const [aiSettings, setAiSettings] = useState<AIReplySettings>({
    ai_enabled: false,
    model_name: 'qwen-plus',
    api_key: '',
    base_url: DEFAULT_AI_BASE_URL,
    user_agent: '',
    max_discount_percent: 10,
    max_discount_amount: 100,
    max_bargain_rounds: 3,
    context_enabled: true,
    context_message_limit: 12,
    context_expire_minutes: 120,
    custom_prompts: '',
  });
  const [saving, setSaving] = useState(false);

  const loadAccounts = async (options?: { silent?: boolean }) => {
    // 轮询刷新走静默模式，避免每 30 秒把整个列表闪成加载态
    if (!options?.silent) setLoading(true);
    try {
      const data = await getAccountDetails();

      // 获取所有账号的AI设置
      let allAISettings: Record<string, AIReplySettings> = {};
      try {
        allAISettings = await getAllAISettings();
      } catch (e) {
        console.error('Failed to load AI settings:', e);
      }

      // 合并AI设置到账号数据
      const accountsWithAI = data.map(account => ({
        ...account,
        ai_enabled: allAISettings[account.id]?.ai_enabled ?? false,
        max_discount_percent: allAISettings[account.id]?.max_discount_percent ?? 10,
        max_discount_amount: allAISettings[account.id]?.max_discount_amount ?? 100,
        max_bargain_rounds: allAISettings[account.id]?.max_bargain_rounds ?? 3,
        custom_prompts: allAISettings[account.id]?.custom_prompts ?? '',
      }));

      setAccounts(accountsWithAI);
    } catch (error) {
      console.error('Failed to load accounts:', error);
    } finally {
      if (!options?.silent) setLoading(false);
    }
  };

  useEffect(() => {
    const poll = () => {
      getRiskControlStatus()
        .then(res => {
          setRiskStates(res.accounts || []);
          const active = captchaRequestRef.current;
          const state = active && res.accounts.find(item => item.cookie_id === active.accountId);
          if (state?.diagnostic_id) setCaptchaDiagnosticId(state.diagnostic_id);
          if (state?.recovery_state === 'recovered') { setCaptchaStage('done'); setCaptchaMessage('令牌与消息连接已确认恢复'); }
          else if (state?.recovery_state === 'required' && !captchaTerminalRef.current) { setCaptchaStage('failed'); setCaptchaMessage(state.verification_message || state.recovery_message); }
        })
        .catch(() => {});
    };
    poll();
    const timer = setInterval(poll, 5000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    loadAccounts();
    // 账号的 runtime_state 会随后端连接情况变化（连上、断线重连、进出风控），
    // 只在挂载时拉一次会让页面一直停在旧快照上 —— 表现为账号已在正常收发心跳，
    // 界面却仍显示「未运行」且状态点是灰的。与风控轮询保持同一节奏。
    const timer = setInterval(() => {
      loadAccounts({ silent: true });
    }, 30000);
    return () => {
      clearInterval(timer);
      qrSessionRef.current = '';
      if (qrPollTimerRef.current) clearTimeout(qrPollTimerRef.current);
    };
  }, []);

  const handleToggle = async (id: string, currentStatus: boolean) => {
    await updateAccountStatus(id, !currentStatus);
    loadAccounts();
  };

  const handleRefreshProfile = async (id: string) => {
    setRefreshingProfileId(id);
    try {
      await refreshAccountProfile(id);
      setFailedAvatars(previous => {
        const next = new Set(previous);
        next.delete(id);
        return next;
      });
      await loadAccounts();
    } catch (error) {
      console.error('Failed to refresh account profile:', error);
      notify(error instanceof Error ? error.message : '账号资料刷新失败');
    } finally {
      setRefreshingProfileId(null);
    }
  };

  const handleDelete = async (id: string) => {
    if (await confirmAction('确认删除该账号吗？')) {
      await deleteAccount(id);
      loadAccounts();
    }
  };

  // 人工滑块验证：在本页开弹窗，通过 WebSocket 把服务器端浏览器的截图推过来，
  // 鼠标事件再回传驱动服务器上的真实浏览器 —— 相当于把远端浏览器镜像到这里。
  // 这样部署在没有桌面的服务器上也能人工过验证，不需要访问服务器屏幕。
  const closeManualCaptcha = () => {
    const active = captchaRequestRef.current;
    captchaRequestRef.current = null;
    if (active) {
      void cancelManualCaptchaSession(active.accountId, active.requestId)
        .catch(() => notify('取消请求未确认，请稍后查看账号状态', 'error'));
    }
    setCaptchaAccount(null);
    setManualCaptchaId(null);
  };

  useEffect(() => () => {
    const active = captchaRequestRef.current;
    captchaRequestRef.current = null;
    if (active) void cancelManualCaptchaSession(active.accountId, active.requestId).catch(() => {});
  }, []);

  const handleManualCaptcha = async (account: AccountDetail, checkOnly = false) => {
    if (captchaRequestRef.current) return;
    const requestId = typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
    captchaRequestRef.current = { accountId: account.id, requestId };
    setCaptchaCheckOnly(checkOnly);
    setCaptchaAccount(account);
    setCaptchaShot('');
    setCaptchaDiagnosticId(null);
    captchaTerminalRef.current = false;
    setCaptchaStage('starting');
    setCaptchaMessage(checkOnly ? '正在检查已保存凭证和消息连接，不会刷新验证码页面…' : '正在服务器上启动验证页面，请稍候…');
    setManualCaptchaId(account.id);
    try {
      const result = await startManualCaptchaSession(account.id, 300, requestId, checkOnly);
      if (captchaRequestRef.current?.requestId !== requestId) return;
      captchaTerminalRef.current = true;
      setCaptchaDiagnosticId(result.diagnostic_id || null);
      if (result.last_screenshot) setCaptchaShot(`data:image/jpeg;base64,${result.last_screenshot}`);
      captchaWsRef.current?.close();
      if (!result.success) throw new Error(result.message || '人工验证未完成');
      setCaptchaStage(result.recovery_state === 'checking' ? 'checking' : 'failed');
      setCaptchaMessage(result.message);
      notify(result.message, 'info');
      const status = await getRiskControlStatus();
      setRiskStates(status.accounts || []);
      await loadAccounts({ silent: true });
    } catch (error) {
      if (captchaRequestRef.current?.requestId !== requestId) return;
      captchaTerminalRef.current = true;
      const msg = error instanceof Error ? error.message : '人工验证启动失败';
      setCaptchaStage('failed');
      setCaptchaMessage(msg);
      notify(msg, 'error');
    } finally {
      if (captchaRequestRef.current?.requestId === requestId) setManualCaptchaId(null);
    }
  };

  useEffect(() => {
    if (!captchaAccount) return;
    let closed = false;
    let timer: ReturnType<typeof setTimeout>;
    const pollDiagnosticId = async () => {
      try {
        const result = await getRiskControlStatus();
        const state = result.accounts.find(item => item.cookie_id === captchaAccount.id);
        if (!closed && state?.diagnostic_id) setCaptchaDiagnosticId(state.diagnostic_id);
      } catch { /* 日志查询错误由日志区域提示。 */ }
      if (!closed) timer = setTimeout(pollDiagnosticId, 1000);
    };
    void pollDiagnosticId();
    return () => { closed = true; clearTimeout(timer); };
  }, [captchaAccount?.id]);

  const captchaDialog = useDialogFocus(Boolean(captchaAccount), closeManualCaptcha);

  // 弹窗打开后连上服务器端会话，持续接收截图；关闭时断开
  useEffect(() => {
    if (!captchaAccount || captchaCheckOnly) {
      captchaWsRef.current?.close();
      captchaWsRef.current = null;
      return;
    }

    let closed = false;
    let retry = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      if (closed || captchaTerminalRef.current) return;
      const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(
        `${scheme}://${location.host}/api/captcha/ws/${encodeURIComponent(captchaAccount.id)}`,
        ['xianyu-auth', localStorage.getItem('auth_token') || '']
      );
      captchaWsRef.current = ws;

      ws.onmessage = event => {
        let data: any;
        try {
          data = JSON.parse(event.data);
        } catch {
          return;
        }
        if (captchaTerminalRef.current) return;
        if (data.type === 'session_info' || data.type === 'screenshot_update') {
          if (data.screenshot) {
            setCaptchaShot(
              String(data.screenshot).startsWith('data:')
                ? data.screenshot
                : `data:image/jpeg;base64,${data.screenshot}`
            );
            setCaptchaStage('ready');
            setCaptchaMessage('按住滑块向右拖动完成验证');
          }
        } else if (data.type === 'completed') {
          closed = true;
          setCaptchaStage('checking');
          setCaptchaMessage('验证页面已完成，等待检查令牌与消息连接…');
        } else if (data.type === 'error') {
          // 会话尚未建立时后端会立刻返回 error，稍后重试即可
          if (retry < 20) {
            retry += 1;
            timer = setTimeout(connect, 1000);
          }
        }
      };

      ws.onclose = () => {
        if (!closed && retry < 20) {
          retry += 1;
          timer = setTimeout(connect, 1000);
        }
      };
    };

    connect();
    return () => {
      closed = true;
      if (timer) clearTimeout(timer);
      captchaWsRef.current?.close();
      captchaWsRef.current = null;
    };
  }, [captchaCheckOnly, captchaAccount]);

  // 把本页的鼠标坐标换算成服务器端浏览器的坐标后回传
  const sendCaptchaMouse = (eventType: 'down' | 'move' | 'up', e: React.MouseEvent) => {
    const ws = captchaWsRef.current;
    const img = captchaImgRef.current;
    if (captchaStage !== 'ready' || captchaTerminalRef.current || !ws || ws.readyState !== WebSocket.OPEN || !img || !img.naturalWidth) return;

    const rect = img.getBoundingClientRect();
    // 截图在页面上被等比缩放显示，坐标要按缩放比还原回原始分辨率
    const x = (e.clientX - rect.left) * (img.naturalWidth / rect.width);
    const y = (e.clientY - rect.top) * (img.naturalHeight / rect.height);
    ws.send(JSON.stringify({
      type: 'mouse_event',
      event_type: eventType,
      coordinate_space: 'screenshot',
      x: Math.round(x),
      y: Math.round(y),
    }));
  };

  const openEditModal = (account: AccountDetail) => {
    setOneTimeApiKey('');
    setEditingAccount(account);
    setEditForm({
      remark: account.remark || account.note || '',
      cookie: account.cookie || account.value || '',
      auto_confirm: account.auto_confirm || false,
      pause_duration: account.pause_duration || 0,
      username: account.username || '',
      login_password: account.login_password || '',
      show_browser: account.show_browser || false,
      showLoginPassword: false,
    });
    setActiveModal('edit');
  };

  const openAIModal = async (account: AccountDetail) => {
    setOneTimeApiKey('');
    setEditingAccount(account);
    setSaving(true);
    try {
      const settings = await getAccountAISettings(account.id);
      // 全量加载：PUT 是整行覆盖保存，漏掉的字段（user_agent、
      // context_* 等）会被后端默认值静默重置
      setAiSettings({
        ai_enabled: settings.ai_enabled ?? false,
        model_name: settings.model_name || 'qwen-plus',
        api_key: settings.api_key || '',
        base_url: settings.base_url || DEFAULT_AI_BASE_URL,
        user_agent: settings.user_agent ?? '',
        max_discount_percent: settings.max_discount_percent ?? 10,
        max_discount_amount: settings.max_discount_amount ?? 100,
        max_bargain_rounds: settings.max_bargain_rounds ?? 3,
        context_enabled: settings.context_enabled ?? true,
        context_message_limit: settings.context_message_limit ?? 12,
        context_expire_minutes: settings.context_expire_minutes ?? 120,
        custom_prompts: settings.custom_prompts ?? '',
      });
    } catch (e) {
      console.error('Failed to load AI settings:', e);
    } finally {
      setSaving(false);
    }
    setActiveModal('ai-settings');
  };

  const handleSaveEdit = async () => {
    if (!editingAccount) return;
    setSaving(true);

    try {
      const promises: Promise<any>[] = [];

      // 更新备注
      if (editForm.remark !== (editingAccount.remark || editingAccount.note || '')) {
        promises.push(updateAccountRemark(editingAccount.id, editForm.remark));
      }

      // 更新Cookie
      if (editForm.cookie && editForm.cookie !== (editingAccount.cookie || editingAccount.value || '')) {
        promises.push(updateAccountCookie(editingAccount.id, editForm.cookie));
      }

      // 更新自动确认
      if (editForm.auto_confirm !== editingAccount.auto_confirm) {
        promises.push(updateAccountAutoConfirm(editingAccount.id, editForm.auto_confirm));
      }

      // 更新暂停时长
      if (editForm.pause_duration !== (editingAccount.pause_duration || 0)) {
        promises.push(updateAccountPauseDuration(editingAccount.id, editForm.pause_duration));
      }

      // 更新登录信息
      if (
        editForm.username !== (editingAccount.username || '') ||
        editForm.login_password !== (editingAccount.login_password || '') ||
        editForm.show_browser !== (editingAccount.show_browser || false)
      ) {
        promises.push(updateAccountLoginInfo(editingAccount.id, {
          username: editForm.username,
          login_password: editForm.login_password,
          show_browser: editForm.show_browser,
        }));
      }

      await Promise.all(promises);
      setActiveModal(null);
      loadAccounts();
    } catch (error) {
      console.error('更新账号失败:', error);
      notify('更新失败，请重试');
    } finally {
      setSaving(false);
    }
  };

  const handleSaveAISettings = async () => {
    if (!editingAccount) return;
    setSaving(true);

    try {
      await updateAccountAISettings(editingAccount.id, aiSettings);
      setActiveModal(null);
      loadAccounts();
    } catch (error) {
      console.error('更新AI设置失败:', error);
      notify('更新失败，请重试');
    } finally {
      setSaving(false);
    }
  };

  const startQRLogin = async () => {
    qrSessionRef.current = '';
    if (qrPollTimerRef.current) clearTimeout(qrPollTimerRef.current);
    setShowQRModal(true);
    setQrStatus('loading');
    setQrMessage('');
    setQrRecoveryAccountId('');
    setQrRecoveryAction('');
    setVerificationQrUrl('');
    setVerificationUrl('');
    try {
      const res = await generateQRLogin();
      if (res.success && res.qr_code_url && res.session_id) {
        setQrCodeUrl(res.qr_code_url);
        setQrStatus('waiting');
        setQrMessage('等待扫码');
        qrSessionRef.current = res.session_id;

        const pollStatus = async () => {
          if (qrSessionRef.current !== res.session_id) return;
          try {
            const statusRes = await checkQRLoginStatus(res.session_id!);
            if (qrSessionRef.current !== res.session_id) return;

            if (statusRes.status === 'success') {
              qrSessionRef.current = '';
              if (statusRes.account_ready) {
                setQrStatus('success');
                setQrMessage(statusRes.message || '账号 Cookie 已刷新并保存');
                setTimeout(() => {
                  setShowQRModal(false);
                  loadAccounts();
                }, 1000);
              } else {
                setQrStatus('error');
                setQrMessage(statusRes.message || '扫码已确认，但账号 Cookie 未准备完成');
                loadAccounts();
              }
              return;
            }

            if (statusRes.status === 'login_verification_required' || statusRes.status === 'message_verification_required' || statusRes.status === 'auth_error') {
              qrSessionRef.current = '';
              setQrStatus(statusRes.status);
              setQrRecoveryAccountId(statusRes.account_info?.account_id || '');
              setQrRecoveryAction(statusRes.recovery_action || 'none');
              setQrMessage(statusRes.message || '扫码凭证已保存，但后续认证检查未完成');
              void loadAccounts({ silent: true });
              void getRiskControlStatus().then(status => setRiskStates(status.accounts || []));
              return;
            }

            if (statusRes.status === 'scanned') {
              setQrStatus('scanned');
              setQrMessage('已扫码，请在手机上确认登录');
            } else if (statusRes.status === 'processing') {
              setQrStatus('processing');
              setQrMessage(statusRes.message || '已确认，正在准备账号...');
            } else if (
              statusRes.status === 'expired' ||
              statusRes.status === 'error' ||
              statusRes.status === 'cancelled' ||
              statusRes.status === 'not_found'
            ) {
              qrSessionRef.current = '';
              setQrStatus('error');
              setQrMessage(statusRes.message || '二维码已失效，请重试');
              return;
            } else if (statusRes.status === 'verification_required') {
              // 不能在这里停止轮询：用户正要去手机上完成人脸验证，
              // 验证走完后状态才会变成 success。原实现清掉 session 直接 return，
              // 于是验证做完页面永远停在「请完成验证」，只能关掉重来。
              setQrStatus('verification_required');
              setQrMessage(statusRes.message || '请使用手机扫描二维码完成安全验证');
              if (statusRes.verification_qr_code_url) {
                setVerificationQrUrl(statusRes.verification_qr_code_url);
              }
              if (statusRes.verification_url) {
                setVerificationUrl(statusRes.verification_url);
              }
              // 验证要花上一两分钟，放慢轮询节奏即可
              qrPollTimerRef.current = setTimeout(pollStatus, 2000);
              return;
            }

            qrPollTimerRef.current = setTimeout(pollStatus, 800);
          } catch (error) {
            qrSessionRef.current = '';
            setQrStatus('error');
            setQrMessage(error instanceof Error ? error.message : '扫码状态查询失败');
          }
        };

        qrPollTimerRef.current = setTimeout(pollStatus, 300);
      } else {
        setQrStatus('error');
        setQrMessage(res.message || '二维码生成失败，请重试');
      }
    } catch (e) {
      setQrStatus('error');
      setQrMessage(e instanceof Error ? e.message : '扫码登录请求失败');
    }
  };

  const closeQRModal = () => {
    qrSessionRef.current = '';
    if (qrPollTimerRef.current) clearTimeout(qrPollTimerRef.current);
    setShowQRModal(false);
  };

  const getRuntimeBadge = (account: AccountDetail) => {
    if (!account.enabled) {
      return { label: '已暂停', className: 'bg-gray-100 text-gray-500' };
    }
    const recovery = riskStates.find(item => item.cookie_id === account.id);
    if (recovery?.auth_paused || recovery?.requires_action) {
      return { label: recovery.recovery_message, className: recovery.severity === 'error' ? 'bg-red-100 text-red-700' : 'bg-amber-100 text-amber-800' };
    }
    if (account.runtime_state === 'running') {
      return { label: '后台消息监听中', className: 'bg-green-100 text-green-700' };
    }
    // 登录态过期是终态：等待和过验证都没用，必须重新扫码。
    // 与「连接中」分开显示，否则用户会一直等一个不会变好的状态。
    if (account.runtime_state === 'need_relogin') {
      return { label: '需重新扫码', className: 'bg-red-100 text-red-700' };
    }
    if (account.runtime_state === 'failed') {
      return { label: '监听异常', className: 'bg-red-100 text-red-700' };
    }
    // 反复重连通常意味着账号被风控拦住：Token 取不到，消息、发货、自动回复
    // 实际都不通。必须与「监听中」区分开，否则界面会给出一个假的健康状态。
    if (account.runtime_state === 'connecting') {
      return { label: '连接中 / 重连', className: 'bg-amber-100 text-amber-800' };
    }
    return { label: '已启用 / 未运行', className: 'bg-amber-100 text-amber-800' };
  };

  if (loading) return <PageLoading label="正在加载账号和运行状态" />;

  return (
    <div className="page-stack animate-fade-in relative">
      {riskBlocked.length > 0 && (
        <div role="status" aria-live="polite" className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3">
          <p className="text-sm font-bold text-amber-900">
            账号状态需要关注：{riskBlocked.length} 个账号
          </p>
          <p className="mt-1 text-xs leading-5 text-amber-800">
            {riskBlocked.map(item => `${item.cookie_id}：${item.event_label || '平台暂时退避'}`).join('；')}。
            {' '}请按对应账号卡片的说明操作。登录凭证保存、消息安全验证和消息连接恢复是不同阶段。
          </p>
        </div>
      )}

      <PageHeader
        title="账号管理"
        description="管理闲鱼账号授权、监听状态、基础资料和账号级回复策略。"
        icon={Users}
        badge={<span className="status-badge status-badge-info">{accounts.length} 个账号</span>}
        actions={(
          <button
            onClick={startQRLogin}
            className="ios-btn-primary flex items-center justify-center gap-2 rounded-md px-4 py-2 text-sm"
          >
            <QrCode className="h-4 w-4" />
            扫码添加账号
          </button>
        )}
      />

      <p className="text-xs leading-5 text-gray-400">
        提示：挂 VPN、代理或部署在海外服务器时，闲鱼风控会明显收紧，滑块和人脸验证的通过率大幅下降，建议使用国内网络环境登录与运行。
      </p>
      <p className="text-xs leading-5 text-gray-500">
        此处状态表示后台消息服务连接与监听情况，不代表买家端显示在线。标有“手动测试”的通知只用于验证投递，不表示账号发生异常，也不会改变这里的状态。
      </p>

      {/* Account Grid */}
      <div className="grid grid-cols-1 gap-3">
        {accounts.map((account) => {
          const runtimeBadge = getRuntimeBadge(account);
          const isListening = account.enabled && account.runtime_state === 'running' && !riskStates.find(item => item.cookie_id === account.id)?.auth_paused;
          const blockedState = riskBlocked.find(item => item.cookie_id === account.id);
          const recoveryState = riskStates.find(item => item.cookie_id === account.id);
          return (
          <article key={account.id} className="ios-card flex flex-col gap-4 p-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex min-w-0 items-start gap-4 sm:items-center">
              <div className="relative">
                {account.avatar_url && !failedAvatars.has(account.avatar_url) ? (
                  <img
                    src={account.avatar_url.startsWith('//') ? `https:${account.avatar_url}` : account.avatar_url}
                    alt=""
                    className="h-14 w-14 rounded-md object-cover"
                    referrerPolicy="no-referrer"
                    loading="lazy"
                    // 按图片地址记录失败，而不是按账号 —— 否则头像换了新地址
                    // 也会一直显示首字母占位，只能靠手动刷新绕过
                    onError={() => setFailedAvatars(previous => new Set(previous).add(account.avatar_url!))}
                  />
                ) : (
                  <div className="flex h-14 w-14 items-center justify-center rounded-md bg-yellow-100 text-lg font-bold text-yellow-800">
                    {account.nickname?.trim().charAt(0) || account.remark?.trim().charAt(0) || '闲'}
                  </div>
                )}
                <div className={`absolute -bottom-1 -right-1 w-6 h-6 rounded-full border-4 border-white flex items-center justify-center ${isListening ? 'bg-green-500' : account.runtime_state === 'failed' ? 'bg-red-500' : 'bg-gray-300'}`}>
                    {isListening && <Check className="w-3 h-3 text-white" />}
                </div>
              </div>
              <div className="min-w-0">
                <div className="mb-1 flex flex-wrap items-center gap-2">
                    <h3 className="break-words text-lg font-bold text-gray-900">{account.nickname || account.remark || '未命名账号'}</h3>
                    {account.remark?.trim() && (
                      <span className="status-badge status-badge-info max-w-full whitespace-normal break-words" title={`备注：${account.remark.trim()}`}>
                        备注：{account.remark.trim()}
                      </span>
                    )}
                    <span className={`status-badge ${runtimeBadge.className}`}>
                      {runtimeBadge.label}
                    </span>
                    {recoveryState?.recovery_state === 'recovered' && account.runtime_state === 'running' && (
                      <span className={`status-badge ${recoveryState.recovery_state === 'recovered' ? 'status-badge-success' : 'status-badge-warning'}`}>
                        {recoveryState.recovery_message}
                      </span>
                    )}
                    {account.ai_enabled && (
                        <span className="status-badge status-badge-warning flex items-center gap-1">
                          <Bot className="w-3 h-3" /> AI
                        </span>
                    )}
                </div>
                <p className="break-all font-mono text-xs text-gray-500">闲鱼 ID {account.id}</p>
                <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-gray-600">
                  {account.location && (
                    <span className="flex items-center gap-1.5">
                      <MapPin className="h-4 w-4 text-gray-400" />
                      {account.location}
                    </span>
                  )}
                  {(account.followers !== undefined || account.following !== undefined) && (
                    <span className="flex items-center gap-1.5">
                      <Users className="h-4 w-4 text-gray-400" />
                      {account.followers?.toLocaleString() ?? '--'} 粉丝
                      <span className="text-gray-400">/</span>
                      {account.following?.toLocaleString() ?? '--'} 关注
                    </span>
                  )}
                </div>
                {account.bio && (
                  <p className="mt-2 max-w-3xl break-words text-sm leading-6 text-gray-600">{account.bio}</p>
                )}
                <div className="flex flex-wrap gap-2">
                   {blockedState?.blocked && !blockedState.auth_paused && <span className="status-badge bg-amber-100 text-amber-800">平台暂时退避 · 剩余 {blockedState.remaining_seconds} 秒</span>}
                   {account.auto_confirm && <span className="status-badge status-badge-warning flex items-center gap-1.5"><MessageSquare className="w-3 h-3"/> 自动确认</span>}
                   {account.pause_duration > 0 && <span className="status-badge status-badge-info flex items-center gap-1.5"><Clock className="w-3 h-3"/> 暂停 {account.pause_duration} 分钟</span>}
                </div>
                {recoveryState?.requires_action && (
                  <div role="alert" className={`mt-2 max-w-3xl rounded-md border px-3 py-2 text-xs leading-5 ${recoveryState.severity === 'error' ? 'border-red-200 bg-red-50 text-red-800' : 'border-amber-200 bg-amber-50 text-amber-900'}`}>
                    <p className="font-bold">{recoveryState.event_label || '认证问题'} · {recoveryState.severity === 'error' ? 'Error' : 'Warn'}</p>
                    {recoveryState.event_impact && <p className="mt-1">影响：{recoveryState.event_impact}</p>}
                    {recoveryState.recovery_detail && <p className="mt-1">原因：{recoveryState.recovery_detail}</p>}
                    {recoveryState.recommended_action && <p className="mt-1">处理：{recoveryState.recommended_action}</p>}
                  </div>
                )}

              </div>
            </div>
            <div className="flex flex-wrap items-center justify-end gap-1 border-t border-gray-100 pt-3 sm:border-0 sm:pt-0">
                {(recoveryState?.auth_paused || recoveryState?.requires_action) && <button type="button"
                  onClick={() => setDiagnosticAccount(recoveryState)} className="ios-btn-secondary rounded-md px-3 py-2 text-xs">查看诊断</button>}
                {recoveryState?.action_codes.includes('sync_web_session') && <button type="button"
                  className="ios-btn-secondary rounded-md px-3 py-2 text-xs"
                  onClick={() => { setWebSessionRequest(''); setWebSessionAccount(account); }}>
                  同步网页登录态
                </button>}
                {recoveryState?.action_codes.includes('qr_login') && <button type="button"
                  onClick={startQRLogin}
                  className="ios-btn-secondary flex shrink-0 items-center gap-1.5 rounded-md px-3 py-2 text-xs font-bold">
                  <QrCode className="h-4 w-4" />使用同一账号重新扫码
                </button>}
                {recoveryState?.can_check_connection && <button type="button"
                    onClick={() => void handleManualCaptcha(account, true)}
                    disabled={manualCaptchaId !== null}
                    className="ios-btn-primary flex shrink-0 items-center gap-1.5 rounded-md px-3 py-2 text-xs font-bold disabled:opacity-50">
                    {manualCaptchaId === account.id
                      ? <Loader2 className="h-4 w-4 animate-spin" />
                      : <RefreshCw className="h-4 w-4" />}
                    <span>检查连接</span>
                </button>}
                {recoveryState?.action_codes.includes('edit_credentials') && <button type="button"
                  onClick={() => openEditModal(account)} className="ios-btn-secondary rounded-md px-3 py-2 text-xs">编辑账号凭证</button>}
                <button
                    onClick={() => handleRefreshProfile(account.id)}
                    disabled={refreshingProfileId === account.id}
                    className="rounded-md p-2.5 text-amber-700 hover:bg-amber-50 disabled:cursor-wait disabled:opacity-50"
                    title="刷新闲鱼资料"
                    aria-label="刷新闲鱼资料"
                >
                    <RefreshCw className={`w-5 h-5 ${refreshingProfileId === account.id ? 'animate-spin' : ''}`} />
                </button>
                <button
                    onClick={() => openEditModal(account)}
                    className="rounded-md p-2.5 text-gray-600 hover:bg-gray-100"
                    title="编辑账号"
                >
                    <Edit2 className="w-5 h-5" />
                </button>
                <button
                    onClick={() => openAIModal(account)}
                    className="rounded-md p-2.5 text-amber-700 hover:bg-amber-50"
                    title="AI设置"
                >
                    <Bot className="w-5 h-5" />
                </button>
                <button
                    onClick={() => handleToggle(account.id, account.enabled)}
                    className={`rounded-md p-2.5 ${account.enabled ? 'text-green-600 hover:bg-green-50' : 'text-gray-400 hover:bg-gray-100'}`}
                    title={account.enabled ? '暂停账号' : '启用账号'}
                    aria-label={account.enabled ? '暂停账号' : '启用账号'}
                >
                    <Power className="w-5 h-5" />
                </button>
                <button
                    onClick={() => handleDelete(account.id)}
                    className="rounded-md p-2.5 text-red-500 hover:bg-red-100"
                    title="删除账号"
                    aria-label="删除账号"
                >
                    <Trash2 className="w-5 h-5" />
                </button>
            </div>
          </article>
          );
        })}

        {accounts.length === 0 && (
            <EmptyState
              title="暂无闲鱼账号"
              description="扫码登录后，系统会保存授权并开始获取账号资料、商品、消息和订单状态。"
              icon={User}
              action={(
                <button type="button" onClick={startQRLogin} className="ios-btn-primary rounded-md px-4 py-2 text-sm">
                  扫码添加账号
                </button>
              )}
            />
        )}
      </div>

      {webSessionAccount && createPortal(
        <div className="modal-overlay">
          <div ref={webSessionDialog} role="dialog" aria-modal="true" aria-labelledby="web-session-title" className="modal-container max-w-2xl">
            <div className="modal-header flex items-start justify-between gap-3">
              <div><h3 id="web-session-title" className="text-lg font-bold text-gray-900">同步已验证的网页登录态</h3>
                <p className="mt-1 text-sm text-gray-500">{webSessionAccount.nickname || webSessionAccount.id}</p></div>
              <button type="button" className="rounded-md p-2 hover:bg-gray-100" aria-label="关闭" onClick={() => { setWebSessionRequest(''); setWebSessionAccount(null); }}><X className="h-5 w-5" /></button>
            </div>
            <div className="modal-body space-y-4">
              <ol className="list-decimal space-y-2 pl-5 text-sm leading-6 text-gray-600">
                <li>在闲鱼官网完成消息页安全验证，确认已经看到会话列表。</li>
                <li>按 F12 打开开发者工具的「网络」，刷新消息页，优先搜索 <code>login.token</code>；若未出现，可选择其它 <code>h5api.m.goofish.com</code> 接口请求。</li>
                <li>右键请求，选择「复制」→「复制为 cURL（bash）」，粘贴到下方。</li>
              </ol>
              <label className="block text-sm font-bold text-gray-700">官网消息请求
                <textarea value={webSessionRequest} onChange={e => setWebSessionRequest(e.target.value)}
                  aria-label="官网消息请求" autoComplete="off" spellCheck={false}
                  placeholder="粘贴从官网复制的 cURL（bash）请求"
                  className="ios-input mt-2 min-h-40 w-full resize-y rounded-md p-3 font-mono text-xs font-normal" />
              </label>
              <p className="text-xs leading-5 text-gray-500">会同步该账号的 Cookie，请求包含设备标识时会一并同步。请仅粘贴到此后台，勿发到聊天中；原始请求不写入日志。</p>
            </div>
            <div className="modal-footer flex justify-end gap-3">
              <button type="button" className="ios-btn-secondary rounded-md px-4 py-2 text-sm" onClick={() => { setWebSessionRequest(''); setWebSessionAccount(null); }}>取消</button>
              <button type="button" disabled={importingWebSession || !webSessionRequest.trim()}
                className="ios-btn-primary flex items-center gap-2 rounded-md px-4 py-2 text-sm disabled:opacity-60"
                onClick={async () => {
                  const account = webSessionAccount;
                  setImportingWebSession(true);
                  try {
                    const result = await importVerifiedWebSession(account.id, webSessionRequest);
                    setWebSessionRequest(''); setWebSessionAccount(null);
                    notify(result.message, 'info');
                    void loadAccounts({ silent: true });
                    const status = await getRiskControlStatus(); setRiskStates(status.accounts || []);
                  } catch (error) { notify(error instanceof Error ? error.message : '同步网页登录态失败', 'error'); }
                  finally { setImportingWebSession(false); }
                }}>
                {importingWebSession && <Loader2 className="h-4 w-4 animate-spin" />}同步并检查连接
              </button>
            </div>
          </div>
        </div>, document.body
      )}

      {/* QR Code Modal */}
      {showQRModal && createPortal(
          <div className="modal-overlay">
              <div className="modal-container" style={{maxWidth: '24rem'}}>
                  <div className="modal-header flex items-start justify-between gap-4">
                    <div>
                      <h3 className="text-lg font-bold text-gray-900">扫码登录</h3>
                      <p className="mt-1 text-xs text-gray-500">使用闲鱼 App 扫码并在手机端确认。</p>
                    </div>
                    <button
                      type="button"
                      onClick={closeQRModal}
                      className="rounded-md p-2 hover:bg-gray-100"
                      aria-label="关闭扫码登录"
                    >
                      <X className="h-5 w-5 text-gray-600" />
                    </button>
                  </div>

                  <div className="modal-body py-5">
                      <div className="text-center">
                          <div className="media-light-surface relative mx-auto flex h-60 w-60 items-center justify-center overflow-hidden rounded-md border border-gray-200 bg-gray-50">
                              {qrStatus === 'loading' && <Loader2 className="h-9 w-9 animate-spin text-amber-500" />}
                              {(qrStatus === 'waiting' || qrStatus === 'scanned') && (
                                  <img src={qrCodeUrl} alt="闲鱼登录二维码" className="h-full w-full p-2" />
                              )}
                              {qrStatus === 'scanned' && (
                                  <div className="absolute inset-x-3 bottom-3 border border-gray-200 bg-white/95 px-3 py-2 text-sm font-semibold text-gray-800">
                                      已扫码，请在手机确认
                                  </div>
                              )}
                              {qrStatus === 'processing' && (
                                  <div className="absolute inset-0 flex flex-col items-center justify-center bg-white/95 text-gray-800">
                                      <Loader2 className="mb-4 h-9 w-9 animate-spin text-amber-500" />
                                      <span className="font-bold">正在准备账号</span>
                                      <span className="mt-2 text-xs text-gray-500">无需重复扫码</span>
                                  </div>
                              )}
                              {qrStatus === 'success' && (
                                  <div className="absolute inset-0 flex animate-fade-in flex-col items-center justify-center bg-white/95 text-green-700">
                                      <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-md bg-green-50">
                                         <Check className="h-7 w-7" />
                                      </div>
                                      <span className="text-base font-bold">登录成功</span>
                                  </div>
                              )}
                              {qrStatus === 'verification_required' && (
                                  <div className="flex h-full w-full flex-col items-center justify-center bg-white p-2 text-center">
                                      {verificationQrUrl ? (
                                        <img src={verificationQrUrl} alt="闲鱼人脸验证二维码" className="h-44 w-44" />
                                      ) : (
                                        <ShieldCheck className="mb-3 h-12 w-12 text-amber-600" />
                                      )}
                                      <span className="mt-1 text-sm font-bold text-amber-800">手机扫码完成人脸验证</span>
                                      <span className="mt-1 text-xs text-gray-500">验证通过后本页会自动继续，请勿关闭</span>
                                      {verificationUrl && (
                                        <a href={verificationUrl} target="_blank" rel="noreferrer" className="mt-2 text-xs text-blue-600 underline">在当前设备打开验证页</a>
                                      )}
                                  </div>
                              )}
                              {(qrStatus === 'login_verification_required' || qrStatus === 'message_verification_required' || qrStatus === 'auth_error') && (
                                <div className="flex flex-col items-center gap-3 px-4 text-center">
                                  <ShieldCheck className={`h-9 w-9 ${qrStatus === 'auth_error' ? 'text-red-600' : 'text-amber-600'}`} />
                                  <span className="font-bold text-gray-900">{qrStatus === 'login_verification_required' ? '登录安全验证待完成' : qrStatus === 'message_verification_required' ? '消息安全验证待完成' : '账号认证检查未完成'}</span>
                                  <span className="text-xs leading-5 text-gray-500">{qrMessage}</span>
                                  {qrRecoveryAction === 'sync_web_session' && <button type="button" disabled={!accounts.some(item => item.id === qrRecoveryAccountId)} className="ios-btn-primary rounded-md px-3 py-2 text-xs disabled:opacity-60"
                                    onClick={() => {
                                      const account = accounts.find(item => item.id === qrRecoveryAccountId);
                                      setShowQRModal(false);
                                      if (account) { setWebSessionRequest(''); setWebSessionAccount(account); }
                                    }}>同步已验证的网页登录态</button>}
                                  {qrRecoveryAction === 'qr_login' && <button type="button" className="ios-btn-secondary rounded-md px-3 py-2 text-xs" onClick={startQRLogin}>重新扫码登录</button>}
                                  {qrStatus === 'message_verification_required' && <button type="button" disabled={!accounts.some(item => item.id === qrRecoveryAccountId)} className="ios-btn-secondary rounded-md px-3 py-2 text-xs disabled:opacity-60"
                                    onClick={() => {
                                      const account = accounts.find(item => item.id === qrRecoveryAccountId);
                                      setShowQRModal(false);
                                      if (account) void handleManualCaptcha(account, true);
                                    }}>验证完成后检查连接</button>}
                                  <button type="button" className="text-xs text-gray-500" onClick={() => setShowQRModal(false)}>返回账号管理</button>
                                </div>
                              )}
                              {qrStatus === 'error' && (
                                  <div className="flex flex-col items-center px-4 text-center">
                                      <span className="mb-2 font-bold text-red-600">账号未登录完成</span>
                                      {qrMessage && <span className="mb-3 text-xs text-gray-500">{qrMessage}</span>}
                                      <button
                                        type="button"
                                        onClick={startQRLogin}
                                        className="ios-btn-secondary flex items-center gap-1.5 rounded-md px-3 py-2 text-xs"
                                      >
                                        <RefreshCw className="h-3.5 w-3.5" />
                                        重新生成
                                      </button>
                                  </div>
                              )}
                          </div>

                          <p role="status" aria-live="polite" className="mt-4 rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-xs font-medium text-gray-500">
                            {qrMessage || '二维码有效期为5分钟，请尽快扫码。'}
                          </p>
                      </div>
                  </div>
              </div>
          </div>,
          document.body
      )}

      {/* 编辑账号弹窗 */}
      {activeModal === 'edit' && editingAccount && createPortal(
        <div className="modal-overlay">
          <div ref={editDialog} role="dialog" aria-modal="true" aria-label="编辑账号" tabIndex={-1} className="modal-container" style={{maxWidth: '600px'}}>
            <div className="modal-header flex items-start justify-between gap-4">
              <div>
                <h3 className="text-lg font-bold text-gray-900">编辑账号</h3>
                <p className="mt-1 text-xs text-gray-500">{editingAccount.nickname || editingAccount.remark || editingAccount.id}</p>
              </div>
              <button
                type="button"
                onClick={() => setActiveModal(null)}
                className="shrink-0 rounded-md p-2 hover:bg-gray-100"
                aria-label="关闭编辑账号"
              >
                <X className="w-5 h-5 text-gray-500" />
              </button>
            </div>

            <div className="modal-body space-y-5">
              {/* 账号ID */}
              <div>
                <label className="block text-sm font-bold text-gray-700 mb-2">账号ID</label>
                <input
                  type="text"
                  value={editingAccount.id}
                  disabled
                  className="ios-input w-full rounded-md bg-gray-50 px-3 py-2.5 text-gray-500"
                />
              </div>

              {/* 备注 */}
              <div>
                <label className="block text-sm font-bold text-gray-700 mb-2">备注</label>
                <input
                  type="text"
                  value={editForm.remark}
                  onChange={(e) => setEditForm({ ...editForm, remark: e.target.value })}
                  placeholder="为账号添加备注"
                  className="ios-input w-full rounded-md px-3 py-2.5"
                />
              </div>

              <div className="space-y-2 rounded-md border border-gray-200 p-3">
                <p className="text-sm">接口密钥仅限此账号，重新生成会撤销旧密钥。</p>
                <button type="button" className="ios-btn-secondary rounded-md px-3 py-2 text-sm" onClick={async () => {
                  if (!await confirmAction('生成新密钥后旧密钥立即失效，是否继续？')) return;
                  try { const result = await rotateAccountApiKey(editingAccount.id); setOneTimeApiKey(result.api_key); }
                  catch { notify('生成密钥失败，请稍后重试', 'error'); }
                }}>生成账号接口密钥</button>
                {oneTimeApiKey && <label className="block text-xs">密钥仅显示本次，请安全保存<input readOnly type="password" value={oneTimeApiKey} className="ios-input w-full font-mono" onFocus={event => event.target.select()} /></label>}
              </div>
              {/* Cookie */}
              <div>
                <label className="block text-sm font-bold text-gray-700 mb-2">Cookie</label>
                <textarea
                  value={editForm.cookie}
                  onChange={(e) => setEditForm({ ...editForm, cookie: e.target.value })}
                  placeholder="留空保留当前 Cookie；填写新的值才会替换"
                  className="ios-input h-28 w-full resize-y rounded-md px-3 py-2.5 font-mono text-xs"
                />
                <p className="text-xs text-gray-500 mt-1">{editingAccount.cookie_configured ? `当前已配置 Cookie（${editingAccount.cookie_length || 0} 字符）；留空不会修改` : '尚未配置 Cookie'}</p>
              </div>

              {/* 自动确认发货 */}
              <div className="flex items-center justify-between gap-4 rounded-md border border-gray-200 bg-gray-50 p-4">
                <div>
                  <div className="font-bold text-gray-900 flex items-center gap-2">
                    <Check className="w-4 h-4 text-green-500" />
                    自动确认发货
                  </div>
                  <div className="text-xs text-gray-500">卡券发送完成后确认卖家发货；请按业务需要开启</div>
                </div>
                <button
                  type="button"
                  onClick={() => setEditForm({ ...editForm, auto_confirm: !editForm.auto_confirm })}
                  className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${
                    editForm.auto_confirm ? 'bg-[#ffe100]' : 'bg-gray-300'
                  }`}
                >
                  <span
                    className={`absolute left-1 top-1 h-4 w-4 rounded-full bg-white shadow-sm transition-transform ${
                      editForm.auto_confirm ? 'translate-x-5' : ''
                    }`}
                  />
                </button>
              </div>

              {/* 暂停时长 */}
              <div>
                <label className="block text-sm font-bold text-gray-700 mb-2 flex items-center gap-2">
                  <Clock className="w-4 h-4 text-blue-500" />
                  暂停处理时长（分钟）
                </label>
                <input
                  type="number"
                  value={editForm.pause_duration}
                  onChange={(e) => setEditForm({ ...editForm, pause_duration: parseInt(e.target.value) || 0 })}
                  placeholder="0"
                  min="0"
                  max="1440"
                  className="ios-input w-full rounded-md px-3 py-2.5"
                />
                <p className="text-xs text-gray-500 mt-1">手动回复后暂停自动回复相应分钟，不会暂停订单任务</p>
              </div>

              {/* 登录信息 */}
              <div className="border-t border-gray-200 pt-5">
                <h3 className="text-lg font-bold text-gray-900 mb-4 flex items-center gap-2">
                  <Key className="w-5 h-5 text-amber-500" />
                  登录信息
                </h3>
                <div className="space-y-4">
                  <div>
                    <label className="block text-sm font-bold text-gray-700 mb-2">用户名</label>
                    <input
                      type="text"
                      value={editForm.username}
                      onChange={(e) => setEditForm({ ...editForm, username: e.target.value })}
                      placeholder="闲鱼账号/手机号"
                      className="ios-input w-full rounded-md px-3 py-2.5"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-bold text-gray-700 mb-2">登录密码</label>
                    <div className="relative">
                      <input
                        type={editForm.showLoginPassword ? 'text' : 'password'}
                        value={editForm.login_password}
                        onChange={(e) => setEditForm({ ...editForm, login_password: e.target.value })}
                        placeholder={editingAccount.login_password_configured ? '已保存，留空保留原密码' : '用于自动登录'}
                        className="ios-input w-full rounded-md px-3 py-2.5 pr-11"
                      />
                      <button
                        type="button"
                        onClick={() => setEditForm({ ...editForm, showLoginPassword: !editForm.showLoginPassword })}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                      >
                        {editForm.showLoginPassword ? <EyeOff className="w-5 h-5" /> : <Eye className="w-5 h-5" />}
                      </button>
                    </div>
                  </div>
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="font-bold text-gray-900">登录时显示浏览器</div>
                      <div className="text-xs text-gray-500">调试时可开启查看登录过程</div>
                    </div>
                    <button
                      type="button"
                      onClick={() => setEditForm({ ...editForm, show_browser: !editForm.show_browser })}
                      className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${
                        editForm.show_browser ? 'bg-[#ffe100]' : 'bg-gray-300'
                      }`}
                    >
                      <span
                        className={`absolute left-1 top-1 h-4 w-4 rounded-full bg-white shadow-sm transition-transform ${
                          editForm.show_browser ? 'translate-x-5' : ''
                        }`}
                      />
                    </button>
                  </div>
                </div>
              </div>
            </div>

            <div className="modal-footer">
              <div className="flex w-full gap-2">
                <button
                  type="button"
                  onClick={() => setActiveModal(null)}
                  className="ios-btn-secondary flex-1 rounded-md px-4 py-2.5 text-sm"
                  disabled={saving}
                >
                  取消
                </button>
                <button
                  type="button"
                  onClick={handleSaveEdit}
                  className="ios-btn-primary flex flex-1 items-center justify-center gap-2 rounded-md px-4 py-2.5 text-sm"
                  disabled={saving}
                >
                  {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                  {saving ? '保存中...' : '保存'}
                </button>
              </div>
            </div>
          </div>
        </div>,
        document.body
      )}

      {/* AI设置弹窗 */}
      {activeModal === 'ai-settings' && editingAccount && createPortal(
        <div className="modal-overlay">
          <div className="modal-container" style={{maxWidth: '600px'}}>
            <div className="modal-header flex items-start justify-between gap-4">
              <div>
                <h3 className="flex items-center gap-2 text-lg font-bold text-gray-900">
                  <Bot className="h-5 w-5 text-amber-600" />
                  AI 助手设置
                </h3>
                <p className="mt-1 text-xs text-gray-500">{editingAccount.nickname || editingAccount.remark || editingAccount.id}</p>
              </div>
              <button
                type="button"
                onClick={() => setActiveModal(null)}
                className="shrink-0 rounded-md p-2 hover:bg-gray-100"
                aria-label="关闭 AI 助手设置"
              >
                <X className="w-5 h-5 text-gray-500" />
              </button>
            </div>

            <div className="modal-body space-y-6">
              {/* 启用AI */}
              <div className="flex items-center justify-between gap-4 rounded-md border border-gray-200 bg-gray-50 p-4">
                <div>
                  <div className="font-bold text-gray-900 flex items-center gap-2">
                    <Bot className="h-4 w-4 text-amber-600" />
                    启用 AI 自动回复
                  </div>
                  <div className="text-xs text-gray-500">AI将自动处理买家的砍价消息</div>
                </div>
                <button
                  type="button"
                  onClick={() => setAiSettings({ ...aiSettings, ai_enabled: !aiSettings.ai_enabled })}
                  className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${
                    aiSettings.ai_enabled ? 'bg-[#ffe100]' : 'bg-gray-300'
                  }`}
                >
                  <span
                    className={`absolute left-1 top-1 h-4 w-4 rounded-full bg-white shadow-sm transition-transform ${
                      aiSettings.ai_enabled ? 'translate-x-5' : ''
                    }`}
                  />
                </button>
              </div>

              {/* 砍价策略 */}
              <div className="border-t border-gray-200 pt-6">
                <h3 className="text-lg font-bold text-gray-900 mb-4">砍价策略</h3>
                <div className="grid gap-4 sm:grid-cols-3">
                  <div>
                    <label className="block text-sm font-bold text-gray-700 mb-2">最大折扣比例 (%)</label>
                    <input
                      type="number"
                      value={aiSettings.max_discount_percent}
                      onChange={(e) => setAiSettings({ ...aiSettings, max_discount_percent: parseInt(e.target.value) || 0 })}
                      className="ios-input w-full rounded-md px-3 py-2.5"
                      min="0"
                      max="100"
                    />
                    <p className="text-xs text-gray-500 mt-1">例如：10表示最多降价10%</p>
                  </div>
                  <div>
                    <label className="block text-sm font-bold text-gray-700 mb-2">最大折扣金额 (元)</label>
                    <input
                      type="number"
                      value={aiSettings.max_discount_amount}
                      onChange={(e) => setAiSettings({ ...aiSettings, max_discount_amount: parseInt(e.target.value) || 0 })}
                      className="ios-input w-full rounded-md px-3 py-2.5"
                      min="0"
                    />
                    <p className="text-xs text-gray-500 mt-1">例如：100表示最多降价100元</p>
                  </div>
                  <div>
                    <label className="block text-sm font-bold text-gray-700 mb-2">最大砍价轮次</label>
                    <input
                      type="number"
                      value={aiSettings.max_bargain_rounds}
                      onChange={(e) => setAiSettings({ ...aiSettings, max_bargain_rounds: parseInt(e.target.value) || 1 })}
                      className="ios-input w-full rounded-md px-3 py-2.5"
                      min="1"
                      max="10"
                    />
                    <p className="text-xs text-gray-500 mt-1">买家最多可以砍价的次数</p>
                  </div>
                </div>
              </div>

              {/* 自定义提示词 */}
              <div>
                <label className="block text-sm font-bold text-gray-700 mb-2">自定义提示词（可选）</label>
                <textarea
                  value={aiSettings.custom_prompts}
                  onChange={(e) => setAiSettings({ ...aiSettings, custom_prompts: e.target.value })}
                  placeholder="输入自定义的AI回复规则或风格指引...&#10;&#10;例如：回复时保持礼貌专业、使用简洁的语言、强调产品质量等"
                  className="ios-input h-36 w-full resize-y rounded-md px-3 py-2.5"
                />
              </div>

              {/* AI如何工作 */}
              <div className="rounded-md border border-blue-200 bg-blue-50 p-4">
                <h4 className="mb-2 flex items-center gap-2 font-bold text-blue-900">
                  <Settings className="w-4 h-4" />
                  AI如何工作
                </h4>
                <ul className="text-xs text-blue-800 space-y-1">
                  <li>• 自动识别买家的砍价请求</li>
                  <li>• 根据设定的策略智能回复</li>
                  <li>• 在合理范围内同意降价或礼貌拒绝</li>
                  <li>• 保持专业友好的沟通风格</li>
                </ul>
              </div>
            </div>

            <div className="modal-footer">
              <div className="flex w-full gap-2">
                <button
                  type="button"
                  onClick={() => setActiveModal(null)}
                  className="ios-btn-secondary flex-1 rounded-md px-4 py-2.5 text-sm"
                  disabled={saving}
                >
                  取消
                </button>
                <button
                  type="button"
                  onClick={handleSaveAISettings}
                  className="ios-btn-primary flex flex-1 items-center justify-center gap-2 rounded-md px-4 py-2.5 text-sm"
                  disabled={saving}
                >
                  {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                  {saving ? '保存中...' : '保存'}
                </button>
              </div>
            </div>
          </div>
        </div>,
        document.body
      )}

      {diagnosticAccount && createPortal(
        <div className="modal-overlay">
          <div ref={diagnosticDialog} role="dialog" aria-modal="true" aria-labelledby="account-issue-title" tabIndex={-1} className="modal-container modal-container-lg">
            <div className="modal-header flex items-center justify-between gap-3">
              <h3 id="account-issue-title" className="text-lg font-bold">{diagnosticAccount.event_label || '账号诊断'}</h3>
              <button type="button" onClick={() => setDiagnosticAccount(null)} aria-label="关闭诊断" className="rounded-md p-2"><X className="h-5 w-5" /></button>
            </div>
            <div className="modal-body space-y-4 text-sm">
              <p className="break-words">{diagnosticAccount.recovery_detail || diagnosticAccount.event_impact}</p>
              <p className="rounded-md bg-[var(--surface-subtle)] p-3 leading-6">建议操作：{diagnosticAccount.recommended_action}</p>
              <BrowserDiagnostics diagnosticId={diagnosticAccount.diagnostic_id} defaultOpen emptyText="此事件没有浏览器诊断。账号状态与通知日志中已记录原因；查看诊断不会请求闲鱼。" />
            </div>
            <div className="modal-footer flex justify-end"><button type="button" onClick={() => setDiagnosticAccount(null)} className="ios-btn-secondary rounded-md px-4 py-2 text-sm">关闭</button></div>
          </div>
        </div>, document.body
      )}
      {captchaAccount && createPortal(
        <div className="modal-overlay">
          <div ref={captchaDialog} role="dialog" aria-modal="true" aria-labelledby="manual-captcha-title" tabIndex={-1} className="modal-container modal-container-lg">
            <div className="modal-header flex items-start justify-between gap-4">
              <div>
                <h3 id="manual-captcha-title" className="text-lg font-bold text-gray-900">{captchaCheckOnly ? '检查账号连接' : '人工滑块验证'}</h3>
                <p className="mt-1 text-sm text-gray-500">
                  {captchaAccount.nickname || captchaAccount.id}
                </p>
              </div>
              <button
                type="button"
                onClick={closeManualCaptcha}
                className="rounded-md p-2 hover:bg-gray-100"
                aria-label="关闭"
              >
                <X className="h-5 w-5" />
              </button>
            </div>

            <div className="modal-body space-y-4">
              <div className="rounded-md border border-blue-200 bg-blue-50 px-3 py-2 text-xs leading-relaxed text-blue-800">
                <p className="font-bold">{captchaCheckOnly ? '消息安全验证后的连接检查' : '本人完成安全验证'}</p>
                <p className="mt-1">{captchaCheckOnly
                  ? '使用已保存的网页登录凭证检查消息连接。若官网消息已通过验证而这里仍被拦截，请通过「同步网页登录态」更新验证后的会话。'
                  : '验证控件准备好后，手动拖动下方滑块。页面报错时本轮停止，请查看浏览器日志。'}</p>
                <p className="mt-1">取得有效令牌并确认消息连接后，才显示已恢复。</p>
              </div>

              <div role="status" aria-live="polite" className={`rounded-md px-3 py-2 text-sm font-semibold ${
                captchaStage === 'done'
                  ? 'bg-green-50 text-green-700'
                  : captchaStage === 'failed'
                    ? 'bg-red-50 text-red-700'
                    : 'bg-amber-50 text-amber-800'
              }`}>
                {captchaMessage || '正在准备…'}
              </div>

              {!captchaCheckOnly && <div className="flex min-h-[320px] items-center justify-center rounded-md border-2 border-dashed border-gray-200 bg-gray-50 p-2">
                {captchaShot ? (
                  <img
                    ref={captchaImgRef}
                    src={captchaShot}
                    alt="服务器端验证页面"
                    draggable={false}
                    className={`max-h-[460px] max-w-full w-auto select-none rounded ${captchaStage === 'ready' ? 'cursor-crosshair' : ''}`}
                    onMouseDown={e => { e.preventDefault(); sendCaptchaMouse('down', e); }}
                    onMouseMove={e => { if (e.buttons === 1) sendCaptchaMouse('move', e); }}
                    onMouseUp={e => sendCaptchaMouse('up', e)}
                    onMouseLeave={e => { if (e.buttons === 1) sendCaptchaMouse('up', e); }}
                  />
                ) : (
                  <div className="text-center text-sm text-gray-500">
                    <Loader2 className="mx-auto mb-2 h-6 w-6 animate-spin text-gray-400" />
                    正在服务器上打开验证页面…
                    <p className="mt-1 text-xs text-gray-400">
                      首次启动需要几秒，若账号当前不在风控状态则不会出现滑块
                    </p>
                  </div>
                )}
              </div>}

              <BrowserDiagnostics diagnosticId={captchaDiagnosticId} emptyText={captchaStage === 'starting' ? '正在创建本轮诊断…' : '本轮尚未取得浏览器诊断'} />
              {!captchaCheckOnly && <p className="text-xs text-gray-500">
                操作方式：在上方画面的滑块按钮上 <b>按住鼠标左键</b>，
                <b>向右拖到底</b> 后松开。画面会随你的操作实时刷新。页面报错时请先查看日志；如需重试，关闭弹窗后再手动开启一次。
              </p>}
            </div>

            <div className="modal-footer flex justify-end">
              <button
                type="button"
                onClick={closeManualCaptcha}
                className="ios-btn-secondary rounded-md px-4 py-2 text-sm"
              >
                关闭
              </button>
            </div>
          </div>
        </div>,
        document.body
      )}
    </div>
  );
};

export default AccountList;
