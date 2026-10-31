import { useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ArrowLeft, Download, ExternalLink, FileCode2 } from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import { useLocale } from "../../app/locale";
import { useAuth } from "../../app/auth";
import {
  EmptyState,
  ErrorState,
  Filters,
  FilterSelect,
  LoadingState,
  Markdown,
  PageHeader,
  Pagination,
  RefreshButton,
  ReviewRows,
  Section,
  StatusBadge,
  Timestamp,
} from "../../components/workflow/Workflow";
import type {
  Envelope,
  PageData,
  Review,
} from "../../components/workflow/types";
import "./reviews.css";

export default function ReviewsPage() {
  const { id } = useParams();
  return id ? <ReviewDetail id={id} key={id} /> : <ReviewList />;
}

function ReviewList() {
  const { t } = useLocale();
  const { permissions } = useAuth();
  const [params] = useSearchParams();
  const query = new URLSearchParams(params);
  query.set("per_page", "20");
  const reviews = useQuery({
    queryKey: ["reviews", query.toString()],
    queryFn: ({ signal }) =>
      api<Envelope<PageData<Review>>>(`/api/v1/reviews?${query}`, { signal }),
    placeholderData: keepPreviousData,
  });
  return (
    <div>
      <PageHeader
        title={t("Pull Request 审查", "Pull request reviews")}
        description={t(
          "搜索审查记录，查看结论和具体修改建议。",
          "Find reviews, understand decisions and explore suggested changes.",
        )}
        actions={
          <>
            {permissions.admin && <Link className="wf-button" to="/queue">{t('审查队列', 'Review queue')}</Link>}
            <a className="wf-button" href={`/api/v1/reviews/export?${params}`}>
              <Download size={16} />
              {t("导出 CSV", "Export CSV")}
            </a>
            <RefreshButton
              busy={reviews.isFetching}
              onClick={() => void reviews.refetch()}
            />
          </>
        }
      />
      <Filters
        placeholder={t(
          "搜索标题、仓库或作者",
          "Search title, repository or author",
        )}
      >
        <FilterSelect
          name="status"
          label={t("全部状态", "All statuses")}
          options={[
            ["pending", t("等待中", "Pending")],
            ["reviewing", t("审查中", "Reviewing")],
            ["completed", t("已完成", "Completed")],
            ["failed", t("失败", "Failed")],
            ["cancelled", t("已取消", "Cancelled")],
          ]}
        />
        <FilterSelect
          name="decision"
          label={t("全部结论", "All decisions")}
          options={[
            ["approve", t("通过", "Approved")],
            ["request_changes", t("需要修改", "Changes requested")],
            ["comment", t("评论", "Comment")],
            ["skip", t("跳过", "Skipped")],
          ]}
        />
        <label className="wf-date-label">
          {t("从", "From")}
          <input
            className="wf-date-input"
            type="date"
            name="date_from"
            defaultValue={params.get("date_from") ?? ""}
          />
        </label>
        <label className="wf-date-label">
          {t("至", "To")}
          <input
            className="wf-date-input"
            type="date"
            name="date_to"
            defaultValue={params.get("date_to") ?? ""}
          />
        </label>
        {params.has("repo") && (
          <input type="hidden" name="repo" value={params.get("repo") ?? ""} />
        )}
      </Filters>
      <div className="wf-section" aria-busy={reviews.isFetching}>
        {reviews.isPending ? (
          <LoadingState />
        ) : reviews.isError ? (
          <ErrorState retry={() => void reviews.refetch()} />
        ) : (
          reviews.data && (
            <>
              {reviews.data.data.items.length ? (
                <ReviewRows reviews={reviews.data.data.items} />
              ) : (
                <EmptyState
                  title={t("没有匹配的审查", "No matching reviews")}
                  description={t(
                    "试试其他关键词，或清除筛选条件。",
                    "Try another search or clear your filters.",
                  )}
                />
              )}
              <Pagination
                data={reviews.data.data}
                busy={reviews.isPlaceholderData}
              />
            </>
          )
        )}
      </div>
    </div>
  );
}

function ReviewDetail({ id }: { id: string }) {
  const { t } = useLocale();
  const [file, setFile] = useState("");
  const [severity, setSeverity] = useState("");
  const review = useQuery({
    queryKey: ["review", id],
    queryFn: ({ signal }) =>
      api<Envelope<Review>>(`/api/v1/reviews/${encodeURIComponent(id)}`, {
        signal,
      }),
  });
  if (review.isPending) return <LoadingState />;
  if (review.isError || !review.data)
    return <ErrorState retry={() => void review.refetch()} />;
  const data = review.data.data;
  const comments = data.comments ?? [];
  const files = [
    ...new Set(comments.map((comment) => comment.file_path || "__overall__")),
  ];
  const filtered = comments.filter(
    (comment) =>
      (!file || (comment.file_path || "__overall__") === file) &&
      (!severity || comment.severity === severity),
  );
  return (
    <div>
      <Link className="wf-back" to="/reviews">
        <ArrowLeft size={15} />
        {t("所有审查", "All reviews")}
      </Link>
      <PageHeader
        title={data.title || t("无标题", "Untitled")}
        description={`${data.repo_owner}/${data.repo_name} · #${data.pr_id}`}
        actions={
          <>
            <a
              className="wf-button"
              target="_blank"
              rel="noopener noreferrer"
              href={`https://github.com/${encodeURIComponent(data.repo_owner ?? "")}/${encodeURIComponent(data.repo_name ?? "")}/pull/${data.pr_id}`}
            >
              <ExternalLink size={16} />
              GitHub
            </a>
            <RefreshButton
              busy={review.isFetching}
              onClick={() => void review.refetch()}
            />
          </>
        }
      />
      <div className="wf-detail-meta">
        <StatusBadge value={data.status} />
        {data.decision && <StatusBadge value={data.decision} />}
        {data.author && <span>@{data.author}</span>}
        {data.branch && <span>{data.branch}</span>}
        <span>
          <Timestamp value={data.created_at} />
        </span>
        {data.overall_score != null && (
          <span>
            {t("评分", "Score")}: <strong>{data.overall_score} / 10</strong>
          </span>
        )}
      </div>
      {data.error_message && (
        <div className="wf-notice" role="alert">
          {data.error_message}
        </div>
      )}
      {data.review_summary && (
        <Section title={t("审查摘要", "Review summary")}>
          <Markdown>{data.review_summary}</Markdown>
        </Section>
      )}
      {data.decision_reason && (
        <Section title={t("结论依据", "Decision rationale")}>
          <Markdown>{data.decision_reason}</Markdown>
        </Section>
      )}
      <Section
        title={t("审查意见", "Review feedback")}
        aside={<span className="wf-muted">{comments.length}</span>}
      >
        {comments.length > 0 && (
          <div className="review-comment-filters">
            <label>
              <FileCode2 size={16} />
              <span className="wf-sr-only">{t("文件", "File")}</span>
              <select
                value={file}
                onChange={(event) => setFile(event.target.value)}
              >
                <option value="">{t("全部文件", "All files")}</option>
                {files.map((path) => (
                  <option key={path} value={path}>
                    {path === "__overall__"
                      ? t("总体意见", "Overall feedback")
                      : path}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span className="wf-sr-only">{t("严重程度", "Severity")}</span>
              <select
                value={severity}
                onChange={(event) => setSeverity(event.target.value)}
              >
                <option value="">{t("全部严重程度", "All severities")}</option>
                <option value="critical">{t("严重", "Critical")}</option>
                <option value="major">{t("主要", "Major")}</option>
                <option value="minor">{t("次要", "Minor")}</option>
                <option value="suggestion">{t("建议", "Suggestion")}</option>
              </select>
            </label>
          </div>
        )}
        {filtered.length ? (
          <div>
            {filtered.map((comment) => (
              <article className="review-comment" key={comment.id}>
                <header>
                  <span>
                    {comment.file_path || t("总体意见", "Overall feedback")}
                    {comment.line_number != null && (
                      <span className="wf-muted">:{comment.line_number}</span>
                    )}
                  </span>
                  <StatusBadge
                    value={comment.severity || comment.comment_type}
                  />
                </header>
                <Markdown>{comment.content}</Markdown>
              </article>
            ))}
          </div>
        ) : (
          <EmptyState title={t("没有匹配的意见", "No matching feedback")} />
        )}
      </Section>
      <Section title={t("审查详情", "Review details")}>
        <dl className="wf-detail-pairs">
          <div>
            <dt>{t("策略", "Strategy")}</dt>
            <dd>{data.strategy || "—"}</dd>
          </div>
          <div>
            <dt>{t("文件数", "Files")}</dt>
            <dd>{data.file_count ?? "—"}</dd>
          </div>
          <div>
            <dt>{t("变更行数", "Changed lines")}</dt>
            <dd>{data.line_count ?? "—"}</dd>
          </div>
          <div>
            <dt>{t("输入 Token", "Input tokens")}</dt>
            <dd>{data.prompt_tokens?.toLocaleString() ?? "—"}</dd>
          </div>
          <div>
            <dt>{t("输出 Token", "Output tokens")}</dt>
            <dd>{data.completion_tokens?.toLocaleString() ?? "—"}</dd>
          </div>
        </dl>
      </Section>
    </div>
  );
}
