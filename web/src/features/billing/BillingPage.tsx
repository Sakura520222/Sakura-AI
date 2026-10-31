import { useState, type FormEvent } from "react";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowUpRight,
  CreditCard,
  ExternalLink,
  Save,
} from "lucide-react";
import { Link, NavLink, Route, Routes, useParams } from "react-router-dom";
import { toast } from "sonner";
import { api } from "../../api/client";
import { useAuth } from "../../app/auth";
import { useLocale } from "../../app/locale";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import {
  EmptyState,
  ErrorState,
  FilterSelect,
  Filters,
  LoadingState,
  PageHeader,
  RefreshButton,
  Section,
  StatusBadge,
  Timestamp,
  safeExternalUrl,
} from "../../components/workflow/Workflow";
import {
  Field,
  Money,
  MutationError,
  OffsetPagination,
  SourceLink,
  useBillingPage,
} from "./BillingCommon";
import type {
  BillingContext,
  OffsetPage,
  Operation,
  Order,
  Plan,
  RefundRequest,
  Transaction,
  Wallet,
} from "./billingTypes";
import { exactCredits, validateCreditInput } from "./billingTypes";
import "./billing.css";
import InvoicePage from "./InvoicePage";

export default function BillingPage() {
  const { t } = useLocale();
  const { features, permissions } = useAuth();
  return (
    <div>
      <PageHeader
        title={t("账单与用量", "Billing & usage")}
        description={t(
          "查看余额、每笔支出和历史订单。",
          "Your balance, spending and order history in one place.",
        )}
        actions={
          permissions.super_admin && (
            <Link className="wf-button" to="/admin/pricing">
              {t("管理计费", "Manage billing")}
              <ArrowUpRight size={16} />
            </Link>
          )
        }
      />
      <nav
        className="billing-tabs"
        aria-label={t("账单导航", "Billing navigation")}
      >
        <NavLink to="/billing" end>
          {t("钱包", "Wallet")}
        </NavLink>
        <NavLink to="/billing/transactions">
          {t("流水", "Transactions")}
        </NavLink>
        <NavLink to="/billing/operations">
          {t("任务账单", "Operations")}
        </NavLink>
        <NavLink to="/billing/usage">{t("模型用量", "Model usage")}</NavLink>
        <NavLink to="/billing/benefits">
          {t("配额权益", "Quota benefits")}
        </NavLink>
        <NavLink to="/billing/orders">{t("订单", "Orders")}</NavLink>
        {features.payment_enabled && (
          <NavLink to="/billing/plans">{t("套餐", "Plans")}</NavLink>
        )}
      </nav>
      <Routes>
        <Route index element={<WalletPanel />} />
        <Route path="credits" element={<WalletPanel />} />
        <Route path="transactions" element={<TransactionPanel />} />
        <Route path="operations" element={<OperationPanel />} />
        <Route path="usage" element={<UsagePanel />} />
        <Route path="benefits" element={<BenefitsPanel />} />
        <Route path="notices" element={<NoticesPanel />} />
        <Route path="orders" element={<OrdersPanel />} />
        <Route path="orders/:id" element={<OrderPanel />} />
        <Route path="plans" element={<PlansPanel />} />
        <Route path="invoice/:orderNo" element={<InvoicePage />} />
        <Route path="result" element={<InvoicePage />} />
        <Route
          path="*"
          element={<EmptyState title={t("页面不存在", "Page not found")} />}
        />
      </Routes>
    </div>
  );
}

function WalletPanel() {
  const { t } = useLocale();
  const client = useQueryClient();
  const [threshold, setThreshold] = useState<string | null>(null);
  const [invalid, setInvalid] = useState(false);
  const wallet = useQuery({
    queryKey: ["wallet"],
    queryFn: ({ signal }) => api<Wallet>("/api/v1/billing/wallet", { signal }),
  });
  const update = useMutation({
    mutationFn: (credits: string) =>
      api<Wallet>("/api/v1/billing/wallet/threshold", {
        method: "PUT",
        body: JSON.stringify({ credits }),
      }),
    onSuccess: (data) => {
      client.setQueryData(["wallet"], data);
      setThreshold(null);
      toast.success(t("余额提醒已更新", "Balance alert updated"));
    },
  });
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      update.mutate(
        validateCreditInput(
          threshold ?? wallet.data?.low_balance_threshold ?? "",
        ),
      );
      setInvalid(false);
    } catch {
      setInvalid(true);
    }
  }
  if (wallet.isPending) return <LoadingState />;
  if (wallet.isError || !wallet.data)
    return <ErrorState retry={() => void wallet.refetch()} />;
  const data = wallet.data;
  return (
    <>
      <div className="wallet-balance">
        <div>
          <span>{t("可用余额", "Available balance")}</span>
          <strong>
            {exactCredits(data.available)} <small>Credits</small>
          </strong>
          {data.low_balance && (
            <p className="wallet-low">
              {t(
                "余额已低于提醒阈值",
                "Your balance is below the alert threshold",
              )}
            </p>
          )}
        </div>
        <CreditCard size={32} />
        <div className="wallet-breakdown">
          <div>
            <span>{t("账面余额", "Balance")}</span>
            <strong>{exactCredits(data.balance)}</strong>
          </div>
          <div>
            <span>{t("预留中", "Reserved")}</span>
            <strong>{exactCredits(data.reserved)}</strong>
          </div>
        </div>
      </div>
      <Section
        title={t("余额提醒", "Low balance alert")}
        aside={
          <Link to="/billing/notices">{t("历史提醒", "Past alerts")}</Link>
        }
      >
        <form className="billing-form" onSubmit={submit}>
          <p className="wf-muted">
            {t(
              "可用余额到达这个值时，通过你的通知偏好提醒。设置为 0 可关闭提醒。",
              "Receive an alert through your notification preferences when the available balance reaches this amount. Set 0 to disable.",
            )}
          </p>
          <Field label={t("提醒阈值（Credits）", "Alert threshold (Credits)")}>
            <input
              inputMode="decimal"
              required
              value={threshold ?? data.low_balance_threshold}
              onChange={(event) => setThreshold(event.target.value)}
            />
          </Field>
          {invalid && (
            <p role="alert">
              {t(
                "请输入非负数，最多六位小数。",
                "Enter a nonnegative amount with at most six decimal places.",
              )}
            </p>
          )}
          <MutationError error={update.error} />
          <button
            className="wf-button wf-button-primary"
            disabled={update.isPending}
          >
            <Save size={16} />
            {t("保存", "Save")}
          </button>
        </form>
      </Section>
    </>
  );
}

function FeatureFilter() {
  const { t } = useLocale();
  return (
    <FilterSelect
      name="feature"
      label={t("全部功能", "All features")}
      options={[
        ["pr_review", "PR Review"],
        ["issue_analysis", "Issue"],
        ["agent", "Agent"],
        ["repo_scan", t("仓库扫描", "Repository scan")],
      ]}
    />
  );
}

interface UsageRow {
  operation_id: string | null;
  provider_id: string;
  model_id: string;
  call_kind: string;
  feature: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cached_input_tokens: number | null;
  cache_creation_tokens: number | null;
  reasoning_tokens: number | null;
  calls: number;
  unknown_calls: number;
  incomplete_calls: number;
  last_occurred_at: string | null;
}
function UsagePanel() {
  const { t } = useLocale();
  const { params, offset, limit } = useBillingPage();
  const qs = new URLSearchParams({
    offset: String(offset),
    limit: String(limit),
  });
  for (const key of ["feature", "operation_id"])
    if (params.get(key)) qs.set(key, params.get(key)!);
  const query = useQuery({
    queryKey: ["billing-usage", qs.toString()],
    queryFn: ({ signal }) =>
      api<OffsetPage<UsageRow>>(`/api/v1/billing/usage?${qs}`, { signal }),
  });
  return (
    <>
      <Filters search={false} placeholder="">
        <FeatureFilter />
        <label className="wf-search">
          <input
            name="operation_id"
            aria-label={t("任务编号", "Operation ID")}
            placeholder={t("任务编号", "Operation ID")}
            defaultValue={params.get("operation_id") ?? ""}
          />
        </label>
      </Filters>
      <Section title={t("实际模型用量", "Actual model usage")}>
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
                        <th>{t("模型", "Model")}</th>
                        <th>{t("调用数", "Calls")}</th>
                        <th>
                          {t("输入 / 输出 Token", "Input / output tokens")}
                        </th>
                        <th>{t("用量完整性", "Usage completeness")}</th>
                        <th>{t("时间", "Date")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {query.data.items.map((row, index) => (
                        <tr key={`${row.operation_id}-${index}`}>
                          <td>
                            {row.model_id}
                            <div className="wf-record-meta">
                              {row.provider_id} · {row.call_kind}
                            </div>
                            <details>
                              <summary>
                                {t("更多计量信息", "More usage details")}
                              </summary>
                              <dl>
                                <dt>{t("缓存读取", "Cache read")}</dt>
                                <dd>{row.cached_input_tokens ?? "—"}</dd>
                                <dt>{t("缓存创建", "Cache write")}</dt>
                                <dd>{row.cache_creation_tokens ?? "—"}</dd>
                                <dt>{t("推理 Token", "Reasoning tokens")}</dt>
                                <dd>{row.reasoning_tokens ?? "—"}</dd>
                                <dt>{t("任务编号", "Operation ID")}</dt>
                                <dd>{row.operation_id ?? "—"}</dd>
                              </dl>
                            </details>
                          </td>
                          <td>{row.calls}</td>
                          <td>
                            {row.input_tokens ?? "—"} /{" "}
                            {row.output_tokens ?? "—"}
                          </td>
                          <td>
                            {row.unknown_calls > 0 || row.incomplete_calls > 0
                              ? t(
                                  "{0} 次未报告，{1} 次不完整",
                                  "{0} unreported, {1} incomplete",
                                  [row.unknown_calls, row.incomplete_calls],
                                )
                              : t("完整", "Complete")}
                          </td>
                          <td>
                            <Timestamp value={row.last_occurred_at} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <EmptyState title={t("暂无用量记录", "No usage records")} />
              )}
              <OffsetPagination data={query.data} />
            </>
          )
        )}
      </Section>
    </>
  );
}

interface Entitlements {
  features: Record<
    string,
    Array<{ period: string; limit: number; used: number }>
  >;
  legacy_remaining: Record<string, number>;
}
function BenefitsPanel() {
  const { t } = useLocale();
  const query = useQuery({
    queryKey: ["entitlements"],
    queryFn: ({ signal }) =>
      api<Entitlements>("/api/v1/billing/entitlements", { signal }),
  });
  if (query.isPending) return <LoadingState />;
  if (query.isError || !query.data)
    return <ErrorState retry={() => void query.refetch()} />;
  const periods: Record<string, string> = {
    daily: t("每日", "Daily"),
    weekly: t("每周", "Weekly"),
    monthly: t("每月", "Monthly"),
  };
  return (
    <>
      <Section title={t("当前配额权益", "Current quota benefits")}>
        <div className="wf-table-wrap">
          <table className="wf-table">
            <thead>
              <tr>
                <th>{t("功能", "Feature")}</th>
                <th>{t("周期", "Period")}</th>
                <th>{t("已使用", "Used")}</th>
                <th>{t("有效配额", "Effective limit")}</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(query.data.features).flatMap(([feature, rows]) =>
                rows.map((row) => (
                  <tr key={`${feature}-${row.period}`}>
                    <td>
                      {feature === "pr_review"
                        ? "PR Review"
                        : feature === "issue_analysis"
                          ? "Issue"
                          : "Agent"}
                    </td>
                    <td>{periods[row.period] ?? row.period}</td>
                    <td>{row.used}</td>
                    <td>{row.limit}</td>
                  </tr>
                )),
              )}
            </tbody>
          </table>
        </div>
      </Section>
      <Section title={t("剩余历史权益", "Remaining legacy benefits")}>
        <dl className="wf-detail-pairs">
          {Object.entries(query.data.legacy_remaining).map(
            ([feature, count]) => (
              <div key={feature}>
                <dt>{feature}</dt>
                <dd>{count}</dd>
              </div>
            ),
          )}
        </dl>
      </Section>
    </>
  );
}

interface Notice {
  id: number;
  kind: string;
  created_at: string;
  read_at: string | null;
}
function NoticesPanel() {
  const { t } = useLocale();
  const { offset, limit } = useBillingPage();
  const query = useQuery({
    queryKey: ["billing-notices", offset],
    queryFn: ({ signal }) =>
      api<OffsetPage<Notice>>(
        `/api/v1/billing/notices?offset=${offset}&limit=${limit}`,
        { signal },
      ),
  });
  const mutation = useMutation({
    mutationFn: (id: number) =>
      api(`/api/v1/billing/notices/${id}/read`, { method: "POST" }),
    onSuccess: () => void query.refetch(),
  });
  return (
    <>
      <MutationError error={mutation.error} />
      <Section title={t("历史余额提醒", "Historical balance alerts")}>
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
                        <th>{t("类型", "Type")}</th>
                        <th>{t("时间", "Date")}</th>
                        <th>{t("状态", "Status")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {query.data.items.map((notice) => (
                        <tr key={notice.id}>
                          <td>{notice.kind}</td>
                          <td>
                            <Timestamp value={notice.created_at} />
                          </td>
                          <td>
                            {notice.read_at ? (
                              t("已读", "Read")
                            ) : (
                              <button
                                className="wf-button"
                                disabled={mutation.isPending}
                                onClick={() => mutation.mutate(notice.id)}
                              >
                                {t("标为已读", "Mark read")}
                              </button>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <EmptyState title={t("暂无历史提醒", "No historical alerts")} />
              )}
              <OffsetPagination data={query.data} />
            </>
          )
        )}
      </Section>
    </>
  );
}
function TransactionPanel() {
  const { t } = useLocale();
  const { params, offset, limit } = useBillingPage();
  const qs = new URLSearchParams({
    offset: String(offset),
    limit: String(limit),
  });
  for (const key of ["feature", "kind"])
    if (params.get(key)) qs.set(key, params.get(key)!);
  const query = useQuery({
    queryKey: ["transactions", qs.toString()],
    queryFn: ({ signal }) =>
      api<OffsetPage<Transaction>>(`/api/v1/billing/transactions?${qs}`, {
        signal,
      }),
    placeholderData: keepPreviousData,
  });
  return (
    <>
      <Filters
        search={false}
        placeholder={t("筛选流水", "Filter transactions")}
      >
        <FeatureFilter />
        <FilterSelect
          name="kind"
          label={t("全部类型", "All types")}
          options={[
            ["purchase", t("购买", "Purchase")],
            ["grant", t("发放", "Grant")],
            ["consumption", t("消耗", "Consumption")],
            ["refund", t("退款", "Refund")],
            ["adjustment", t("调整", "Adjustment")],
            ["migration", t("迁移", "Migration")],
          ]}
        />
      </Filters>
      <Section
        title={t("Credits 流水", "Credit transactions")}
        aside={
          <RefreshButton
            busy={query.isFetching}
            onClick={() => void query.refetch()}
          />
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
                        <th>{t("类型", "Type")}</th>
                        <th>Credits</th>
                        <th>{t("来源", "Source")}</th>
                        <th>{t("时间", "Date")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {query.data.items.map((row) => (
                        <tr key={row.id}>
                          <td>
                            {row.kind}
                            <div className="wf-record-meta">{row.feature}</div>
                          </td>
                          <td className="wf-number">
                            {exactCredits(row.credits)}
                          </td>
                          <td>
                            <SourceLink source={row.source} />
                          </td>
                          <td>
                            <Timestamp value={row.created_at} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <EmptyState title={t("暂无流水", "No transactions")} />
              )}
              <OffsetPagination data={query.data} />
            </>
          )
        )}
      </Section>
    </>
  );
}
function OperationPanel() {
  const { t } = useLocale();
  const { params, offset, limit } = useBillingPage();
  const qs = new URLSearchParams({
    offset: String(offset),
    limit: String(limit),
  });
  for (const key of ["feature", "status"])
    if (params.get(key)) qs.set(key, params.get(key)!);
  const query = useQuery({
    queryKey: ["operations", qs.toString()],
    queryFn: ({ signal }) =>
      api<OffsetPage<Operation>>(`/api/v1/billing/operations?${qs}`, {
        signal,
      }),
    placeholderData: keepPreviousData,
  });
  return (
    <>
      <Filters
        search={false}
        placeholder={t("筛选任务账单", "Filter operations")}
      >
        <FeatureFilter />
        <FilterSelect
          name="status"
          label={t("全部状态", "All statuses")}
          options={[
            ["settled", t("已结算", "Settled")],
            ["pending_pricing", t("等待定价", "Pending pricing")],
            ["pending_reconciliation", t("等待核对", "Pending reconciliation")],
            ["running", t("运行中", "Running")],
          ]}
        />
      </Filters>
      <Section title={t("任务账单", "Operation charges")}>
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
                        <th>{t("任务", "Operation")}</th>
                        <th>{t("状态", "Status")}</th>
                        <th>{t("已结算 Credits", "Settled Credits")}</th>
                        <th>{t("预留 Credits", "Reserved Credits")}</th>
                        <th>{t("时间", "Date")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {query.data.items.map((row) => (
                        <tr key={row.operation_id}>
                          <td>
                            <SourceLink source={row.source} />
                            <div className="wf-record-meta">{row.feature}</div>
                            <details>
                              <summary className="wf-muted">
                                {t("任务编号", "Operation ID")}
                              </summary>
                              <code>{row.operation_id}</code>
                            </details>
                          </td>
                          <td>
                            <StatusBadge value={row.status} />
                            <div className="wf-record-meta">{row.outcome}</div>
                          </td>
                          <td className="wf-number">
                            {exactCredits(row.settled_credits)}
                          </td>
                          <td className="wf-number">
                            {exactCredits(row.reserved_credits)}
                          </td>
                          <td>
                            <Timestamp value={row.created_at} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <EmptyState title={t("暂无账单", "No operation charges")} />
              )}
              <OffsetPagination data={query.data} />
            </>
          )
        )}
      </Section>
    </>
  );
}

function OrdersPanel() {
  const { t } = useLocale();
  const { offset, limit } = useBillingPage();
  const query = useQuery({
    queryKey: ["orders", offset],
    queryFn: ({ signal }) =>
      api<{ orders: Order[]; total: number }>(
        `/api/v1/billing/orders?offset=${offset}&limit=${limit}`,
        { signal },
      ),
    placeholderData: keepPreviousData,
  });
  return (
    <Section
      title={t("订单历史", "Order history")}
      aside={
        <RefreshButton
          busy={query.isFetching}
          onClick={() => void query.refetch()}
        />
      }
    >
      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState retry={() => void query.refetch()} />
      ) : (
        query.data && (
          <>
            {query.data.orders.length ? (
              <div className="wf-table-wrap">
                <table className="wf-table">
                  <thead>
                    <tr>
                      <th>{t("订单", "Order")}</th>
                      <th>{t("金额", "Amount")}</th>
                      <th>{t("状态", "Status")}</th>
                      <th>{t("创建时间", "Created")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {query.data.orders.map((order) => (
                      <tr key={order.id}>
                        <td>
                          <Link
                            className="wf-record-title"
                            to={`/billing/orders/${order.id}`}
                          >
                            {order.plan_name || order.order_no}
                          </Link>
                          <div className="wf-record-meta">
                            {order.order_no} · {order.payment_provider}
                          </div>
                        </td>
                        <td>
                          <Money
                            amount={order.formatted_amount}
                            currency={order.currency}
                            minor={order.amount_cents}
                          />
                        </td>
                        <td>
                          <StatusBadge value={order.status} />
                        </td>
                        <td>
                          <Timestamp value={order.created_at} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState title={t("暂无订单", "No orders yet")} />
            )}
            <OffsetPagination
              data={{ ...query.data, items: query.data.orders, offset, limit }}
            />
          </>
        )
      )}
    </Section>
  );
}

function OrderPanel() {
  const { id } = useParams();
  const { t } = useLocale();
  const confirm = useConfirm();
  const client = useQueryClient();
  const [reason, setReason] = useState("");
  const query = useQuery({
    queryKey: ["order", id],
    queryFn: ({ signal }) =>
      api<Order>(`/api/v1/billing/orders/${encodeURIComponent(id ?? "")}`, {
        signal,
      }),
  });
  const refund = useQuery({
    queryKey: ["order-refund", id],
    queryFn: ({ signal }) =>
      api<{ request: RefundRequest | null }>(
        `/api/v1/billing/orders/${encodeURIComponent(id ?? "")}/refund-requests`,
        { signal },
      ),
  });
  const mutation = useMutation({
    mutationFn: (action: string) =>
      api(`/api/v1/billing/orders/${encodeURIComponent(id ?? "")}/${action}`, {
        method: "POST",
        body:
          action === "refund-request" ? JSON.stringify({ reason }) : undefined,
      }),
    retry: false,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["order", id] });
      void client.invalidateQueries({ queryKey: ["order-refund", id] });
      void client.invalidateQueries({ queryKey: ["orders"] });
      toast.success(t("订单已更新", "Order updated"));
    },
  });
  async function act(action: string, label: string) {
    if (
      await confirm({
        title: label,
        description: t(
          "请确认对此订单执行该操作。历史交易记录会保留。",
          "Confirm this action for this order. Historical financial records will be retained.",
        ),
        confirmLabel: label,
        destructive: action !== "refund-request",
      })
    )
      mutation.mutate(action);
  }
  if (query.isPending) return <LoadingState />;
  if (query.isError || !query.data)
    return <ErrorState retry={() => void query.refetch()} />;
  const order = query.data;
  const checkout = safeExternalUrl(order.checkout_url);
  return (
    <>
      <Link className="wf-back" to="/billing/orders">
        <ArrowLeft size={15} />
        {t("所有订单", "All orders")}
      </Link>
      <Section
        title={order.order_no}
        aside={<StatusBadge value={order.status} />}
      >
        <dl className="wf-detail-pairs">
          <div>
            <dt>{t("订单金额", "Order amount")}</dt>
            <dd>
              <Money
                amount={order.formatted_amount}
                currency={order.currency}
                minor={order.amount_cents}
              />
            </dd>
          </div>
          <div>
            <dt>{t("已退款", "Refunded")}</dt>
            <dd>
              <Money
                amount={order.formatted_refunded_amount}
                currency={order.currency}
              />
            </dd>
          </div>
          <div>
            <dt>{t("支付方式", "Payment method")}</dt>
            <dd>{order.payment_provider}</dd>
          </div>
          <div>
            <dt>{t("创建时间", "Created")}</dt>
            <dd>
              <Timestamp value={order.created_at} />
            </dd>
          </div>
        </dl>
        <div className="billing-form">
          <MutationError error={mutation.error} />
          {order.status === "pending" && (
            <div className="wf-actions">
              {checkout && (
                <a
                  className="wf-button wf-button-primary"
                  href={checkout}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {t("继续支付", "Continue payment")}
                  <ExternalLink size={15} />
                </a>
              )}
              {order.payment_provider === "tron" && (
                <a
                  className="wf-button"
                  href={`/app/billing/invoice/${encodeURIComponent(order.order_no)}`}
                >
                  {t("查看转账信息", "View transfer details")}
                </a>
              )}
              <button
                className="wf-button"
                disabled={mutation.isPending}
                onClick={() =>
                  void act("cancel", t("取消订单", "Cancel order"))
                }
              >
                {t("取消订单", "Cancel order")}
              </button>
            </div>
          )}
          {order.status === "fulfilled" && (
            <>
              <Field label={t("退款申请原因", "Refund request reason")}>
                <textarea
                  maxLength={2000}
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                />
              </Field>
              <button
                className="wf-button"
                disabled={
                  mutation.isPending ||
                  refund.isPending ||
                  refund.isError ||
                  refund.data?.request?.status === "pending"
                }
                onClick={() =>
                  void act("refund-request", t("申请退款", "Request refund"))
                }
              >
                {t("申请退款", "Request refund")}
              </button>
            </>
          )}
          {["pending", "cancelled", "expired", "refunded"].includes(
            order.status,
          ) && (
            <button
              className="wf-button wf-button-quiet"
              disabled={mutation.isPending}
              onClick={() =>
                void act("hide", t("从订单列表隐藏", "Hide from order list"))
              }
            >
              {t("从订单列表隐藏", "Hide from order list")}
            </button>
          )}
        </div>
      </Section>
      {refund.isError && <ErrorState retry={() => void refund.refetch()} />}
      {refund.data?.request && (
        <Section title={t("退款申请", "Refund request")}>
          <div className="billing-form">
            <StatusBadge value={refund.data.request.status} />
            <p>{refund.data.request.reason}</p>
            {refund.data.request.review_note && (
              <p>{refund.data.request.review_note}</p>
            )}
          </div>
        </Section>
      )}
    </>
  );
}

function PlansPanel() {
  const { t } = useLocale();
  const { features } = useAuth();
  const confirm = useConfirm();
  const client = useQueryClient();
  const [code, setCode] = useState("");
  const [provider, setProvider] = useState("");
  const [checkoutOrder, setCheckoutOrder] = useState<number | null>(null);
  const context = useQuery({
    queryKey: ["billing-context"],
    queryFn: ({ signal }) =>
      api<BillingContext>("/api/v1/billing/context", { signal }),
  });
  const enabled = features.payment_enabled && context.data?.payment_enabled;
  const plans = useQuery({
    queryKey: ["plans"],
    queryFn: ({ signal }) => api<Plan[]>("/api/v1/billing/plans", { signal }),
    enabled: Boolean(enabled),
  });
  const purchase = useMutation({
    mutationFn: (planId: number) =>
      api<{ id: number }>("/api/v1/billing/orders", {
        method: "POST",
        body: JSON.stringify({ plan_id: planId, provider }),
      }),
    retry: false,
    onSuccess: (data) => {
      setCheckoutOrder(data.id);
      void client.invalidateQueries({ queryKey: ["orders"] });
    },
  });
  const redeem = useMutation({
    mutationFn: () =>
      api("/api/v1/billing/redeem", {
        method: "POST",
        body: JSON.stringify({ code }),
      }),
    retry: false,
    onSuccess: () => {
      setCode("");
      toast.success(t("兑换成功", "Code redeemed"));
      void client.invalidateQueries({ queryKey: ["wallet"] });
      void client.invalidateQueries({ queryKey: ["orders"] });
    },
  });
  async function buy(plan: Plan) {
    if (
      await confirm({
        title: t("购买 {0}？", "Purchase {0}?", [plan.name]),
        description: `${plan.currency} ${plan.formatted_price} · ${provider}`,
        confirmLabel: t("创建订单", "Create order"),
      })
    )
      purchase.mutate(plan.id);
  }
  if (context.isPending) return <LoadingState />;
  if (context.isError)
    return <ErrorState retry={() => void context.refetch()} />;
  if (!enabled)
    return (
      <EmptyState
        title={t("购买入口已关闭", "Purchases are currently unavailable")}
        description={t(
          "你仍然可以查看钱包、账单和已有订单。",
          "Your wallet, charges and existing orders remain available.",
        )}
      />
    );
  return (
    <>
      {checkoutOrder && (
        <div className="wf-notice" role="status">
          {t("订单已创建", "Order created")} ·{" "}
          <Link to={`/billing/orders/${checkoutOrder}`}>
            {t("前往支付", "Continue to payment")}
          </Link>
        </div>
      )}
      <MutationError error={purchase.error} />
      <Field label={t("支付方式", "Payment method")}>
        <select
          value={provider}
          onChange={(event) => setProvider(event.target.value)}
        >
          <option value="">
            {t("选择支付方式", "Choose a payment method")}
          </option>
          {context.data?.providers.map((item) => (
            <option key={item.id} value={item.id}>
              {item.label}
            </option>
          ))}
        </select>
      </Field>
      {plans.isPending ? (
        <LoadingState />
      ) : plans.isError ? (
        <ErrorState retry={() => void plans.refetch()} />
      ) : plans.data?.length ? (
        <div className="billing-plans">
          {plans.data.map((plan) => (
            <section key={plan.id}>
              <h2>{plan.name}</h2>
              <p>{plan.description}</p>
              <strong>
                <Money
                  amount={plan.formatted_price}
                  currency={plan.currency}
                  minor={plan.price_cents}
                />
              </strong>
              <span>{exactCredits(plan.credit_grant)} Credits</span>
              {plan.concurrency_limit != null && (
                <span>
                  {t("同时任务上限：{0}", "Concurrent task limit: {0}", [
                    plan.concurrency_limit,
                  ])}
                </span>
              )}
              {plan.duration_days && (
                <span>
                  {plan.duration_days} {t("天", "days")}
                </span>
              )}
              <button
                className="wf-button wf-button-primary"
                disabled={
                  !provider ||
                  purchase.isPending ||
                  plan.formatted_price == null
                }
                onClick={() => void buy(plan)}
              >
                {t("购买", "Purchase")}
              </button>
            </section>
          ))}
        </div>
      ) : (
        <EmptyState title={t("暂无可购买套餐", "No plans available")} />
      )}
      <Section title={t("兑换码", "Redeem a code")}>
        <form
          className="billing-form"
          onSubmit={(event) => {
            event.preventDefault();
            redeem.mutate();
          }}
        >
          <Field label={t("兑换码", "Redemption code")}>
            <input
              autoComplete="off"
              required
              value={code}
              onChange={(event) => setCode(event.target.value)}
            />
          </Field>
          <MutationError error={redeem.error} />
          <button className="wf-button" disabled={redeem.isPending}>
            {t("兑换", "Redeem")}
          </button>
        </form>
      </Section>
    </>
  );
}
