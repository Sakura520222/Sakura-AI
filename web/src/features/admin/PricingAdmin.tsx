import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "../../api/client";
import { useLocale } from "../../app/locale";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  Section,
  StatusBadge,
  Timestamp,
} from "../../components/workflow/Workflow";
import type { Envelope } from "../../components/workflow/types";
import {
  Field,
  Money,
  MutationError,
  OffsetPagination,
  useBillingPage,
} from "../billing/BillingCommon";
import {
  parseIntegerInput,
  validateCreditInput,
  type OffsetPage,
  type Plan,
} from "../billing/billingTypes";

interface Account {
  id: string;
  name: string;
  provider_id: string;
  models: string[];
  default_model: string;
  enabled: boolean;
}
interface Auxiliary {
  feature: string;
  provider_id: string;
  model_id: string;
}
interface PricingContext {
  accounts: Account[];
  auxiliary: Auxiliary[];
  call_kinds: string[];
  units: string[];
  currencies: string[];
}
interface Price {
  id: number;
  account_id: string | null;
  provider_id: string;
  model_id: string;
  call_kind: string;
  version: number;
  config: Record<string, string | boolean>;
  created_at: string;
}
interface PaymentEvent {
  id: number;
  provider: string;
  type: string | null;
  event_key: string;
  status: string;
  pending_reason: string | null;
  order_id: number | null;
  order_no: string | null;
  amount_cents: number | null;
  currency: string | null;
  formatted_amount: string | null;
  received_at: string;
}

export default function PricingAdmin() {
  const { t } = useLocale();
  const { offset, limit } = useBillingPage();
  const [editing, setEditing] = useState<Price | undefined>();
  const context = useQuery({
    queryKey: ["pricing-context"],
    queryFn: ({ signal }) =>
      api<PricingContext>("/api/v1/billing/admin/pricing-context", { signal }),
  });
  const prices = useQuery({
    queryKey: ["prices", offset],
    queryFn: ({ signal }) =>
      api<OffsetPage<Price>>(
        `/api/v1/billing/admin/pricing?offset=${offset}&limit=${limit}`,
        { signal },
      ),
  });
  if (context.isPending) return <LoadingState />;
  if (context.isError || !context.data)
    return <ErrorState retry={() => void context.refetch()} />;
  return (
    <>
      <Section
        title={
          editing
            ? t("基于版本 {0} 新建报价", "New price from version {0}", [editing.version])
            : t("发布模型报价", "Publish model pricing")
        }
      >
        <PriceEditor
          key={editing?.id ?? "new"}
          context={context.data}
          price={editing}
          onComplete={() => {
            setEditing(undefined);
            void prices.refetch();
          }}
        />
      </Section>
      <Section title={t("价格版本", "Price versions")}>
        {prices.isPending ? (
          <LoadingState />
        ) : prices.isError ? (
          <ErrorState retry={() => void prices.refetch()} />
        ) : (
          prices.data && (
            <>
              {prices.data.items.length ? (
                <div className="wf-table-wrap">
                  <table className="wf-table">
                    <thead>
                      <tr>
                        <th>{t("账号 / 模型", "Account / model")}</th>
                        <th>{t("调用类型", "Call kind")}</th>
                        <th>{t("版本", "Version")}</th>
                        <th>{t("创建时间", "Created")}</th>
                        <th>{t("操作", "Action")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {prices.data.items.map((price) => (
                        <tr key={price.id}>
                          <td>
                            <strong>{price.model_id}</strong>
                            <div className="wf-record-meta">
                              {price.account_id ||
                                t("独立定价", "Unbound pricing")}{" "}
                              · {price.provider_id}
                            </div>
                          </td>
                          <td>{price.call_kind}</td>
                          <td>{price.version}</td>
                          <td>
                            <Timestamp value={price.created_at} />
                          </td>
                          <td>
                            <button
                              className="wf-button"
                              onClick={() => {
                                setEditing(price);
                                window.scrollTo({
                                  top: 0,
                                  behavior: "instant",
                                });
                              }}
                            >
                              {t("载入为新版本", "Use for new version")}
                            </button>
                            <details>
                              <summary>{t("查看报价", "View pricing")}</summary>
                              <pre style={{ maxWidth: 420, overflow: "auto" }}>
                                {JSON.stringify(price.config, null, 2)}
                              </pre>
                            </details>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <EmptyState title={t("暂无报价", "No pricing configured")} />
              )}
              <OffsetPagination data={prices.data} />
            </>
          )
        )}
      </Section>
      <Payments context={context.data} />
      <GrantAndAdjustment />
    </>
  );
}

function PriceEditor({
  context,
  price,
  onComplete,
}: {
  context: PricingContext;
  price?: Price;
  onComplete: () => void;
}) {
  const { t } = useLocale();
  const client = useQueryClient();
  const confirm = useConfirm();
  const [source, setSource] = useState(
    price?.account_id
      ? `account:${price.account_id}`
      : price
        ? `auxiliary:${price.call_kind}`
        : "",
  );
  const [model, setModel] = useState(price?.model_id ?? "");
  const [kind, setKind] = useState(
    price?.call_kind === "chat_stream" ? "chat" : (price?.call_kind ?? "chat"),
  );
  const [unit, setUnit] = useState(String(price?.config.unit ?? "tokens"));
  const [advanced, setAdvanced] = useState(false);
  const [invalid, setInvalid] = useState(false);
  const account = context.accounts.find(
    (item) => `account:${item.id}` === source,
  );
  const auxiliary = context.auxiliary.find(
    (item) => `auxiliary:${item.feature}` === source,
  );
  const models = useQuery({
    queryKey: ["pricing-models", account?.id],
    queryFn: ({ signal }) =>
      api<Envelope<{ models: string[]; discovery_failed: boolean }>>(
        `/api/v1/billing/admin/pricing/accounts/${encodeURIComponent(account?.id ?? "")}/models`,
        { signal },
      ),
    enabled: Boolean(account?.enabled),
    staleTime: 60_000,
  });
  const refreshModels = useMutation({
    mutationFn: () =>
      api<Envelope<{ models: string[]; discovery_failed: boolean }>>(
        `/api/v1/billing/admin/pricing/accounts/${encodeURIComponent(account?.id ?? "")}/models?refresh=true`,
      ),
    onSuccess: (data) =>
      client.setQueryData(["pricing-models", account?.id], data),
  });
  const modelChoices = [
    ...new Set([
      ...(account?.models ?? []),
      ...(models.data?.data.models ?? []),
      ...(price && price.account_id === account?.id ? [price.model_id] : []),
    ]),
  ];
  const mutation = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api("/api/v1/billing/admin/pricing", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: () => {
      toast.success(t("新价格版本已发布", "New price version published"));
      onComplete();
    },
  });
  const rateFields: Array<[string, string]> =
    unit === "tokens"
      ? [
          ["input_price", t("输入 / 百万 Token", "Input / million tokens")],
          ["output_price", t("输出 / 百万 Token", "Output / million tokens")],
          [
            "cached_input_price",
            t(
              "缓存读取 / 百万 Token（可选）",
              "Cache read / million tokens (optional)",
            ),
          ],
          [
            "cache_creation_price",
            t(
              "缓存创建 / 百万 Token（可选）",
              "Cache write / million tokens (optional)",
            ),
          ],
          [
            "reasoning_price",
            t(
              "推理 / 百万 Token（可选）",
              "Reasoning / million tokens (optional)",
            ),
          ],
        ]
      : [["unit_price", t("每单位价格", "Price per unit")]];
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      let config: Record<string, unknown>;
      if (advanced) {
        config = JSON.parse(String(form.get("config")));
        if (
          !config ||
          Array.isArray(config) ||
          typeof config !== "object" ||
          Object.values(config).some((value) => typeof value === "number")
        )
          throw new Error("invalid");
      } else {
        config = { unit };
        for (const key of [
          "currency",
          "settlement_currency",
          "fx_rate",
          "markup",
          "credits_per_currency_unit",
          ...rateFields.map(([key]) => key),
        ]) {
          const value = String(form.get(key) ?? "").trim();
          if (value) config[key] = value;
        }
        if (unit !== "tokens") config.meter = String(form.get("meter"));
        else
          for (const key of [
            "cache_read_supported",
            "cache_creation_supported",
            "reasoning_supported",
          ]) {
            const value = String(form.get(key) ?? "");
            if (value) config[key] = value === "true";
          }
      }
      if ((!account?.enabled && !auxiliary) || (!auxiliary && !model))
        throw new Error("invalid");
      const body = {
        account_id: account?.id ?? null,
        provider_id: account?.provider_id ?? auxiliary?.provider_id,
        model_id: auxiliary?.model_id ?? model,
        call_kind: auxiliary?.feature ?? kind,
        config,
      };
      if (
        await confirm({
          title: t("发布新价格版本？", "Publish this price version?"),
          description: t(
            "后续调用使用新版本；已有调用与历史账单继续使用冻结的报价。",
            "Future calls use the new version. Existing calls and historical bills keep their frozen prices.",
          ),
          confirmLabel: t("发布", "Publish"),
        })
      )
        mutation.mutate(body);
      setInvalid(false);
    } catch {
      setInvalid(true);
    }
  }
  function changeSource(value: string) {
    setSource(value);
    const selectedAccount = context.accounts.find(
      (item) => `account:${item.id}` === value,
    );
    const selectedAuxiliary = context.auxiliary.find(
      (item) => `auxiliary:${item.feature}` === value,
    );
    setModel(
      selectedAuxiliary?.model_id ?? selectedAccount?.default_model ?? "",
    );
    if (selectedAuxiliary) setKind(selectedAuxiliary.feature);
  }
  return (
    <form className="billing-form" onSubmit={(event) => void submit(event)}>
      <p className="wf-muted">
        {t(
          "所有价格和换算参数需明确填写。发布会新增版本，不覆盖历史报价。",
          "Enter prices and conversion parameters explicitly. Publishing creates a new version and preserves historical pricing.",
        )}
      </p>
      <div className="billing-form-grid">
        <Field label={t("计费来源", "Billing source")}>
          <select
            required
            value={source}
            onChange={(event) => changeSource(event.target.value)}
          >
            <option value="">
              {t(
                "选择账号或独立服务",
                "Choose an account or auxiliary service",
              )}
            </option>
            {context.accounts.map((item) => (
              <option
                key={item.id}
                value={`account:${item.id}`}
                disabled={!item.enabled}
              >
                {item.name} · {item.provider_id}
                {!item.enabled && ` (${t("已停用", "Disabled")})`}
              </option>
            ))}
            {context.auxiliary.map((item) => (
              <option key={item.feature} value={`auxiliary:${item.feature}`}>
                {item.feature} · {item.provider_id}/{item.model_id}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t("模型", "Model")}>
          <select
            required
            disabled={Boolean(auxiliary)}
            value={auxiliary?.model_id ?? model}
            onChange={(event) => setModel(event.target.value)}
          >
            <option value="">{t("选择模型", "Choose model")}</option>
            {auxiliary ? (
              <option value={auxiliary.model_id}>{auxiliary.model_id}</option>
            ) : (
              modelChoices.map((value) => <option key={value}>{value}</option>)
            )}
          </select>
        </Field>
        <Field label={t("调用类型", "Call kind")}>
          <select
            value={auxiliary?.feature ?? kind}
            disabled={Boolean(auxiliary)}
            onChange={(event) => setKind(event.target.value)}
          >
            {context.call_kinds.map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        </Field>
        <Field label={t("计量单位", "Usage unit")}>
          <select
            value={unit}
            onChange={(event) => setUnit(event.target.value)}
          >
            {context.units.map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        </Field>
      </div>
      {account && (
        <div className="wf-actions">
          <span className="wf-muted">
            {models.isFetching
              ? t("正在获取模型…", "Loading models…")
              : models.isError || models.data?.data.discovery_failed
                ? t(
                    "发现失败，保留已保存模型",
                    "Discovery failed; saved models remain available",
                  )
                : t("模型已更新", "Models updated")}
          </span>
          <button
            className="wf-button"
            type="button"
            disabled={models.isFetching || refreshModels.isPending}
            onClick={() => refreshModels.mutate()}
          >
            {t("刷新模型", "Refresh models")}
          </button>
        </div>
      )}
      <label>
        <input
          type="checkbox"
          checked={advanced}
          onChange={(event) => setAdvanced(event.target.checked)}
        />{" "}
        {t("高级 JSON 编辑", "Advanced JSON editor")}
      </label>
      {advanced ? (
        <Field
          label={t(
            "价格配置（金额使用字符串）",
            "Pricing configuration (quote decimal amounts)",
          )}
        >
          <textarea
            name="config"
            className="billing-json"
            defaultValue={price ? JSON.stringify(price.config, null, 2) : ""}
            required
          />
        </Field>
      ) : (
        <div className="billing-form-grid">
          {[
            ["currency", t("成本币种", "Provider currency")],
            ["settlement_currency", t("结算币种", "Settlement currency")],
          ].map(([key, label]) => (
            <Field key={key} label={label}>
              <select
                name={key}
                required
                defaultValue={
                  context.currencies.includes(String(price?.config[key] ?? ""))
                    ? String(price?.config[key])
                    : ""
                }
              >
                <option value="">{t("选择币种", "Choose currency")}</option>
                {context.currencies.map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </Field>
          ))}
          {[
            ["fx_rate", t("汇率", "FX rate")],
            ["markup", t("价格倍率", "Markup multiplier")],
            [
              "credits_per_currency_unit",
              t(
                "每结算货币单位的 Credits",
                "Credits per settlement currency unit",
              ),
            ],
            ...rateFields,
          ].map(([key, label]) => (
            <Field key={key} label={label}>
              <input
                name={key}
                inputMode="decimal"
                required={
                  ![
                    "cached_input_price",
                    "cache_creation_price",
                    "reasoning_price",
                  ].includes(key)
                }
                defaultValue={String(price?.config[key] ?? "")}
              />
            </Field>
          ))}
          {unit === "tokens" ? (
            [
              ["cache_read_supported", t("缓存读取能力", "Cache read support")],
              [
                "cache_creation_supported",
                t("缓存创建能力", "Cache write support"),
              ],
              ["reasoning_supported", t("推理能力", "Reasoning support")],
            ].map(([key, label]) => (
              <Field key={key} label={label}>
                <select
                  name={key}
                  defaultValue={
                    price?.config[key] == null ? "" : String(price.config[key])
                  }
                >
                  <option value="">
                    {t("沿用用量语义", "Use reported usage semantics")}
                  </option>
                  <option value="true">{t("支持", "Supported")}</option>
                  <option value="false">{t("不支持", "Not supported")}</option>
                </select>
              </Field>
            ))
          ) : (
            <Field label={t("实际用量字段", "Reported usage meter")}>
              <select
                name="meter"
                defaultValue={String(price?.config.meter ?? unit)}
              >
                {context.units
                  .filter((value) => value !== "tokens")
                  .map((value) => (
                    <option key={value}>{value}</option>
                  ))}
              </select>
            </Field>
          )}
        </div>
      )}
      {invalid && (
        <p role="alert">
          {t(
            "检查计费来源和模型；JSON 金额必须用引号包围的十进制字符串。",
            "Check the billing source and model. JSON amounts must be quoted decimal strings.",
          )}
        </p>
      )}
      <MutationError error={mutation.error} />
      <MutationError error={refreshModels.error} />
      <button
        className="wf-button wf-button-primary"
        disabled={mutation.isPending}
      >
        {t("发布新版本", "Publish new version")}
      </button>
    </form>
  );
}

function Payments({ context }: { context: PricingContext }) {
  const { t } = useLocale();
  const { offset, limit } = useBillingPage();
  const query = useQuery({
    queryKey: ["payment-events", offset],
    queryFn: ({ signal }) =>
      api<OffsetPage<PaymentEvent>>(
        `/api/v1/billing/admin/payment-events?offset=${offset}&limit=${limit}`,
        { signal },
      ),
  });
  return (
    <Section
      title={t("待核对的支付事件", "Payment events requiring reconciliation")}
    >
      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState retry={() => void query.refetch()} />
      ) : (
        query.data && (
          <>
            {query.data.items.length ? (
              query.data.items.map((event) => (
                <PaymentCard
                  key={event.id}
                  event={event}
                  currencies={context.currencies}
                  onComplete={() => void query.refetch()}
                />
              ))
            ) : (
              <EmptyState
                title={t(
                  "没有待核对的事件",
                  "No events awaiting reconciliation",
                )}
              />
            )}
            <OffsetPagination data={query.data} />
          </>
        )
      )}
    </Section>
  );
}
function PaymentCard({
  event,
  currencies,
  onComplete,
}: {
  event: PaymentEvent;
  currencies: string[];
  onComplete: () => void;
}) {
  const { t } = useLocale();
  const confirm = useConfirm();
  const [invalid, setInvalid] = useState(false);
  const mutation = useMutation({
    mutationFn: ({ action, body }: { action: string; body?: unknown }) =>
      api(`/api/v1/billing/admin/payment-events/${event.id}/${action}`, {
        method: "POST",
        body: body ? JSON.stringify(body) : undefined,
      }),
    retry: false,
    onSuccess: onComplete,
  });
  async function resolve(submitEvent: FormEvent<HTMLFormElement>) {
    submitEvent.preventDefault();
    const form = new FormData(submitEvent.currentTarget);
    try {
      const body: Record<string, unknown> = {};
      for (const [key, value] of form)
        if (value)
          body[key] =
            key.endsWith("_cents") || key === "order_id"
              ? parseIntegerInput(String(value), 1)
              : String(value);
      if (
        await confirm({
          title: t("提交核对证据？", "Submit reconciliation evidence?"),
          description: t(
            "请确保金额、币种和支付来源已经核实。该操作可能完成入账或退款。",
            "Verify the amounts, currencies and payment source. This may complete a payment or refund.",
          ),
        })
      )
        mutation.mutate({ action: "resolve", body });
      setInvalid(false);
    } catch {
      setInvalid(true);
    }
  }
  async function replay() {
    if (
      await confirm({
        title: t("重新处理支付事件？", "Replay this payment event?"),
        description: t(
          "将使用已保存的证据和现有幂等规则重新处理。",
          "The saved evidence will be processed using the existing idempotency rules.",
        ),
      })
    )
      mutation.mutate({ action: "replay" });
  }
  return (
    <div
      className="billing-form"
      style={{ borderBottom: "1px solid var(--border)" }}
    >
      <strong>
        {event.provider} · #{event.id}
      </strong>
      <div className="wf-actions">
        <StatusBadge value={event.status} />
        <Money
          amount={event.formatted_amount}
          currency={event.currency ?? "—"}
          minor={event.amount_cents ?? undefined}
        />
        <Timestamp value={event.received_at} />
      </div>
      <p>
        {event.order_no} · {event.pending_reason}
      </p>
      <MutationError error={mutation.error} />
      <button
        className="wf-button"
        disabled={mutation.isPending}
        onClick={() => void replay()}
      >
        {t("重新处理", "Replay event")}
      </button>
      <details style={{ width: "100%" }}>
        <summary>{t("提交核对证据", "Submit reconciliation evidence")}</summary>
        <form
          className="billing-form"
          onSubmit={(submitEvent) => void resolve(submitEvent)}
        >
          <Field label={t("核实证据", "Verified evidence")}>
            <textarea name="evidence" required maxLength={2000} />
          </Field>
          <div className="billing-form-grid">
            {[
              ["order_id", t("订单 ID", "Order ID")],
              [
                "checkout_amount_cents",
                t("支付金额（最小货币单位）", "Checkout amount (minor units)"),
              ],
              ["refund_reference_id", t("退款凭据 ID", "Refund reference ID")],
              [
                "refund_amount_cents",
                t("退款金额（最小货币单位）", "Refund amount (minor units)"),
              ],
            ].map(([key, label]) => (
              <Field key={key} label={label}>
                <input
                  name={key}
                  inputMode={key === "refund_reference_id" ? "text" : "numeric"}
                  defaultValue={
                    key === "order_id" ? (event.order_id ?? "") : ""
                  }
                />
              </Field>
            ))}
            {[
              ["checkout_currency", t("支付币种", "Checkout currency")],
              ["refund_currency", t("退款币种", "Refund currency")],
            ].map(([key, label]) => (
              <Field key={key} label={label}>
                <select name={key}>
                  <option value="">{t("未提供", "Not provided")}</option>
                  {currencies.map((value) => (
                    <option key={value}>{value}</option>
                  ))}
                </select>
              </Field>
            ))}
          </div>
          {invalid && (
            <p role="alert">
              {t(
                "金额与 ID 必须是正整数。",
                "Amounts and IDs must be positive integers.",
              )}
            </p>
          )}
          <button
            className="wf-button wf-button-primary"
            disabled={mutation.isPending}
          >
            {t("提交核对", "Submit evidence")}
          </button>
        </form>
      </details>
    </div>
  );
}

function GrantAndAdjustment() {
  const { t } = useLocale();
  const confirm = useConfirm();
  const [grantKey, setGrantKey] = useState(
    () => `webui-grant:${crypto.randomUUID()}`,
  );
  const [adjustKey, setAdjustKey] = useState(
    () => `webui-adjust:${crypto.randomUUID()}`,
  );
  const [invalid, setInvalid] = useState(false);
  const plans = useQuery({
    queryKey: ["admin-plans"],
    queryFn: ({ signal }) =>
      api<Plan[]>("/api/v1/billing/admin/plans", { signal }),
  });
  const mutation = useMutation({
    mutationFn: ({ path, body }: { path: string; body: unknown }) =>
      api(path, { method: "POST", body: JSON.stringify(body) }),
    retry: false,
    onSuccess: (_, input) => {
      if (input.path.endsWith("/grant"))
        setGrantKey(`webui-grant:${crypto.randomUUID()}`);
      else setAdjustKey(`webui-adjust:${crypto.randomUUID()}`);
      toast.success(t("操作已完成", "Operation completed"));
    },
  });
  async function submit(event: FormEvent<HTMLFormElement>, grant: boolean) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      const userId = parseIntegerInput(String(form.get("user_id")), 1);
      const body = grant
        ? {
            user_id: userId,
            plan_id: parseIntegerInput(String(form.get("plan_id")), 1),
            idempotency_key: grantKey,
          }
        : {
            credits: validateCreditInput(String(form.get("credits")), true),
            reason: String(form.get("reason")),
            idempotency_key: adjustKey,
          };
      if (
        await confirm({
          title: grant
            ? t("发放套餐？", "Grant this plan?")
            : t("调整用户余额？", "Adjust this balance?"),
          description: t(
            "变更会记入财务流水。请确认用户和金额。",
            "This change is recorded in the financial ledger. Confirm the user and amount.",
          ),
          destructive: true,
        })
      )
        mutation.mutate({
          path: grant
            ? "/api/v1/billing/admin/grant"
            : `/api/v1/billing/admin/wallets/${userId}/adjust`,
          body,
        });
      setInvalid(false);
    } catch {
      setInvalid(true);
    }
  }
  return (
    <>
      <MutationError error={mutation.error} />
      {invalid && (
        <p role="alert" className="wf-notice">
          {t(
            "检查用户 ID 和金额（Credits 最多六位小数）。",
            "Check the user ID and amount (Credits allow up to six decimal places).",
          )}
        </p>
      )}
      <Section title={t("发放套餐", "Grant a plan")}>
        <form
          className="billing-form"
          onSubmit={(event) => void submit(event, true)}
        >
          <div className="billing-form-grid">
            <Field label={t("用户 ID", "User ID")}>
              <input name="user_id" inputMode="numeric" required />
            </Field>
            <Field label={t("套餐", "Plan")}>
              <select name="plan_id" required>
                <option value="">{t("选择套餐", "Choose plan")}</option>
                {plans.data
                  ?.filter((plan) => plan.is_active)
                  .map((plan) => (
                    <option key={plan.id} value={plan.id}>
                      {plan.name}
                    </option>
                  ))}
              </select>
            </Field>
          </div>
          <button className="wf-button" disabled={mutation.isPending}>
            {t("发放", "Grant")}
          </button>
        </form>
      </Section>
      <Section title={t("余额调整", "Balance adjustment")}>
        <form
          className="billing-form"
          onSubmit={(event) => void submit(event, false)}
        >
          <div className="billing-form-grid">
            <Field label={t("用户 ID", "User ID")}>
              <input name="user_id" inputMode="numeric" required />
            </Field>
            <Field
              label={t("Credits 变动量", "Credits adjustment")}
              help={t(
                "正数增加，负数扣减。",
                "Positive adds Credits; negative deducts Credits.",
              )}
            >
              <input name="credits" inputMode="decimal" required />
            </Field>
          </div>
          <Field label={t("调整原因", "Reason")}>
            <textarea name="reason" maxLength={1000} required />
          </Field>
          <button className="wf-button" disabled={mutation.isPending}>
            {t("确认调整", "Apply adjustment")}
          </button>
        </form>
      </Section>
    </>
  );
}
