import { useState } from "react";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { ArrowLeft, ExternalLink, RotateCcw } from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { api, ApiError } from "../../api/client";
import { useLocale } from "../../app/locale";
import { useConfirm } from "../../components/ui/ConfirmProvider";
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
  Section,
  StatusBadge,
  Timestamp,
  safeExternalUrl,
} from "../../components/workflow/Workflow";
import type {
  Envelope,
  Issue,
  PageData,
} from "../../components/workflow/types";

interface TaskStatus {
  task_id: string;
  issue_analysis_id: number;
  status: string;
  dispatch_status: string;
}

export default function IssuesPage() {
  const { id } = useParams();
  return id ? <IssueDetail id={id} key={id} /> : <IssueList />;
}

function IssueList() {
  const { t } = useLocale();
  const [params] = useSearchParams();
  const query = new URLSearchParams(params);
  query.set("per_page", "20");
  const issues = useQuery({
    queryKey: ["issues", query.toString()],
    queryFn: ({ signal }) =>
      api<Envelope<PageData<Issue>>>(`/api/v1/issues?${query}`, { signal }),
    placeholderData: keepPreviousData,
  });
  return (
    <div>
      <PageHeader
        title={t("Issue 分析", "Issue analysis")}
        description={t(
          "将反馈整理为清晰的优先级和可执行的建议。",
          "Turn incoming feedback into clear priorities and actionable suggestions.",
        )}
        actions={
          <RefreshButton
            busy={issues.isFetching}
            onClick={() => void issues.refetch()}
          />
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
            ["analyzing", t("分析中", "Analyzing")],
            ["completed", t("已完成", "Completed")],
            ["failed", t("失败", "Failed")],
            ["cancelled", t("已取消", "Cancelled")],
          ]}
        />
        <FilterSelect
          name="priority"
          label={t("全部优先级", "All priorities")}
          options={[
            ["critical", t("严重", "Critical")],
            ["high", t("高", "High")],
            ["medium", t("中", "Medium")],
            ["low", t("低", "Low")],
          ]}
        />
        <FilterSelect
          name="category"
          label={t("全部分类", "All categories")}
          options={[
            ["bug", t("缺陷", "Bug")],
            ["feature", t("功能", "Feature")],
            ["enhancement", t("改进", "Enhancement")],
            ["question", t("问题", "Question")],
            ["documentation", t("文档", "Documentation")],
            ["refactor", t("重构", "Refactor")],
          ]}
        />
        {params.has("repo_name") && (
          <input
            type="hidden"
            name="repo_name"
            value={params.get("repo_name") ?? ""}
          />
        )}
        {params.has("repo_owner") && (
          <input
            type="hidden"
            name="repo_owner"
            value={params.get("repo_owner") ?? ""}
          />
        )}
      </Filters>
      <div className="wf-section" aria-busy={issues.isFetching}>
        {issues.isPending ? (
          <LoadingState />
        ) : issues.isError ? (
          <ErrorState retry={() => void issues.refetch()} />
        ) : (
          issues.data && (
            <>
              {issues.data.data.items.length ? (
                <div className="wf-table-wrap">
                  <table className="wf-table">
                    <thead>
                      <tr>
                        <th>Issue</th>
                        <th>{t("优先级", "Priority")}</th>
                        <th>{t("状态", "Status")}</th>
                        <th>{t("创建时间", "Created")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {issues.data.data.items.map((issue) => (
                        <tr key={issue.id}>
                          <td>
                            <Link
                              className="wf-record-title"
                              to={`/issues/${issue.id}`}
                            >
                              {issue.title || t("无标题", "Untitled")}
                            </Link>
                            <div className="wf-record-meta">
                              {issue.repo_owner}/{issue.repo_name}
                              <span>#{issue.issue_number}</span>
                              {issue.category && (
                                <span>· {issue.category}</span>
                              )}
                            </div>
                          </td>
                          <td>
                            <StatusBadge value={issue.priority} />
                          </td>
                          <td>
                            <StatusBadge value={issue.status} />
                          </td>
                          <td className="wf-nowrap">
                            <Timestamp value={issue.created_at} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <EmptyState
                  title={t("没有匹配的 Issue", "No matching issues")}
                  description={t(
                    "试试其他关键词，或清除筛选条件。",
                    "Try another search or clear your filters.",
                  )}
                />
              )}
              <Pagination
                data={issues.data.data}
                busy={issues.isPlaceholderData}
              />
            </>
          )
        )}
      </div>
    </div>
  );
}

function IssueDetail({ id }: { id: string }) {
  const { t } = useLocale();
  const confirm = useConfirm();
  const client = useQueryClient();
  const [taskId, setTaskId] = useState<string | null>(null);
  const issue = useQuery({
    queryKey: ["issue", id],
    queryFn: ({ signal }) =>
      api<Envelope<Issue>>(`/api/v1/issues/${encodeURIComponent(id)}`, {
        signal,
      }),
  });
  const task = useQuery({
    queryKey: ["issue-task", taskId],
    queryFn: ({ signal }) =>
      api<Envelope<TaskStatus>>(
        `/api/v1/issues/tasks/${encodeURIComponent(taskId ?? "")}`,
        { signal },
      ),
    enabled: Boolean(taskId),
    refetchInterval: (query) =>
      ["pending", "analyzing"].includes(
        query.state.data?.data.status ?? "pending",
      )
        ? 3000
        : false,
  });
  const reanalyze = useMutation({
    mutationFn: () =>
      api<Envelope<{ task_id: string }>>(
        `/api/v1/issues/${encodeURIComponent(id)}/reanalyze`,
        { method: "POST" },
      ),
    retry: false,
    onSuccess: (result) => {
      setTaskId(result.data.task_id);
      toast.success(t("分析任务已提交", "Analysis submitted"));
      void client.invalidateQueries({ queryKey: ["issues"] });
    },
  });
  async function submit() {
    if (
      await confirm({
        title: t("重新分析此 Issue？", "Analyze this issue again?"),
        description: t(
          "将创建新的分析任务，并按当前配额与计费规则执行。",
          "This creates a new analysis task using the current quota and billing policy.",
        ),
        confirmLabel: t("开始分析", "Start analysis"),
      })
    )
      reanalyze.mutate();
  }
  if (issue.isPending) return <LoadingState />;
  if (issue.isError || !issue.data)
    return <ErrorState retry={() => void issue.refetch()} />;
  const data = issue.data.data;
  const relation = data.issue_relations;
  const relations = [
    ...(relation?.primary ? [relation.primary] : []),
    ...(relation?.related ?? []),
  ];
  const busy =
    reanalyze.isPending ||
    Boolean(taskId && !task.data) ||
    ["pending", "analyzing"].includes(task.data?.data.status ?? "");
  const postedUrl = safeExternalUrl(data.comment_url);
  const errorBody =
    reanalyze.error instanceof ApiError
      ? (reanalyze.error.body as { error?: unknown })
      : null;
  return (
    <div>
      <Link className="wf-back" to="/issues">
        <ArrowLeft size={15} />
        {t("所有 Issue", "All issues")}
      </Link>
      <PageHeader
        title={data.title || t("无标题", "Untitled")}
        description={`${data.repo_owner}/${data.repo_name} · #${data.issue_number}`}
        actions={
          <>
            <a
              className="wf-button"
              target="_blank"
              rel="noopener noreferrer"
              href={`https://github.com/${encodeURIComponent(data.repo_owner ?? "")}/${encodeURIComponent(data.repo_name ?? "")}/issues/${data.issue_number}`}
            >
              <ExternalLink size={16} />
              GitHub
            </a>
            <button
              className="wf-button wf-button-primary"
              disabled={busy || data.issue_state === "closed"}
              onClick={() => void submit()}
            >
              <RotateCcw size={16} />
              {t("重新分析", "Analyze again")}
            </button>
          </>
        }
      />
      <div className="wf-detail-meta">
        <StatusBadge value={data.status} />
        <StatusBadge value={data.priority} />
        {data.category && <span>{data.category}</span>}
        {data.author && <span>@{data.author}</span>}
        <Timestamp value={data.created_at} />
      </div>
      {data.issue_state === "closed" && (
        <p className="wf-notice">
          {t(
            "Issue 已关闭，无法发起新的分析。",
            "This issue is closed and cannot be analyzed again.",
          )}
        </p>
      )}
      {reanalyze.isError && (
        <p className="wf-notice" role="alert">
          {typeof errorBody?.error === "string"
            ? errorBody.error
            : t(
                "无法确认任务是否已提交。请刷新记录后再决定是否重试。",
                "Submission could not be confirmed. Refresh your records before trying again.",
              )}
        </p>
      )}
      {taskId && (
        <div className="wf-notice" role="status">
          {task.isError ? (
            <>
              {t(
                "任务状态暂时不可用",
                "Task status is temporarily unavailable",
              )}{" "}
              <button className="wf-button" onClick={() => void task.refetch()}>
                {t("重试查询", "Check again")}
              </button>
            </>
          ) : task.data ? (
            <>
              {t("新的分析任务", "New analysis task")} ·{" "}
              <StatusBadge value={task.data.data.status} />{" "}
              <Link to={`/issues/${task.data.data.issue_analysis_id}`}>
                {t("查看记录", "View record")}
              </Link>
            </>
          ) : (
            t("正在确认任务状态…", "Checking task status…")
          )}
        </div>
      )}
      {data.error_message && (
        <p className="wf-notice" role="alert">
          {data.error_message}
        </p>
      )}
      {data.duplicate_of != null && (
        <p className="wf-notice">
          {t("可能重复于", "Potential duplicate of")} #{data.duplicate_of}
        </p>
      )}
      <div className="wf-detail-grid">
        <Section title={t("分析摘要", "Analysis summary")}>
          <Markdown>
            {data.summary || t("暂无摘要。", "No summary available.")}
          </Markdown>
        </Section>
        <Section title={t("可行性", "Feasibility")}>
          <Markdown>
            {data.feasibility ||
              t("暂无可行性分析。", "No feasibility analysis available.")}
          </Markdown>
        </Section>
      </div>
      <div className="wf-detail-grid">
        <Section title={t("建议标签", "Suggested labels")}>
          <div className="wf-tags">
            {data.suggested_labels?.length ? (
              data.suggested_labels.map((label, index) => (
                <span className="wf-tag" key={`${label.name}-${index}`}>
                  {label.name}
                  {label.confidence != null &&
                    ` · ${Math.round(label.confidence * 100)}%`}
                </span>
              ))
            ) : (
              <span className="wf-muted">
                {t("暂无建议", "No suggestions")}
              </span>
            )}
          </div>
        </Section>
        <Section title={t("建议指派人", "Suggested assignees")}>
          {data.suggested_assignees?.length ? (
            data.suggested_assignees.map((assignee, index) => (
              <div key={`${assignee.username}-${index}`}>
                <div className="wf-tags">
                  <span className="wf-tag">@{assignee.username}</span>
                </div>
                {assignee.reason && <Markdown>{assignee.reason}</Markdown>}
              </div>
            ))
          ) : (
            <div className="wf-tags wf-muted">
              {t("暂无建议", "No suggestions")}
            </div>
          )}
        </Section>
      </div>
      {data.suggested_title && (
        <Section title={t("建议标题", "Suggested title")}>
          <div className="wf-tags">{data.suggested_title}</div>
        </Section>
      )}
      {data.suggested_milestone && (
        <Section title={t("建议里程碑", "Suggested milestone")}>
          <div className="wf-tags">{data.suggested_milestone}</div>
        </Section>
      )}
      {data.related_prs?.length > 0 && (
        <Section title={t("相关 Pull Request", "Related pull requests")}>
          <div className="wf-tags">
            {data.related_prs.map((pr, index) => (
              <span className="wf-tag" key={index}>
                #{pr.number ?? pr.pr_number} {pr.title}
              </span>
            ))}
          </div>
        </Section>
      )}
      {relations.length > 0 && (
        <Section title={t("关联 Issue", "Related issues")}>
          {relations.map((item, index) => (
            <article key={`${item.number}-${index}`}>
              <div className="wf-tags">
                <span className="wf-tag">
                  #{item.number} · {item.relation} ·{" "}
                  {Math.round(item.confidence * 100)}%
                </span>
              </div>
              <Markdown>{item.reason}</Markdown>
              {item.similarities?.length ? (
                <Markdown>{`${t("相似点", "Similarities")}: ${item.similarities.join("; ")}`}</Markdown>
              ) : null}
              {item.differences?.length ? (
                <Markdown>{`${t("差异", "Differences")}: ${item.differences.join("; ")}`}</Markdown>
              ) : null}
              {item.evidence?.map((evidence, evidenceIndex) => (
                <Markdown
                  key={evidenceIndex}
                >{`> ${evidence.current_quote}\n\n> ${evidence.candidate_quote}`}</Markdown>
              ))}
            </article>
          ))}
        </Section>
      )}
      {data.analysis_detail && (
        <Section title={t("完整分析", "Full analysis")}>
          <Markdown>{data.analysis_detail}</Markdown>
        </Section>
      )}
      {(data.prompt_tokens != null || data.completion_tokens != null) && (
        <Section title={t("用量", "Usage")}>
          <dl className="wf-detail-pairs">
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
      )}
      {postedUrl && (
        <a
          className="wf-button"
          href={postedUrl}
          target="_blank"
          rel="noopener noreferrer"
        >
          <ExternalLink size={16} />
          {t("查看已发布的评论", "View published comment")}
        </a>
      )}
    </div>
  );
}
