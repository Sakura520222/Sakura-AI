import type { ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useLocale } from "../../app/locale";
import { ApiError } from "../../api/client";
import { Pagination } from "../../components/workflow/Workflow";
import type { OffsetPage, Source } from "./billingTypes";

export function useBillingPage() {
  const [params] = useSearchParams();
  const raw = Number(params.get("page") || 1);
  const page = Number.isSafeInteger(raw) && raw > 0 ? raw : 1;
  return { params, page, offset: (page - 1) * 20, limit: 20 };
}
export function OffsetPagination({ data }: { data: OffsetPage<unknown> }) {
  const page = Math.floor(data.offset / data.limit) + 1;
  const totalPages =
    data.total == null
      ? page + (data.has_more ? 1 : 0)
      : Math.max(1, Math.ceil(data.total / data.limit));
  return (
    <Pagination
      data={{
        page,
        total: data.total ?? data.offset + data.items.length,
        total_pages: totalPages,
      }}
    />
  );
}
export function SourceLink({ source }: { source: Source }) {
  const { t } = useLocale();
  const route = source.review_id
    ? `/reviews/${source.review_id}`
    : source.issue_analysis_id
      ? `/issues/${source.issue_analysis_id}`
      : source.order_id
        ? `/billing/orders/${source.order_id}`
        : null;
  if (route)
    return (
      <Link to={route}>{source.repo || t("查看记录", "View record")}</Link>
    );
  if (source.url?.startsWith("/") && !source.url.startsWith("//"))
    return (
      <a href={source.url}>{source.repo || t("查看记录", "View record")}</a>
    );
  return <>—</>;
}
export function Money({
  amount,
  currency,
  minor,
}: {
  amount: string | null;
  currency: string;
  minor?: number;
}) {
  const { t } = useLocale();
  return (
    <span className="wf-number">
      {currency}{" "}
      {amount ?? `${minor ?? "—"} · ${t("单位未知", "unknown units")}`}
    </span>
  );
}
export function Field({
  label,
  children,
  help,
}: {
  label: string;
  children: ReactNode;
  help?: string;
}) {
  return (
    <label className="billing-field">
      <span>{label}</span>
      {children}
      {help && <small>{help}</small>}
    </label>
  );
}
export function MutationError({ error }: { error: unknown }) {
  const { t } = useLocale();
  if (!error) return null;
  let message = t(
    "操作未完成。请检查当前状态后再重试。",
    "The operation did not complete. Check the current state before trying again.",
  );
  if (error instanceof ApiError) {
    const body = error.body as {
      detail?: string | { message?: string };
      error?: string;
    } | null;
    const detail = body?.detail;
    message =
      typeof detail === "string"
        ? detail
        : detail && !Array.isArray(detail) && typeof detail.message === "string"
          ? detail.message
          : typeof body?.error === "string"
            ? body.error
            : message;
  }
  return (
    <div role="alert" className="wf-notice">
      {message}
    </div>
  );
}
