import { useState, type FormEvent } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Copy, Plus } from "lucide-react";
import { toast } from "sonner";
import { api } from "../../api/client";
import { useLocale } from "../../app/locale";
import { Dialog } from "../../components/ui/Dialog";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import { BulkActions, SelectRows, useRowSelection } from './BulkActions';
import {
  EmptyState,
  ErrorState,
  LoadingState,
  Section,
  Timestamp,
} from "../../components/workflow/Workflow";
import {
  Field,
  MutationError,
  OffsetPagination,
  useBillingPage,
} from "../billing/BillingCommon";
import {
  parseIntegerInput,
  type OffsetPage,
  type Plan,
} from "../billing/billingTypes";

interface CodeRow {
  id: number;
  code: string;
  plan_id: number;
  plan_name: string | null;
  batch_name: string | null;
  status: string;
  used_count: number;
  max_uses: number;
  expires_at: string | null;
}
export default function CodesAdmin() {
  const { t } = useLocale();
  const confirm = useConfirm();
  const { offset, limit } = useBillingPage();
  const [editing, setEditing] = useState<CodeRow | "new" | null>(null);
  const [generated, setGenerated] = useState<string[]>([]);
  const query = useQuery({
    queryKey: ["codes", offset],
    queryFn: ({ signal }) =>
      api<OffsetPage<CodeRow>>(
        `/api/v1/billing/admin/codes?offset=${offset}&limit=${limit}`,
        { signal },
      ),
  });
  const selection = useRowSelection(query.data?.items.map(code => code.id) ?? []);
  const action = useMutation({
    mutationFn: ({
      id,
      status,
      remove,
    }: {
      id: number;
      status?: string;
      remove?: boolean;
    }) =>
      api(`/api/v1/billing/admin/codes/${id}`, {
        method: remove ? "DELETE" : "PUT",
        body: remove ? undefined : JSON.stringify({ status }),
      }),
    onSuccess: () => void query.refetch(),
  });
  async function remove(row: CodeRow) {
    if (
      await confirm({
        title: t("删除兑换码？", "Delete this code?"),
        description: t(
          "已使用的兑换码会由服务器保护。",
          "The server preserves codes linked to redemption history.",
        ),
        destructive: true,
      })
    )
      action.mutate({ id: row.id, remove: true });
  }
  return (
    <>
      <MutationError error={action.error} />
      <BulkActions domain="codes" selected={selection.selected} onClear={selection.clear} onComplete={ids => { selection.remove(ids); void query.refetch(); }} />
      {generated.length > 0 && (
        <Section title={t("新生成的兑换码", "Generated codes")}>
          <div className="billing-form">
            <pre style={{ whiteSpace: "pre-wrap" }}>{generated.join("\n")}</pre>
            <button
              className="wf-button"
              onClick={() =>
                void navigator.clipboard.writeText(generated.join("\n")).then(
                  () => toast.success(t("已复制", "Copied")),
                  () =>
                    toast.error(
                      t(
                        "复制失败，请手动复制",
                        "Copy failed; please copy manually",
                      ),
                    ),
                )
              }
            >
              <Copy size={16} />
              {t("复制全部", "Copy all")}
            </button>
          </div>
        </Section>
      )}
      <Section
        title={t("兑换码", "Redemption codes")}
        aside={
          <button className="wf-button" onClick={() => setEditing("new")}>
            <Plus size={16} />
            {t("生成兑换码", "Generate codes")}
          </button>
        }
      >
        {query.isPending ? (
          <LoadingState />
        ) : query.isError ? (
          <ErrorState retry={() => void query.refetch()} />
        ) : (
          query.data && (
            <>
              {query.data.items.length ? (
                <div className="wf-table-wrap">
                  <table className="wf-table">
                    <thead>
                      <tr>
                        <th className="billing-select-cell"><SelectRows total={query.data.items.length} selected={selection.selected.length} onChange={selection.toggleAll} /></th>
                        <th>{t("兑换码", "Code")}</th>
                        <th>{t("套餐", "Plan")}</th>
                        <th>{t("使用次数", "Usage")}</th>
                        <th>{t("状态", "Status")}</th>
                        <th>{t("到期时间", "Expires")}</th>
                        <th>{t("操作", "Actions")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {query.data.items.map((row) => (
                        <tr key={row.id}>
                          <td className="billing-select-cell"><input type="checkbox" aria-label={t('选择兑换码 #{0}', 'Select code #{0}', [row.id])} checked={selection.selected.includes(row.id)} onChange={() => selection.toggle(row.id)} /></td>
                          <td>
                            <code>{row.code}</code>
                            <div className="wf-record-meta">
                              {row.batch_name}
                            </div>
                          </td>
                          <td>{row.plan_name}</td>
                          <td>
                            {row.used_count} / {row.max_uses}
                          </td>
                          <td>{row.status}</td>
                          <td>
                            <Timestamp value={row.expires_at} />
                          </td>
                          <td>
                            <div className="billing-record-actions">
                              <button
                                className="wf-button"
                                onClick={() => setEditing(row)}
                              >
                                {t("编辑", "Edit")}
                              </button>
                              {row.status !== "exhausted" && (
                                <button
                                  className="wf-button"
                                  disabled={action.isPending}
                                  onClick={() =>
                                    action.mutate({
                                      id: row.id,
                                      status:
                                        row.status === "active"
                                          ? "disabled"
                                          : "active",
                                    })
                                  }
                                >
                                  {row.status === "active"
                                    ? t("停用", "Disable")
                                    : t("启用", "Enable")}
                                </button>
                              )}
                              <button
                                className="wf-button wf-button-quiet"
                                disabled={action.isPending}
                                onClick={() => void remove(row)}
                              >
                                {t("删除", "Delete")}
                              </button>
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <EmptyState title={t("暂无兑换码", "No codes yet")} />
              )}
              <OffsetPagination data={query.data} />
            </>
          )
        )}
      </Section>
      <Dialog
        open={editing !== null}
        onOpenChange={(open) => {
          if (!open) setEditing(null);
        }}
        title={
          editing === "new"
            ? t("生成兑换码", "Generate codes")
            : t("编辑兑换码", "Edit code")
        }
      >
        {editing && (
          <CodeEditor
            key={editing === "new" ? "new" : editing.id}
            code={editing === "new" ? undefined : editing}
            onComplete={(codes) => {
              if (codes) setGenerated(codes);
              setEditing(null);
              void query.refetch();
            }}
          />
        )}
      </Dialog>
    </>
  );
}
function CodeEditor({
  code,
  onComplete,
}: {
  code?: CodeRow;
  onComplete: (codes?: string[]) => void;
}) {
  const { t } = useLocale();
  const [invalid, setInvalid] = useState(false);
  const plans = useQuery({
    queryKey: ["admin-plans"],
    queryFn: ({ signal }) =>
      api<Plan[]>("/api/v1/billing/admin/plans", { signal }),
    enabled: !code,
  });
  const mutation = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api<{ codes?: string[] }>(
        `/api/v1/billing/admin/codes/${code ? code.id : "generate"}`,
        { method: code ? "PUT" : "POST", body: JSON.stringify(body) },
      ),
    onSuccess: (data) => onComplete(data.codes),
  });
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      const body: Record<string, unknown> = {
        max_uses: parseIntegerInput(String(form.get("max_uses")), 1),
      };
      if (code) {
        const expiry = String(form.get("expires_at") || "");
        if (expiry && !/(Z|[+-]\d{2}:\d{2})$/.test(expiry))
          throw new Error("timezone_required");
        body.expires_at = expiry ? new Date(expiry).toISOString() : null;
      } else {
        body.plan_id = parseIntegerInput(String(form.get("plan_id")), 1);
        body.count = parseIntegerInput(String(form.get("count")), 1);
        body.batch_name = String(form.get("batch_name"));
      }
      mutation.mutate(body);
      setInvalid(false);
    } catch {
      setInvalid(true);
    }
  }
  return (
    <form className="billing-form" onSubmit={submit}>
      {code ? <Field label={t('已发行的套餐', 'Issued plan')}><p>{code.plan_name || `#${code.plan_id}`}</p><small className="muted">{t('已发行兑换码的套餐快照保持不变。更换套餐请生成新兑换码。', 'An issued code retains its purchased offer. Generate a new code to use a different plan.')}</small></Field> : <Field label={t("套餐", "Plan")}>
        <select name="plan_id" defaultValue="" required>
          <option value="">{t("选择套餐", "Choose plan")}</option>
          {plans.data?.map((plan) => (
            <option key={plan.id} value={plan.id}>
              {plan.name}
              {!plan.is_active && ` (${t("已停用", "Disabled")})`}
            </option>
          ))}
        </select>
      </Field>}
      <Field label={t("最大使用次数", "Maximum uses")}>
        <input
          name="max_uses"
          type="number"
          min="1"
          required
          defaultValue={code?.max_uses ?? 1}
        />
      </Field>
      {code ? (
        <Field
          label={t(
            "到期时间（包含时区，留空为永不过期）",
            "Expiry (with timezone; blank for no expiry)",
          )}
        >
          <input
            name="expires_at"
            placeholder="2027-01-01T00:00:00+08:00"
            defaultValue={code.expires_at ?? ""}
          />
        </Field>
      ) : (
        <>
          <Field label={t("生成数量", "Number of codes")}>
            <input
              name="count"
              type="number"
              min="1"
              max="100"
              defaultValue={1}
              required
            />
          </Field>
          <Field label={t("批次名称", "Batch name")}>
            <input name="batch_name" maxLength={100} />
          </Field>
        </>
      )}
      {invalid && (
        <p role="alert">
          {t(
            "请检查数量和有效时间。",
            "Check the quantities and expiration date.",
          )}
        </p>
      )}
      <MutationError error={mutation.error} />
      <button
        className="wf-button wf-button-primary"
        disabled={mutation.isPending || (!code && (plans.isPending || plans.isError))}
      >
        {t("保存", "Save")}
      </button>
    </form>
  );
}
