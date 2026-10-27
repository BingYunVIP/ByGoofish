import React, { useEffect, useRef, useState } from 'react';
import { Save, Eye } from 'lucide-react';
import { NotificationEventDefinition } from '../types';
import { getNotificationTemplates, previewNotificationTemplate, saveNotificationTemplate } from '../services/api';
import { notify } from '../services/feedback';
import { describeApiError } from '../lib/errorMessage';
import { SectionHeader } from './ui';

const NotificationTemplateEditor: React.FC<{ events: NotificationEventDefinition[] }> = ({ events }) => {
  const [eventType, setEventType] = useState('login_verification_required');
  const [templates, setTemplates] = useState<Record<string, string>>({});
  const [savedTemplates, setSavedTemplates] = useState<Record<string, string>>({});
  const [variables, setVariables] = useState<Record<string, string>>({});
  const [defaultContent, setDefaultContent] = useState('{{message}}');
  const [preview, setPreview] = useState('');
  const [previewIsHtml, setPreviewIsHtml] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [saving, setSaving] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const content = templates[eventType] ?? defaultContent;
  const dirty = content !== (savedTemplates[eventType] ?? defaultContent);

  useEffect(() => {
    let cancelled = false;
    getNotificationTemplates().then(data => {
      if (cancelled) return;
      setTemplates(data.templates);
      setSavedTemplates(data.templates);
      setVariables(data.variables);
      setDefaultContent(data.default_content);
    }).catch(error => {
      if (!cancelled) setLoadError(describeApiError(error, '加载通知模板失败，请切换页面后重试'));
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (loading || loadError) return;
    let cancelled = false;
    setPreviewing(true);
    setError('');
    const timer = window.setTimeout(() => {
      previewNotificationTemplate(eventType, content).then(data => {
        if (!cancelled) {
          setPreview(data.content);
          setPreviewIsHtml(Boolean(data.is_html));
        }
      }).catch(error => {
        if (!cancelled) { setPreview(''); setError(describeApiError(error, '预览失败')); }
      }).finally(() => { if (!cancelled) setPreviewing(false); });
    }, 250);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [eventType, content, loading, loadError]);

  const save = async () => {
    setSaving(true);
    try {
      const result = await saveNotificationTemplate(eventType, content);
      setTemplates(current => ({ ...current, [eventType]: result.content }));
      setSavedTemplates(current => ({ ...current, [eventType]: result.content }));
      notify('通知模板已保存，后续正式通知即时生效', 'success');
    } catch (error) {
      notify(describeApiError(error, '保存通知模板失败'), 'error');
    } finally { setSaving(false); }
  };

  const insertVariable = (variable: string) => {
    const textarea = textareaRef.current;
    const start = textarea?.selectionStart ?? content.length;
    const end = textarea?.selectionEnd ?? start;
    const token = `{{${variable}}}`;
    setTemplates(current => ({ ...current, [eventType]: content.slice(0, start) + token + content.slice(end) }));
    window.requestAnimationFrame(() => {
      textarea?.focus();
      textarea?.setSelectionRange(start + token.length, start + token.length);
    });
  };

  return (
    <section className="section-panel">
      <SectionHeader title="通知预览" icon={Eye}
        description="按事件编辑通知正文，保存后用于当前用户全部账号及已订阅渠道；预览使用示例数据，不发送通知。" />
      <div className="space-y-4 p-4">
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-sm">
            通知事件
            <select aria-label="通知模板事件" value={eventType} onChange={event => setEventType(event.target.value)}
            disabled={loading || saving || Boolean(loadError)} className="ios-input rounded-md px-3 py-2">
              {events.map(event => <option key={event.id} value={event.id}>{event.label}</option>)}
            </select>
          </label>
          <button type="button" onClick={() => setTemplates(current => ({ ...current, [eventType]: defaultContent }))}
            disabled={loading || saving || Boolean(loadError)} className="ios-btn-secondary rounded-md px-3 py-2 text-sm">恢复默认</button>
          <button type="button" onClick={() => void save()} disabled={loading || saving || previewing || Boolean(error) || Boolean(loadError) || !dirty}
            className="ios-btn-primary flex items-center gap-2 rounded-md px-4 py-2 text-sm disabled:opacity-50">
            <Save className="h-4 w-4" />{saving ? '保存中…' : '保存模板'}
          </button>
          {dirty && <span className="text-xs text-[var(--text-muted)]">当前事件有未保存修改</span>}
        </div>
        <div className="grid min-w-0 gap-4 lg:grid-cols-2">
          <div className="min-w-0">
            <label htmlFor="notification-template-content" className="mb-2 block text-sm font-bold">编写内容</label>
            <textarea id="notification-template-content" ref={textareaRef} value={loading ? '' : content} maxLength={8000}
              style={{ minHeight: '32rem' }}
              onChange={event => setTemplates(current => ({ ...current, [eventType]: event.target.value }))}
              disabled={loading || saving || Boolean(loadError)} className="ios-input min-h-[32rem] w-full resize-y rounded-md p-3 font-mono text-xs leading-6" />
            <p className="mt-2 text-xs leading-5 text-[var(--text-muted)]">点击变量插入；{'{{message}}'} 保留系统原始通知。默认模板采用响应式邮件卡片样式。支持纯文本及受限 HTML；邮件发送 HTML 和纯文本版本，其他渠道转为纯文本。脚本、图片和外部资源会被过滤；验证链接会安全保留。</p>
            <div className="mt-2 flex flex-wrap gap-2">
              {Object.entries(variables).map(([key, label]) => (
                <button key={key} type="button" title={`插入 ${label}`} disabled={loading || saving || Boolean(loadError)}
                  onClick={() => insertVariable(key)} className="ios-btn-secondary rounded-md px-2 py-1 text-xs">
                  {label} {`{{${key}}}`}
                </button>
              ))}
            </div>
          </div>
          <div className="min-w-0">
            <h3 className="mb-2 text-sm font-bold">实时预览（示例数据）</h3>
            {loading || previewing ? (
              <div aria-live="polite" aria-busy="true"
                className="flex min-h-[32rem] items-center justify-center rounded-md border border-[var(--border)] bg-[var(--surface-subtle)] p-4 text-sm text-[var(--text-muted)]">
                {loading ? '加载中…' : '正在更新预览…'}
              </div>
            ) : previewIsHtml ? (
              <iframe
                title="通知邮件实时预览"
                aria-busy={previewing}
                sandbox=""
                referrerPolicy="no-referrer"
                srcDoc={preview}
                className="h-[32rem] w-full rounded-md border border-[var(--border)] bg-white"
              />
            ) : (
              <div aria-live="polite" aria-busy={previewing}
                className="min-h-[32rem] whitespace-pre-wrap break-words rounded-md border border-[var(--border)] bg-[var(--surface-subtle)] p-4 text-sm">
                {preview}
              </div>
            )}
          </div>
        </div>
        {(error || loadError) && <p role="alert" className="text-sm text-red-600">{loadError || error}</p>}
      </div>
    </section>
  );
};

export default NotificationTemplateEditor;
