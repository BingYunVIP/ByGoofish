import { useEffect, useState } from 'react';
import { formatOrderTime } from '../lib/orderTime';
import { Activity } from 'lucide-react';
import { AutoReplyLog, DefaultReply } from '../types';
import { getAutoReplyLogs, getDefaultReply } from '../services/api';
import { SectionHeader } from './ui';

const reasons: Record<string, string> = {
  waiting_first_reply: '首次咨询等待5秒', selecting_reply: '正在选择回复', reply_sent: '平台已确认发送', auto_reply_disabled: '自动回复未开启',
  default_reply_disabled: '账号默认回复未开启', chat_paused: '人工接管中', chat_paused_before_send: '发送前已人工接管',
  skip_reply_filter: '命中过滤规则', empty_reply: '回复内容为空', reply_once: '此会话已触发单次回复',
  message_outside_window: '消息已超过本轮处理窗口', no_rule_matched: '没有匹配规则', image_prepare_failed: '图片处理失败', image_url_invalid: '图片链接无效', image_download_failed: '图片链接读取失败', image_too_large: '图片超过5MB', image_decode_failed: '图片格式或尺寸无效', image_file_missing: '本地图片不存在', image_upload_failed: '上传闲鱼图片服务器失败', platform_rejected: '平台拒绝发送',
  result_unconfirmed: '结果待核查，请勿重复发送', account_unavailable: '账号连接不可用',
  automation_echo: '本系统自动消息，不再互相回复', reply_selection_failed: '回复生成失败', send_failed: '发送失败',
};
const states: Record<string, string> = {
  success: '已发送', failed: '失败', unknown: '待核查', sending: '发送中',
  not_requested: '未发送', skipped: '已暂停',
};
export default function AutoReplyActivity({ account, version }: { account: string; version: number }) {
  const [data, setData] = useState<{ logs: AutoReplyLog[]; config: DefaultReply } | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    let busy = false;
    setData(null);
    async function refresh() {
      if (busy || document.visibilityState === 'hidden') return;
      busy = true;
      try {
        const [logs, config] = await Promise.all([
          getAutoReplyLogs({ cookie_id: account, page_size: 8 }), getDefaultReply(account),
        ]);
        if (active) { setData({ logs: logs.data, config }); setError(''); }
      } catch { if (active) setError('回复记录加载失败，稍后自动重试'); }
      finally { busy = false; }
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15000);
    const visible = () => { if (document.visibilityState === 'visible') void refresh(); };
    document.addEventListener('visibilitychange', visible);
    return () => { active = false; window.clearInterval(timer); document.removeEventListener('visibilitychange', visible); };
  }, [account, version]);
  const runtime = data?.config.runtime;
  return <section className="section-panel" aria-label="自动回复处理记录">
    <SectionHeader title="回复处理记录" icon={Activity} />
    <div className="space-y-3 px-4 py-4">
      <p className="text-xs text-[var(--text-muted)]" role="status">
        {error || (!data ? '正在读取回复状态…' : !runtime?.connected ? '消息连接未就绪，请在账号管理检查连接'
          : runtime.paused_conversations ? `${runtime.paused_conversations} 个会话正在人工接管，最近一个约 ${Math.ceil(runtime.remaining_pause_seconds / 60)} 分钟后恢复；可到消息中心提前恢复`
          : '消息连接正常，配置对新来信生效')}
      </p>
      {data?.config.updated_at && <p className="text-xs text-[var(--text-soft)]">配置更新时间：{formatOrderTime(data.config.updated_at)}</p>}
      {data && !data.logs.length && <p className="text-sm text-[var(--text-muted)]">暂无新消息处理记录</p>}
      {data?.logs.map(entry => <article key={entry.id} className="min-w-0 rounded-md border border-[var(--border)] p-3">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className={`status-badge ${entry.process_status === 'success' ? 'status-badge-success' : entry.process_status === 'failed' ? 'status-badge-danger' : 'status-badge-info'}`}>
            {reasons[entry.decision_reason || ''] || '历史处理记录'}
          </span>
          <span className="text-[var(--text-soft)]">{entry.message_source === 'history' ? '历史补查' : '实时推送'}</span>
          {entry.created_at && <time className="text-[var(--text-soft)]">{formatOrderTime(entry.created_at)}</time>}
        </div>
        <p className="mt-2 whitespace-pre-wrap break-words text-sm text-[var(--text-muted)]">{entry.source_message || '图片消息'}</p>
        <p className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-[var(--text-soft)]">
          <span>文字：{states[entry.text_send_status || 'not_requested'] || '待核查'}</span>
          <span>图片：{states[entry.image_send_status || 'not_requested'] || '待核查'}</span>
          {!!entry.remaining_pause_seconds && <span>本次跳过时剩余暂停 {Math.ceil(entry.remaining_pause_seconds / 60)} 分钟</span>}
        </p>
      </article>)}
    </div>
  </section>;
}
