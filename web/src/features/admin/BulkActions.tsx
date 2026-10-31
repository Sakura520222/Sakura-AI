import { useEffect, useRef, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { components } from '../../api/generated';
import { useLocale } from '../../app/locale';
import { useConfirm } from '../../components/ui/ConfirmProvider';
import { MutationError } from '../billing/BillingCommon';

type Result = components['schemas']['BillingBatchResult'];
export function useRowSelection(visibleIds: number[]) {
  const [ids, setIds] = useState<number[]>([]);
  const selected = ids.filter(id => visibleIds.includes(id));
  return {
    selected,
    toggle: (id: number) => setIds(current => current.includes(id) ? current.filter(value => value !== id) : [...current, id]),
    toggleAll: () => setIds(current => selected.length === visibleIds.length ? current.filter(id => !visibleIds.includes(id)) : [...new Set([...current, ...visibleIds])]),
    remove: (completed: number[]) => setIds(current => current.filter(id => !completed.includes(id))),
    clear: () => setIds([]),
  };
}

export function SelectRows({ total, selected, onChange }: { total: number; selected: number; onChange: () => void }) {
  const { t } = useLocale();
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { if (input.current) input.current.indeterminate = selected > 0 && selected < total; }, [selected, total]);
  return <input ref={input} type="checkbox" aria-label={t('全选当前列表', 'Select all visible rows')} checked={total > 0 && selected === total} disabled={!total} onChange={onChange} />;
}

export function BulkActions({ domain, selected, onComplete, onClear }: { domain: 'plans' | 'codes'; selected: number[]; onComplete: (ids: number[]) => void; onClear: () => void }) {
  const { t } = useLocale();
  const confirm = useConfirm();
  const mutation = useMutation({ mutationFn: ({ action, ids }: { action: string; ids: number[] }) => api<Result>(`/api/v1/billing/admin/${domain}/batch/${action}`, { method: 'POST', body: JSON.stringify({ ids }) }), retry: false, onSuccess: data => onComplete(data.succeeded_ids) });
  const actions = domain === 'plans' ? [{ id: 'toggle', label: t('切换启停', 'Toggle enabled') }, { id: 'disable', label: t('停用', 'Disable') }, { id: 'delete', label: t('永久删除', 'Delete permanently') }] : [{ id: 'enable', label: t('启用', 'Enable') }, { id: 'disable', label: t('停用', 'Disable') }, { id: 'delete', label: t('删除', 'Delete') }];
  async function apply(action: string, label: string) {
    const ids = [...selected];
    if (await confirm({ title: t('对选中的 {0} 项执行「{1}」？', 'Apply “{1}” to {0} selected items?', [ids.length, label]), description: domain === 'plans' ? t('历史订单与已购买权益不会改变。仍被引用的套餐不能永久删除。', 'Historical orders and purchased benefits are retained. Referenced plans cannot be permanently deleted.') : t('已经使用的兑换码不能删除。操作结果会分别列出成功和未处理的项目。', 'Used redemption codes cannot be deleted. Applied and unapplied items are reported separately.'), confirmLabel: label, destructive: action === 'delete' })) mutation.mutate({ action, ids });
  }
  return <div className="billing-bulk">
    {!!selected.length && <div className="billing-bulk-toolbar"><strong>{t('已选 {0} 项', '{0} selected', [selected.length])}</strong>{actions.map(action => <button className={`wf-button ${action.id === 'delete' ? 'danger-text' : ''}`} key={action.id} disabled={mutation.isPending} onClick={() => void apply(action.id, action.label)}>{action.label}</button>)}<button className="wf-button wf-button-quiet" disabled={mutation.isPending} onClick={onClear}>{t('清除选择', 'Clear selection')}</button></div>}
    <MutationError error={mutation.error} />
    {mutation.data && <div className="billing-bulk-result" role="status"><p>{t('已处理 {0} 项，{1} 项未处理。', '{0} items applied; {1} items not applied.', [mutation.data.succeeded_ids.length, mutation.data.failed.length])}</p>{mutation.data.failed.length > 0 && <ul>{mutation.data.failed.map(item => <li key={item.id}>#{item.id} · {item.reason}</li>)}</ul>}</div>}
  </div>;
}
