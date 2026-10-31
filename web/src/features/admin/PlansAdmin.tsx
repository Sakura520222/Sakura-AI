import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
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
} from "../../components/workflow/Workflow";
import { Field, Money, MutationError } from "../billing/BillingCommon";
import {
  exactCredits,
  moneyToMinor,
  parseIntegerInput,
  validateCreditInput,
  type BillingContext,
  type Plan,
} from "../billing/billingTypes";

export default function PlansAdmin() {
  const { t } = useLocale();
  const confirm = useConfirm();
  const [editing, setEditing] = useState<Plan | "new" | null>(null);
  const query = useQuery({
    queryKey: ["admin-plans"],
    queryFn: ({ signal }) =>
      api<Plan[]>("/api/v1/billing/admin/plans", { signal }),
  });
  const selection = useRowSelection(query.data?.map(plan => plan.id) ?? []);
  const mutation = useMutation({
    mutationFn: ({
      id,
      active,
      remove,
    }: {
      id: number;
      active?: boolean;
      remove?: boolean;
    }) =>
      api(`/api/v1/billing/admin/plans/${id}`, {
        method: remove ? "DELETE" : "PUT",
        body: remove ? undefined : JSON.stringify({ is_active: active }),
      }),
    onSuccess: () => void query.refetch(),
  });
  async function remove(plan: Plan) {
    if (
      await confirm({
        title: t("删除套餐 {0}？", "Delete plan {0}?", [plan.name]),
        description: t(
          "已有订单和已购买权益将保留。",
          "Existing orders and purchased benefits will be retained.",
        ),
        destructive: true,
      })
    )
      mutation.mutate({ id: plan.id, remove: true });
  }
  return (
    <>
      <MutationError error={mutation.error} />
      <BulkActions domain="plans" selected={selection.selected} onClear={selection.clear} onComplete={ids => { selection.remove(ids); void query.refetch(); }} />
      <Section
        title={t("套餐与价格", "Plans and prices")}
        aside={
          <button className="wf-button" onClick={() => setEditing("new")}>
            <Plus size={16} />
            {t("新建套餐", "New plan")}
          </button>
        }
      >
        {query.isPending ? (
          <LoadingState />
        ) : query.isError ? (
          <ErrorState retry={() => void query.refetch()} />
        ) : query.data?.length ? (
          <div className="wf-table-wrap">
            <table className="wf-table">
              <thead>
                <tr>
                  <th className="billing-select-cell"><SelectRows total={query.data.length} selected={selection.selected.length} onChange={selection.toggleAll} /></th>
                  <th>{t("套餐", "Plan")}</th>
                  <th>{t("价格", "Price")}</th>
                  <th>Credits</th>
                  <th>{t("状态", "Status")}</th>
                  <th>{t("操作", "Actions")}</th>
                </tr>
              </thead>
              <tbody>
                {query.data.map((plan) => (
                  <tr key={plan.id}>
                    <td className="billing-select-cell"><input type="checkbox" aria-label={t('选择套餐 {0}', 'Select plan {0}', [plan.name])} checked={selection.selected.includes(plan.id)} onChange={() => selection.toggle(plan.id)} /></td>
                    <td>
                      <strong>{plan.name}</strong>
                      <div className="wf-record-meta">
                        {plan.plan_type === "subscription"
                          ? `${t("订阅", "Subscription")} · ${plan.duration_days} ${t("天", "days")}`
                          : t("一次性", "One-time")}
                      </div>
                    </td>
                    <td>
                      <Money
                        amount={plan.formatted_price}
                        currency={plan.currency}
                        minor={plan.price_cents}
                      />
                    </td>
                    <td>{exactCredits(plan.credit_grant)}</td>
                    <td>
                      {plan.is_active
                        ? t("已启用", "Active")
                        : t("已停用", "Disabled")}
                    </td>
                    <td>
                      <div className="billing-record-actions">
                        <button
                          className="wf-button"
                          onClick={() => setEditing(plan)}
                        >
                          {t("编辑", "Edit")}
                        </button>
                        <button
                          className="wf-button"
                          disabled={mutation.isPending}
                          onClick={() =>
                            mutation.mutate({
                              id: plan.id,
                              active: !plan.is_active,
                            })
                          }
                        >
                          {plan.is_active
                            ? t("停用", "Disable")
                            : t("启用", "Enable")}
                        </button>
                        <button
                          className="wf-button wf-button-quiet"
                          disabled={mutation.isPending}
                          onClick={() => void remove(plan)}
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
          <EmptyState title={t("暂无套餐", "No plans yet")} />
        )}
      </Section>
      <Dialog
        open={editing !== null}
        onOpenChange={(open) => {
          if (!open) setEditing(null);
        }}
        title={
          editing === "new"
            ? t("新建套餐", "New plan")
            : t("编辑套餐", "Edit plan")
        }
      >
        {editing && (
          <PlanEditor
            key={editing === "new" ? "new" : editing.id}
            plan={editing === "new" ? undefined : editing}
            onComplete={() => {
              setEditing(null);
              void query.refetch();
            }}
          />
        )}
      </Dialog>
    </>
  );
}

function PlanEditor({
  plan,
  onComplete,
}: {
  plan?: Plan;
  onComplete: () => void;
}) {
  const { t } = useLocale();
  const client = useQueryClient();
  const [currency, setCurrency] = useState(plan?.currency ?? "");
  const [type, setType] = useState(plan?.plan_type ?? "one_time");
  const [invalid, setInvalid] = useState(false);
  const context = useQuery({
    queryKey: ["billing-context"],
    queryFn: ({ signal }) =>
      api<BillingContext>("/api/v1/billing/context", { signal }),
  });
  const mutation = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api(`/api/v1/billing/admin/plans${plan ? `/${plan.id}` : ""}`, {
        method: plan ? "PUT" : "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["plans"] });
      onComplete();
    },
  });
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      const body: Record<string, unknown> = {
        name: String(form.get("name")),
        plan_type: type,
        currency,
        price_cents: moneyToMinor(
          String(form.get("amount")),
          context.data?.currency_minor_exponents[currency] ?? -1,
        ),
        credit_grant: validateCreditInput(String(form.get("credit_grant"))),
        duration_days:
          type === "subscription"
            ? parseIntegerInput(String(form.get("duration_days")), 1)
            : null,
        description: String(form.get("description")),
        sort_order: parseIntegerInput(String(form.get("sort_order"))),
        concurrency_limit: form.get("concurrency_limit")
          ? parseIntegerInput(String(form.get("concurrency_limit")), 1)
          : null,
      };
      const rates: Record<string, number> = {};
      for (const [key, value] of form) {
        if (key.startsWith("rate.") && value)
          rates[key.slice(5)] = parseIntegerInput(String(value));
        if (key.endsWith("_add") || key.endsWith("_bonus"))
          body[key] = parseIntegerInput(String(value));
      }
      body.rate_limits = Object.keys(rates).length ? rates : null;
      mutation.mutate(body);
      setInvalid(false);
    } catch {
      setInvalid(true);
    }
  }
  if (context.isPending) return <LoadingState />;
  if (context.isError)
    return <ErrorState retry={() => void context.refetch()} />;
  return (
    <form className="billing-form" onSubmit={submit}>
      <div className="billing-form-grid">
        <Field label={t("套餐名称", "Plan name")}>
          <input
            name="name"
            required
            maxLength={100}
            defaultValue={plan?.name ?? ""}
          />
        </Field>
        <Field label={t("类型", "Type")}>
          <select
            value={type}
            onChange={(event) => setType(event.target.value)}
          >
            <option value="one_time">{t("一次性", "One-time")}</option>
            <option value="subscription">{t("订阅", "Subscription")}</option>
          </select>
        </Field>
        <Field label={t("币种", "Currency")}>
          <select
            required
            value={context.data?.currencies.includes(currency) ? currency : ""}
            onChange={(event) => setCurrency(event.target.value)}
          >
            <option value="">{t("选择币种", "Choose currency")}</option>
            {context.data?.currencies.map((code) => (
              <option key={code}>{code}</option>
            ))}
          </select>
        </Field>
        <Field
          label={t("价格", "Price")}
          help={
            currency
              ? `${currency} · ${context.data?.currency_minor_exponents[currency] ?? "?"} ${t("位小数", "decimal places")}`
              : undefined
          }
        >
          <input
            name="amount"
            inputMode="decimal"
            required
            defaultValue={plan?.formatted_price ?? ""}
          />
        </Field>
        <Field label={t("发放 Credits", "Credits granted")}>
          <input
            name="credit_grant"
            inputMode="decimal"
            required
            defaultValue={plan?.credit_grant ?? "0"}
          />
        </Field>
        {type === "subscription" && (
          <Field label={t("有效天数", "Duration in days")}>
            <input
              name="duration_days"
              type="number"
              min="1"
              step="1"
              required
              defaultValue={plan?.duration_days ?? ""}
            />
          </Field>
        )}
        <Field label={t("并发限制（可选）", "Concurrency limit (optional)")}>
          <input
            name="concurrency_limit"
            type="number"
            min="1"
            step="1"
            defaultValue={plan?.concurrency_limit ?? ""}
          />
        </Field>
        <Field label={t("显示顺序", "Display order")}>
          <input
            name="sort_order"
            type="number"
            min="0"
            step="1"
            required
            defaultValue={plan?.sort_order ?? 0}
          />
        </Field>
      </div>
      <Field label={t("描述", "Description")}>
        <textarea name="description" defaultValue={plan?.description ?? ""} />
      </Field>
      <details style={{ width: "100%" }}>
        <summary>
          {t("限流与历史配额权益", "Rate limits and legacy quota benefits")}
        </summary>
        <div className="billing-form-grid">
          {[
            "pr_daily",
            "pr_weekly",
            "pr_monthly",
            "issue_daily",
            "issue_weekly",
            "issue_monthly",
            "agent_daily",
            "agent_weekly",
            "agent_monthly",
            "repo_scan_daily",
          ].map((key) => (
            <Field key={key} label={key}>
              <input
                name={`rate.${key}`}
                type="number"
                min="0"
                step="1"
                defaultValue={plan?.rate_limits?.[key] ?? ""}
              />
            </Field>
          ))}
          {["pr", "issue", "agent"]
            .flatMap((feature) =>
              ["quota_bonus", "daily_add", "weekly_add", "monthly_add"].map(
                (period) => `${feature}_${period}`,
              ),
            )
            .map((key) => (
              <Field key={key} label={key}>
                <input
                  name={key}
                  type="number"
                  min="0"
                  step="1"
                  defaultValue={String(plan?.[key as keyof Plan] ?? 0)}
                />
              </Field>
            ))}
        </div>
      </details>
      {invalid && (
        <p role="alert">
          {t(
            "请检查金额精度、Credits 和整数限制。",
            "Check currency precision, Credits and integer limits.",
          )}
        </p>
      )}
      <MutationError error={mutation.error} />
      <button
        className="wf-button wf-button-primary"
        disabled={mutation.isPending}
      >
        {t("保存套餐", "Save plan")}
      </button>
    </form>
  );
}
