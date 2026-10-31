import { useEffect, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Bell, Mail, Plus, Send, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { api, ApiError } from '../../api/client';
import { useLocale } from '../../app/locale';
import { Dialog } from '../../components/ui/Dialog';
import { useConfirm } from '../../components/ui/ConfirmProvider';
import { EmptyState, ErrorState, LoadingState, Section, Timestamp, safeExternalUrl } from '../../components/workflow/Workflow';
import type { SettingsStore } from '../settings/state';
import '../security/security.css';
import './notifications.css';

export interface Endpoint { id: number; provider: string; name: string; address: string; enabled: boolean; verified: boolean; credential_configured: boolean }
interface Subscription { id: number; event_type: string; endpoint_id: number | null; repo_name: string | null; resource_type: string | null; resource_id: string | null; min_severity: string; enabled: boolean }
interface Configuration { providers: Array<{ channel: string; capabilities: string[] }>; event_types: string[]; endpoints: Endpoint[]; subscriptions: Subscription[] }
interface EndpointDraft { name: string; provider: string; address: string; signing_secret: string; enabled: boolean }
interface SubscriptionDraft { event_type: string; endpoint_id: string; min_severity: string; repo_name: string; resource_type: string; resource_id: string; enabled: boolean }
const blankEndpoint = (): EndpointDraft => ({ name: '', provider: 'email', address: '', signing_secret: '', enabled: true });
const blankSubscription = (): SubscriptionDraft => ({ event_type: '', endpoint_id: '', min_severity: 'info', repo_name: '', resource_type: '', resource_id: '', enabled: true });
const base = '/api/v1/notifications';
type Translate = (zh: string, en: string) => string;

export function eventLabel(value: string, t: Translate): string {
  const [domain, state] = value.split('.');
  const domains: Record<string, [string, string]> = { review: ['审查', 'Review'], issue: ['Issue 分析', 'Issue analysis'], scan: ['仓库扫描', 'Repository scan'], agent: ['Agent 任务', 'Agent task'], announcement: ['公告', 'Announcement'], security: ['账户安全', 'Account security'], quota: ['配额', 'Quota'], billing: ['余额', 'Balance'], payment: ['支付', 'Payment'], refund: ['退款', 'Refund'], system: ['系统', 'System'], endpoint: ['通知渠道', 'Notification channel'] };
  const states: Record<string, [string, string]> = { started: ['开始', 'Started'], completed: ['完成', 'Completed'], failed: ['失败', 'Failed'], cancelled: ['取消', 'Cancelled'], action_required: ['需要处理', 'Action required'], published: ['发布', 'Published'], changed: ['变更', 'Changed'], lockout: ['锁定', 'Locked'], exceeded: ['不足', 'Exceeded'], low_balance: ['余额不足', 'Low balance'], requested: ['已申请', 'Requested'], rejected: ['拒绝', 'Rejected'], test: ['测试', 'Test'] };
  return `${domains[domain] ? t(...domains[domain]) : domain} · ${states[state] ? t(...states[state]) : state}`;
}

export default function NotificationsPanel({ store }: { store?: SettingsStore }) {
  const { t } = useLocale();
  const confirm = useConfirm();
  const client = useQueryClient();
  const query = useQuery({ queryKey: ['notification-settings'], queryFn: () => api<Configuration>(base + '/settings') });
  const [editing, setEditing] = useState<Endpoint | 'new' | null>(null);
  const [draft, setDraft] = useState(blankEndpoint);
  const [verifying, setVerifying] = useState<Endpoint | null>(null);
  const [code, setCode] = useState('');
  const [subscription, setSubscription] = useState<Subscription | 'new' | null>(null);
  const [rule, setRule] = useState(blankSubscription);
  const [binding, setBinding] = useState<{ token: string; deep_link: string | null; expires_at: string } | null>(null);
  const [deliveryEndpoint, setDeliveryEndpoint] = useState<Endpoint | null>(null);
  const mutation = useMutation({ mutationFn: (operation: () => Promise<void>) => operation(), gcTime: 0 });
  useEffect(() => { store?.setExternalDirty('notifications', !!editing || !!subscription || !!code); return () => store?.setExternalDirty('notifications', false); }, [store, editing, subscription, code]);
  useEffect(() => { const clear = () => { setBinding(null); setDraft(blankEndpoint()); setCode(''); setEditing(null); }; window.addEventListener('sakura:session-expired', clear); return () => window.removeEventListener('sakura:session-expired', clear); }, []);
  async function run(operation: () => Promise<void>, success = t('已更新', 'Updated')) {
    try { await mutation.mutateAsync(operation); await client.invalidateQueries({ queryKey: ['notification-settings'] }); toast.success(success); }
    catch (error) { toast.error(error instanceof ApiError && error.status === 400 ? t('无法完成操作，请检查地址、验证码或渠道状态后重试。', 'Check the address, verification code, or channel status and try again.') : error instanceof ApiError && error.status === 403 ? t('没有权限执行此操作。', 'You do not have permission to perform this action.') : t('操作失败，请稍后重试。', 'Unable to complete this action. Try again later.')); }
    finally { mutation.reset(); }
  }
  function edit(endpoint: Endpoint | 'new') { setEditing(endpoint); setDraft(endpoint === 'new' ? { ...blankEndpoint(), provider: query.data?.providers.find(provider => ['email', 'webhook'].includes(provider.channel))?.channel ?? 'email' } : { ...endpoint, signing_secret: '' }); }
  function saveEndpoint(event: FormEvent) {
    event.preventDefault();
    void run(async () => {
      if (editing === 'new') await api(base + '/endpoints', { method: 'POST', body: JSON.stringify({ name: draft.name, provider: draft.provider, address: draft.address, signing_secret: draft.signing_secret }) });
      else if (editing) await api(base + `/endpoints/${editing.id}`, { method: 'PATCH', body: JSON.stringify({ name: draft.name, enabled: draft.enabled, ...(draft.signing_secret ? { signing_secret: draft.signing_secret } : {}) }) });
      setEditing(null); setDraft(blankEndpoint());
    });
  }
  function editRule(value: Subscription | 'new') {
    setSubscription(value); setRule(value === 'new' ? blankSubscription() : { event_type: value.event_type, endpoint_id: value.endpoint_id == null ? '' : String(value.endpoint_id), min_severity: value.min_severity, repo_name: value.repo_name ?? '', resource_type: value.resource_type ?? '', resource_id: value.resource_id ?? '', enabled: value.enabled });
  }
  function saveRule(event: FormEvent) {
    event.preventDefault();
    void run(async () => { await api(base + '/subscriptions', { method: 'POST', body: JSON.stringify({ ...rule, endpoint_id: rule.endpoint_id ? Number(rule.endpoint_id) : null, repo_name: rule.repo_name || null, resource_type: rule.resource_type || null, resource_id: rule.resource_id || null }) }); setSubscription(null); setRule(blankSubscription()); });
  }
  if (query.isPending) return <LoadingState />;
  if (query.isError) return <ErrorState retry={() => query.refetch()} />;
  const config = query.data;
  const writableProviders = config.providers.filter(provider => ['email', 'webhook'].includes(provider.channel));
  return <div className="security-panel">
    <Section title={t('通知渠道', 'Delivery channels')} aside={<button className="button" onClick={() => edit('new')} disabled={mutation.isPending || !writableProviders.length}><Plus size={16} />{t('添加渠道', 'Add channel')}</button>}>
      <p className="muted">{t('站内通知默认开启。验证外部渠道后，选择希望接收的事件。', 'Inbox notifications are on by default. Verify an external channel, then choose the events you want to receive.')}</p>
      {config.endpoints.length === 0 && <EmptyState title={t('尚未添加外部渠道', 'No external channels yet')} description={t('可以使用邮件、Telegram 或 Webhook 接收通知。', 'Receive notifications through email, Telegram, or a webhook.')} />}
      {config.endpoints.map(endpoint => <div className="notification-endpoint" key={endpoint.id}><div className="notification-endpoint-title"><Mail size={18} /><div><strong>{endpoint.name}</strong><p>{endpoint.address}</p></div><span className={`notification-pill ${endpoint.verified && endpoint.enabled ? 'verified' : ''}`}>{!endpoint.enabled ? t('已停用', 'Disabled') : endpoint.verified ? t('已验证', 'Verified') : t('待验证', 'Unverified')}</span></div><div className="notification-actions"><button className="button" onClick={() => edit(endpoint)} disabled={mutation.isPending}>{t('编辑', 'Edit')}</button>{!endpoint.verified && endpoint.enabled && endpoint.provider !== 'telegram' && <button className="button" onClick={() => { setVerifying(endpoint); setCode(''); }} disabled={mutation.isPending}>{t('验证', 'Verify')}</button>}<button className="button" disabled={mutation.isPending || !endpoint.enabled || !endpoint.verified} onClick={() => void run(async () => { await api(base + `/endpoints/${endpoint.id}/test`, { method: 'POST' }); }, t('测试通知已进入发送队列', 'Test notification queued'))}>{t('发送测试', 'Send test')}</button><button className="button" onClick={() => setDeliveryEndpoint(endpoint)}>{t('发送记录', 'Delivery history')}</button><button className="icon-button danger-text" disabled={mutation.isPending} aria-label={t("移除 {0}", "Remove {0}", [endpoint.name])} onClick={async () => { if (await confirm({ title: t('移除通知渠道？', 'Remove delivery channel?'), description: t('此渠道的待发送通知将被取消。', 'Pending deliveries to this channel will be cancelled.'), destructive: true })) void run(async () => { await api(base + `/endpoints/${endpoint.id}`, { method: 'DELETE' }); }); }}><Trash2 size={16} /></button></div></div>)}
      {config.providers.some(provider => provider.channel === 'telegram') && <button className="button" disabled={mutation.isPending} onClick={() => void run(async () => { const result = await api<{ success: boolean; data: { token: string; deep_link: string | null; expires_at: string } }>('/api/v1/webui/security/telegram/bind', { method: 'POST' }); setBinding(result.data); }, t('绑定链接已生成', 'Binding link created'))}><Send size={16} />{t('绑定 Telegram', 'Connect Telegram')}</button>}
    </Section>
    <Section title={t('通知偏好', 'Notification preferences')} aside={<button className="button" onClick={() => editRule('new')} disabled={mutation.isPending}><Plus size={16} />{t('添加偏好', 'Add preference')}</button>}>
      <p className="muted">{t('每条偏好对应一个事件和一个接收渠道。敏感通知只发送到已验证的私人渠道。', 'Each preference links an event to one destination. Sensitive notifications use verified private destinations only.')}</p>
      {config.subscriptions.length === 0 && <EmptyState title={t('按需要选择通知', 'Choose the notifications you need')} description={t('例如：仅在 Agent 需要处理时发送邮件。', 'For example: email me when an Agent task needs attention.')} />}
      {config.subscriptions.map(item => <div className="security-row" key={item.id}><div><h3>{eventLabel(item.event_type, t)}</h3><p>{item.endpoint_id == null ? t('站内通知', 'Inbox') : config.endpoints.find(endpoint => endpoint.id === item.endpoint_id)?.name ?? t('渠道不可用', 'Unavailable channel')}{item.repo_name ? ` · ${item.repo_name}` : ''} · {item.enabled ? t('开启', 'On') : t('关闭', 'Off')}</p></div><div className="notification-actions"><button className="button" disabled={mutation.isPending} onClick={() => editRule(item)}>{t('编辑', 'Edit')}</button><button className="icon-button danger-text" disabled={mutation.isPending} aria-label={t("删除偏好 {0}", "Delete preference {0}", [eventLabel(item.event_type, t)])} onClick={async () => { if (await confirm({ title: t('删除通知偏好？', 'Delete notification preference?'), description: t('此事件将恢复默认通知规则。', 'The default notification rules will apply to this event.'), destructive: true })) void run(async () => { await api(base + `/subscriptions/${item.id}`, { method: 'DELETE' }); }); }}><Trash2 size={16} /></button></div></div>)}
    </Section>
    <Dialog open={!!editing} onOpenChange={open => { if (!open && !mutation.isPending) { setEditing(null); setDraft(blankEndpoint()); } }} title={editing === 'new' ? t('添加通知渠道', 'Add delivery channel') : t('编辑通知渠道', 'Edit delivery channel')}><form className="security-form" onSubmit={saveEndpoint}><label>{t('名称', 'Name')}<input required maxLength={100} value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} /></label>{editing === 'new' && <><label>{t('渠道类型', 'Channel type')}<select value={draft.provider} onChange={e => setDraft({ ...draft, provider: e.target.value })}>{writableProviders.map(provider => <option key={provider.channel} value={provider.channel}>{provider.channel === 'email' ? t('邮件', 'Email') : 'Webhook'}</option>)}</select></label><label>{draft.provider === 'email' ? t('邮件地址', 'Email address') : t('HTTPS 接收地址', 'HTTPS receiver URL')}<input required type={draft.provider === 'email' ? 'email' : 'url'} value={draft.address} onChange={e => setDraft({ ...draft, address: e.target.value })} /></label></>}{draft.provider === 'webhook' && <label>{editing === 'new' ? t('签名密钥', 'Signing secret') : t('更换签名密钥（留空保留）', 'Replace signing secret (leave blank to keep)')}<input type="password" autoComplete="new-password" value={draft.signing_secret} onChange={e => setDraft({ ...draft, signing_secret: e.target.value })} /><small className="muted">{t('更换密钥后需要重新验证接收地址。', 'Verify the receiver again after replacing its secret.')}</small></label>}{editing !== 'new' && <label className="notification-check"><input type="checkbox" checked={draft.enabled} onChange={e => setDraft({ ...draft, enabled: e.target.checked })} />{t('启用渠道', 'Enable channel')}</label>}<button className="button primary" disabled={mutation.isPending}>{t('保存', 'Save')}</button></form></Dialog>
    <Dialog open={!!verifying} onOpenChange={open => { if (!open && !mutation.isPending) { setVerifying(null); setCode(''); } }} title={t('验证通知渠道', 'Verify delivery channel')}><p>{verifying?.address}</p>{verifying?.provider === 'email' ? <><button className="button" disabled={mutation.isPending} onClick={() => void run(async () => { await api(base + `/endpoints/${verifying.id}/verify`, { method: 'POST', body: JSON.stringify({ code: '' }) }); }, t('验证码已发送，请检查收件箱', 'Verification code sent. Check your inbox'))}>{t('发送验证码', 'Send verification code')}</button><form className="security-form" onSubmit={e => { e.preventDefault(); const value = code; setCode(''); void run(async () => { await api(base + `/endpoints/${verifying.id}/verify`, { method: 'POST', body: JSON.stringify({ code: value }) }); setVerifying(null); }); }}><label>{t('邮件中的验证码', 'Code from your email')}<input required autoComplete="one-time-code" value={code} onChange={e => setCode(e.target.value)} /></label><button className="button primary" disabled={mutation.isPending || !code.trim()}>{t('确认验证', 'Verify')}</button></form></> : <><p className="muted">{t('接收地址需要返回正确的所有权验证响应。', 'Your receiver must return the expected ownership challenge response.')}</p><button className="button primary" disabled={mutation.isPending} onClick={() => void run(async () => { await api(base + `/endpoints/${verifying?.id}/verify`, { method: 'POST', body: JSON.stringify({ code: '' }) }); setVerifying(null); })}>{t('开始验证', 'Verify receiver')}</button></>}</Dialog>
    <Dialog open={!!subscription} onOpenChange={open => { if (!open && !mutation.isPending) { setSubscription(null); setRule(blankSubscription()); } }} title={t('通知偏好', 'Notification preference')}><form className="security-form" onSubmit={saveRule}><label>{t('通知事件', 'Event')}<select required value={rule.event_type} disabled={subscription !== 'new'} onChange={e => setRule({ ...rule, event_type: e.target.value })}><option value="">{t('选择事件', 'Select event')}</option>{config.event_types.map(value => <option value={value} key={value}>{eventLabel(value, t)}</option>)}</select></label><label>{t('接收渠道', 'Destination')}<select value={rule.endpoint_id} disabled={subscription !== 'new'} onChange={e => setRule({ ...rule, endpoint_id: e.target.value })}><option value="">{t('站内通知', 'Inbox')}</option>{config.endpoints.filter(endpoint => endpoint.enabled && endpoint.verified || String(endpoint.id) === rule.endpoint_id).map(endpoint => <option value={endpoint.id} key={endpoint.id}>{endpoint.name}</option>)}</select></label><label>{t('最低通知级别', 'Minimum severity')}<select value={rule.min_severity} onChange={e => setRule({ ...rule, min_severity: e.target.value })}><option value="info">{t('全部', 'All')}</option><option value="warning">{t('警告及以上', 'Warning and above')}</option><option value="error">{t('错误及以上', 'Error and above')}</option><option value="critical">{t('仅严重', 'Critical only')}</option></select></label><label>{t('指定仓库（可选）', 'Repository (optional)')}<input placeholder="owner/repository" disabled={subscription !== 'new'} value={rule.repo_name} onChange={e => setRule({ ...rule, repo_name: e.target.value })} /></label><details><summary>{t('限定特定资源', 'Limit to a specific resource')}</summary><div className="security-form"><label>{t('资源类型', 'Resource type')}<input disabled={subscription !== 'new'} value={rule.resource_type} onChange={e => setRule({ ...rule, resource_type: e.target.value })} /></label><label>{t('资源编号', 'Resource ID')}<input disabled={subscription !== 'new'} value={rule.resource_id} onChange={e => setRule({ ...rule, resource_id: e.target.value })} /></label></div></details><label className="notification-check"><input type="checkbox" checked={rule.enabled} onChange={e => setRule({ ...rule, enabled: e.target.checked })} />{t('接收此通知', 'Receive this notification')}</label><button className="button primary" disabled={mutation.isPending || !rule.event_type}>{t('保存偏好', 'Save preference')}</button></form></Dialog>
    <Dialog open={!!binding} onOpenChange={open => { if (!open) setBinding(null); }} title={t('在 Telegram 中完成绑定', 'Finish connecting in Telegram')}><p>{t('请使用自己的 Telegram 私聊完成绑定。', 'Complete the binding in a private chat using your own Telegram account.')}</p>{safeExternalUrl(binding?.deep_link) ? <a className="button primary" href={safeExternalUrl(binding?.deep_link)} target="_blank" rel="noopener noreferrer">{t('打开 Telegram', 'Open Telegram')}</a> : <code className="notification-token">/start {binding?.token}</code>}<p className="muted">{t('有效期至 ', 'Expires ')}<Timestamp value={binding?.expires_at} /></p><button className="button" onClick={() => { setBinding(null); void query.refetch(); }}>{t('已完成绑定，刷新', 'Connected, refresh')}</button></Dialog>
    <Dialog open={!!deliveryEndpoint} onOpenChange={open => { if (!open) setDeliveryEndpoint(null); }} title={t('发送记录', 'Delivery history')}>{deliveryEndpoint && <DeliveryHistory endpoint={deliveryEndpoint} />}</Dialog>
  </div>;
}

function DeliveryHistory({ endpoint }: { endpoint: Endpoint }) {
  const { t } = useLocale();
  const query = useQuery({ queryKey: ['notification-deliveries', endpoint.id], queryFn: () => api<{ deliveries: Array<{ id: number; status: string; category: string | null; attempts: number; sent_at: string | null }> }>(base + `/endpoints/${endpoint.id}/status`) });
  if (query.isPending) return <LoadingState />;
  if (query.isError) return <ErrorState retry={() => query.refetch()} />;
  return <div><p>{endpoint.name}</p>{query.data.deliveries.length === 0 ? <EmptyState title={t('暂无发送记录', 'No delivery history')} /> : query.data.deliveries.map(item => <div className="security-row" key={item.id}><Bell size={18} /><div><strong>{item.status}</strong><p>{item.category ?? ''} · {t("尝试 {0} 次", "{0} attempts", [item.attempts])}</p></div><Timestamp value={item.sent_at} /></div>)}</div>;
}
