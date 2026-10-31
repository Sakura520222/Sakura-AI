import { useState, type FormEvent, type ReactNode } from "react";
import { ArrowLeft, ArrowRight, RefreshCw, Search } from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useLocale } from "../../app/locale";
import type { PageData, Review } from "./types";
import "./workflow.css";
export { LoadingState, ErrorState, EmptyState } from "../common/states";

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="wf-page-header">
      <div>
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="wf-actions">{actions}</div>}
    </header>
  );
}

export function RefreshButton({
  onClick,
  busy,
}: {
  onClick: () => void;
  busy: boolean;
}) {
  const { t } = useLocale();
  return (
    <button className="wf-button" disabled={busy} onClick={onClick}>
      <RefreshCw size={16} className={busy ? "wf-spin" : undefined} />
      {t("刷新", "Refresh")}
    </button>
  );
}

const statusNames: Record<string, [string, string]> = {
  completed: ["已完成", "Completed"],
  failed: ["失败", "Failed"],
  reviewing: ["审查中", "Reviewing"],
  analyzing: ["分析中", "Analyzing"],
  pending: ["等待中", "Pending"],
  cancelled: ["已取消", "Cancelled"],
  approve: ["已通过", "Approved"],
  request_changes: ["需要修改", "Changes requested"],
  comment: ["已评论", "Commented"],
  skip: ["已跳过", "Skipped"],
  critical: ["严重", "Critical"],
  major: ["主要", "Major"],
  minor: ["次要", "Minor"],
  suggestion: ["建议", "Suggestion"],
  high: ["高", "High"],
  medium: ["中", "Medium"],
  low: ["低", "Low"],
  queued: ["已排队", "Queued"],
  planning: ["规划中", "Planning"],
  cloning: ["准备仓库", "Preparing repository"],
  editing: ["修改中", "Editing"],
  self_reviewing: ["自检中", "Self-reviewing"],
  validating: ["验证中", "Validating"],
  pushing: ["推送中", "Pushing"],
  pr_opened: ["已创建 Pull Request", "Pull request opened"],
  external_reviewing: ["外部审查中", "External review"],
  iterating: ["迭代中", "Iterating"],
  waiting_human: ["等待人工处理", "Awaiting human input"],
  candidate: ["候选", "Candidate"],
  abandoned: ["已放弃", "Abandoned"],
  running: ["运行中", "Running"],
  connected: ["已连接", "Connected"],
  connecting: ["连接中", "Connecting"],
  disconnected: ["已断开", "Disconnected"],
  reconnecting: ["正在重连", "Reconnecting"],
  active: ["已启用", "Active"],
  disabled: ["已停用", "Disabled"],
  paid: ["已支付", "Paid"],
  fulfilled: ["已履约", "Fulfilled"],
  refunded: ["已退款", "Refunded"],
  expired: ["已过期", "Expired"],
  approved: ["已批准", "Approved"],
  rejected: ["已拒绝", "Rejected"],
  settled: ["已结算", "Settled"],
  pending_pricing: ["等待定价", "Pending pricing"],
  pending_reconciliation: ["等待核对", "Pending reconciliation"],
  processed: ["已处理", "Processed"],
  processing: ["处理中", "Processing"],
  indexing: ["建立索引", "Indexing"],
  reporting: ["生成报告", "Reporting"],
  exhausted: ["已用尽", "Exhausted"],
};

export function StatusBadge({ value }: { value: string | null | undefined }) {
  const { t } = useLocale();
  const labels = statusNames[value ?? ""];
  const tone = ["failed", "critical"].includes(value ?? "")
    ? "danger"
    : ["completed", "approve"].includes(value ?? "")
      ? "success"
      : [
            "reviewing",
            "analyzing",
            "pending",
            "request_changes",
            "high",
            "major",
          ].includes(value ?? "")
        ? "active"
        : "neutral";
  return (
    <span className={`wf-status wf-status-${tone}`}>
      <span aria-hidden="true" />
      {labels ? t(...labels) : value || "—"}
    </span>
  );
}

export function Timestamp({ value }: { value: string | null | undefined }) {
  const { language, timezone } = useLocale();
  if (!value) return <>—</>;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return <>—</>;
  return (
    <time
      dateTime={value}
      title={date.toLocaleString(language, {
        timeZone: timezone,
        timeZoneName: "longOffset",
      })}
    >
      {new Intl.DateTimeFormat(language, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        timeZone: timezone,
      }).format(date)}
    </time>
  );
}

export function Pagination({
  data,
  busy = false,
}: {
  data: Pick<PageData<unknown>, "page" | "total_pages" | "total">;
  busy?: boolean;
}) {
  const { t, language } = useLocale();
  const [params, setParams] = useSearchParams();
  function go(page: number) {
    const next = new URLSearchParams(params);
    next.set("page", String(page));
    setParams(next);
  }
  return (
    <nav className="wf-pagination" aria-label={t("分页", "Pagination")}>
      <span>
        {data.total.toLocaleString(language)} {t("条结果", "results")}
      </span>
      <div>
        <button
          className="wf-button wf-icon-button"
          aria-label={t("上一页", "Previous page")}
          disabled={busy || data.page <= 1}
          onClick={() => go(data.page - 1)}
        >
          <ArrowLeft size={16} />
        </button>
        <span>
          {data.page} / {Math.max(1, data.total_pages)}
        </span>
        <button
          className="wf-button wf-icon-button"
          aria-label={t("下一页", "Next page")}
          disabled={busy || data.page >= data.total_pages}
          onClick={() => go(data.page + 1)}
        >
          <ArrowRight size={16} />
        </button>
      </div>
    </nav>
  );
}

export function Filters({
  children,
  placeholder,
  search = true,
}: {
  children?: ReactNode;
  placeholder: string;
  search?: boolean;
}) {
  const { t } = useLocale();
  const [params, setParams] = useSearchParams();
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const next = new URLSearchParams();
    for (const [key, value] of new FormData(event.currentTarget))
      if (typeof value === "string" && value.trim())
        next.set(key, value.trim());
    setParams(next);
  }
  return (
    <form key={params.toString()} className="wf-filterbar" onSubmit={submit}>
      {search && (
        <label className="wf-search">
          <Search size={17} />
          <input
            name="search"
            aria-label={placeholder}
            placeholder={placeholder}
            defaultValue={params.get("search") ?? ""}
          />
        </label>
      )}
      {children}
      <button className="wf-button wf-button-primary" type="submit">
        {t("筛选", "Apply")}
      </button>
      {params.size > 0 && (
        <button
          className="wf-button wf-button-quiet"
          type="button"
          onClick={() => setParams({})}
        >
          {t("清除", "Clear")}
        </button>
      )}
    </form>
  );
}

export function FilterSelect({
  name,
  label,
  options,
}: {
  name: string;
  label: string;
  options: Array<[string, string]>;
}) {
  const [params] = useSearchParams();
  return (
    <label className="wf-select-label">
      <span className="wf-sr-only">{label}</span>
      <select name={name} defaultValue={params.get(name) ?? ""}>
        <option value="">{label}</option>
        {options.map(([value, text]) => (
          <option key={value} value={value}>
            {text}
          </option>
        ))}
      </select>
    </label>
  );
}

export function Markdown({ children }: { children: string }) {
  // Raw HTML is intentionally disabled. External images are omitted to avoid tracking
  // URLs from arbitrary repository content loading inside an authenticated console.
  return (
    <div className="wf-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={{
          img: ({ alt }) => <span>{alt}</span>,
          a: ({ href, children: label }) => (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {label}
            </a>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}

export function Section({
  title,
  children,
  aside,
}: {
  title: string;
  children: ReactNode;
  aside?: ReactNode;
}) {
  return (
    <section className="wf-section">
      <div className="wf-section-heading">
        <h2>{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

export function ReviewRows({ reviews }: { reviews: Review[] }) {
  const { t } = useLocale();
  return (
    <div className="wf-table-wrap">
      <table className="wf-table">
        <thead>
          <tr>
            <th>{t("Pull Request", "Pull request")}</th>
            <th>{t("状态", "Status")}</th>
            <th>{t("评分", "Score")}</th>
            <th>{t("创建时间", "Created")}</th>
          </tr>
        </thead>
        <tbody>
          {reviews.map((review) => (
            <tr key={review.id}>
              <td>
                <Link className="wf-record-title" to={`/reviews/${review.id}`}>
                  {review.title || t("无标题", "Untitled")}
                </Link>
                <div className="wf-record-meta">
                  {review.repo_owner}/{review.repo_name}{" "}
                  <span>#{review.pr_id}</span>
                  {review.author && <span>· {review.author}</span>}
                </div>
              </td>
              <td>
                <StatusBadge
                  value={
                    review.status === "completed" && review.decision
                      ? review.decision
                      : review.status
                  }
                />
              </td>
              <td className="wf-number">
                {review.overall_score != null ? (
                  <>
                    {review.overall_score}
                    <span className="wf-muted"> / 10</span>
                  </>
                ) : (
                  "—"
                )}
              </td>
              <td className="wf-nowrap">
                <Timestamp value={review.created_at} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Disclosure({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="wf-disclosure"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>{title}</summary>
      {open && children}
    </details>
  );
}

export function safeExternalUrl(
  value: string | null | undefined,
): string | undefined {
  if (!value) return undefined;
  if (
    value.startsWith("/") &&
    !value.startsWith("//") &&
    !/[\\\u0000-\u0020]/.test(value)
  )
    return value;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password
      ? url.href
      : undefined;
  } catch {
    return undefined;
  }
}
