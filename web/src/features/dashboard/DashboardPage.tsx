import { useQuery } from "@tanstack/react-query";
import {
  ArrowUpRight,
  CheckCheck,
  GitPullRequest,
  MessageSquare,
  Timer,
} from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import { useLocale } from "../../app/locale";
import {
  Disclosure,
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  RefreshButton,
  ReviewRows,
  Section,
} from "../../components/workflow/Workflow";
import type { Envelope, Review } from "../../components/workflow/types";
import "./dashboard.css";

interface Stats {
  total: number;
  completed: number;
  reviewing: number;
  pending: number;
  failed: number;
  approved: number;
  comment_count: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  token_usage_available: boolean;
}
interface Chart {
  trend: { labels: string[]; completed: number[]; failed: number[] };
  top_repos: { labels: string[]; counts: number[] };
}

export default function DashboardPage() {
  const { t, language } = useLocale();
  const stats = useQuery({
    queryKey: ["dashboard", "stats"],
    queryFn: ({ signal }) =>
      api<Envelope<Stats>>("/api/v1/dashboard/stats", { signal }),
  });
  const recent = useQuery({
    queryKey: ["dashboard", "recent"],
    queryFn: ({ signal }) =>
      api<Envelope<Review[]>>("/api/v1/dashboard/recent-reviews", { signal }),
  });
  const charts = useQuery({
    queryKey: ["dashboard", "charts"],
    queryFn: ({ signal }) =>
      api<Envelope<Chart>>("/api/v1/dashboard/chart-data", { signal }),
  });
  const values = stats.data?.data;
  const cards = [
    {
      label: t("审查总数", "Total reviews"),
      value: values?.total,
      icon: GitPullRequest,
      detail: t("所有审查记录", "All review records"),
    },
    {
      label: t("正在处理", "In progress"),
      value: values ? values.reviewing + values.pending : undefined,
      icon: Timer,
      detail: values
        ? t("{0} 个等待中", "{0} waiting", [values.pending])
        : "—",
    },
    {
      label: t("已完成", "Completed"),
      value: values?.completed,
      icon: CheckCheck,
      detail: values
        ? t("{0} 个已通过", "{0} approved", [values.approved])
        : "—",
    },
    {
      label: t("审查意见", "Review comments"),
      value: values?.comment_count,
      icon: MessageSquare,
      detail: t("已生成的代码反馈", "Feedback generated for your code"),
    },
  ];
  return (
    <div className="dashboard-page">
      <PageHeader
        title={t("概览", "Overview")}
        description={t(
          "从这里了解代码审查进展，继续你的工作。",
          "Keep up with your reviews and pick up where you left off.",
        )}
        actions={
          <RefreshButton
            busy={stats.isFetching || recent.isFetching || charts.isFetching}
            onClick={() => {
              void stats.refetch();
              void recent.refetch();
              void charts.refetch();
            }}
          />
        }
      />
      {stats.isError ? (
        <ErrorState retry={() => void stats.refetch()} />
      ) : (
        <div className="dashboard-metrics" aria-busy={stats.isPending}>
          {cards.map((card) => (
            <div className="dashboard-metric" key={card.label}>
              <div className="dashboard-metric-label">
                <span>{card.label}</span>
                <card.icon size={18} />
              </div>
              <strong>{card.value?.toLocaleString(language) ?? "—"}</strong>
              <span className="dashboard-metric-detail">{card.detail}</span>
            </div>
          ))}
        </div>
      )}
      {values && values.failed > 0 && (
        <Link className="dashboard-attention" to="/reviews?status=failed">
          <span>
            <span className="dashboard-attention-dot" />
            {t("{0} 个审查需要留意", "{0} reviews need attention", [values.failed])}
          </span>
          <span>
            {t("查看失败记录", "View failed reviews")}
            <ArrowUpRight size={16} />
          </span>
        </Link>
      )}
      <div className="dashboard-chart-grid">
        <Section
          title={t("审查趋势", "Review activity")}
          aside={
            <span className="wf-muted dashboard-caption">
              {t("最近 30 天", "Last 30 days")}
            </span>
          }
        >
          {charts.isPending ? (
            <LoadingState />
          ) : charts.isError ? (
            <ErrorState retry={() => void charts.refetch()} />
          ) : (
            charts.data && <ActivityChart trend={charts.data.data.trend} />
          )}
        </Section>
        <Section title={t("快速开始", "Quick access")}>
          <div className="dashboard-shortcuts">
            <Link to="/reviews">
              <GitPullRequest size={19} />
              <span>
                <strong>
                  {t("Pull Request 审查", "Pull request reviews")}
                </strong>
                <small>
                  {t("检查结果与逐文件意见", "Results and feedback by file")}
                </small>
              </span>
              <ArrowUpRight size={16} />
            </Link>
            <Link to="/issues">
              <MessageSquare size={19} />
              <span>
                <strong>{t("Issue 分析", "Issue analysis")}</strong>
                <small>
                  {t(
                    "分类、优先级和下一步建议",
                    "Triage, priorities and next steps",
                  )}
                </small>
              </span>
              <ArrowUpRight size={16} />
            </Link>
            <Link to="/settings/personal">
              <CheckCheck size={19} />
              <span>
                <strong>{t("个人设置", "Your preferences")}</strong>
                <small>
                  {t(
                    "语言、外观和通知偏好",
                    "Language, appearance and notifications",
                  )}
                </small>
              </span>
              <ArrowUpRight size={16} />
            </Link>
          </div>
          {values?.token_usage_available && (
            <div className="dashboard-usage">
              <span>{t("已记录 Token 用量", "Recorded token usage")}</span>
              <strong>
                {(
                  values.total_prompt_tokens + values.total_completion_tokens
                ).toLocaleString(language)}
              </strong>
            </div>
          )}
        </Section>
      </div>
      <Section
        title={t("最近的审查", "Recent reviews")}
        aside={
          <Link to="/reviews">
            {t("查看全部", "View all")}
            <ArrowUpRight size={15} />
          </Link>
        }
      >
        {recent.isPending ? (
          <LoadingState />
        ) : recent.isError ? (
          <ErrorState retry={() => void recent.refetch()} />
        ) : recent.data?.data.length ? (
          <ReviewRows reviews={recent.data.data} />
        ) : (
          <EmptyState
            title={t("还没有审查记录", "No reviews yet")}
            description={t(
              "GitHub App 接收到 Pull Request 后，审查结果会显示在这里。",
              "Reviews will appear here when your GitHub App receives a pull request.",
            )}
          />
        )}
      </Section>
    </div>
  );
}

function ActivityChart({ trend }: { trend: Chart["trend"] }) {
  const { t } = useLocale();
  const maximum = Math.max(
    1,
    ...trend.completed.map(
      (count, index) => count + (trend.failed[index] ?? 0),
    ),
  );
  return (
    <div className="dashboard-chart">
      <div className="dashboard-chart-legend">
        <span>
          <i />
          {t("已完成", "Completed")}
        </span>
        <span>
          <i className="failed" />
          {t("失败", "Failed")}
        </span>
      </div>
      <div
        className="dashboard-bars"
        role="img"
        aria-label={t(
          "最近 30 天每日审查量，详细数据见下方表格。",
          "Daily review counts for the last 30 days. Detailed data is available in the table below.",
        )}
      >
        {trend.labels.map((label, index) => (
          <div
            className="dashboard-bar-column"
            key={`${label}-${index}`}
            title={`${label}: ${t("已完成", "Completed")} ${trend.completed[index] ?? 0}, ${t("失败", "Failed")} ${trend.failed[index] ?? 0}`}
          >
            <div
              className="dashboard-bar-failed"
              style={{
                height: `${((trend.failed[index] ?? 0) / maximum) * 100}%`,
              }}
            />
            <div
              className="dashboard-bar-completed"
              style={{
                height: `${((trend.completed[index] ?? 0) / maximum) * 100}%`,
              }}
            />
          </div>
        ))}
      </div>
      <div className="dashboard-chart-axis">
        <span>{trend.labels[0]}</span>
        <span>{trend.labels.at(-1)}</span>
      </div>
      <Disclosure title={t("查看数据表", "View data table")}>
        <div className="wf-table-wrap">
          <table className="wf-table">
            <thead>
              <tr>
                <th>{t("日期", "Date")}</th>
                <th>{t("已完成", "Completed")}</th>
                <th>{t("失败", "Failed")}</th>
              </tr>
            </thead>
            <tbody>
              {trend.labels.map((label, index) => (
                <tr key={`${label}-${index}`}>
                  <td>{label}</td>
                  <td>{trend.completed[index] ?? 0}</td>
                  <td>{trend.failed[index] ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Disclosure>
    </div>
  );
}
