import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, ArrowUpRight, Play, RotateCcw, Square } from "lucide-react";
import { toast } from "sonner";
import { api } from "../../api/client";
import { useAuth } from "../../app/auth";
import { useLocale } from "../../app/locale";
import { PermissionGuard } from "../../components/common/PermissionGuard";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import {
  Disclosure,
  EmptyState,
  ErrorState,
  FilterSelect,
  Filters,
  LoadingState,
  Markdown,
  PageHeader,
  Pagination,
  RefreshButton,
  safeExternalUrl,
  Section,
  StatusBadge,
  Timestamp,
} from "../../components/workflow/Workflow";
import type { Envelope, PageData } from "../../components/workflow/types";
import "./scans.css";

interface Scan {
  id: number;
  repo_owner: string;
  repo_name: string;
  status: string;
  progress: number;
  trigger_type: string;
  total_findings: number;
  overall_health_score: number | null;
  created_at: string;
  completed_at: string | null;
}
interface Finding {
  id: number;
  file_path: string;
  line_start: number | null;
  line_end: number | null;
  severity: string;
  category: string;
  title: string;
  description: string;
  suggestion: string | null;
  confidence: number | null;
  code_snippet: string | null;
}
interface ScanDetail extends Scan {
  summary: string | null;
  scan_rounds: number | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  triggered_by: string | null;
  commit_sha: string | null;
  current_phase: string | null;
  error_message: string | null;
  file_count: number;
  code_file_count: number;
  critical_count: number;
  major_count: number;
  minor_count: number;
  suggestion_count: number;
  report_issue_number: number | null;
  report_issue_url: string | null;
  findings: Finding[];
}
const activeStatuses = ["pending", "indexing", "analyzing", "reporting"];
async function action(path: string) {
  const result = await api<Envelope<{ count?: number }>>(path, {
    method: "POST",
  });
  if (!result.success) throw new Error("Scan operation declined");
  return result;
}

export default function ScansPage() {
  const { id } = useParams();
  return (
    <PermissionGuard permission="admin">
      {id ? <ScanView id={id} /> : <ScanList />}
    </PermissionGuard>
  );
}

function ScanList() {
  const { t } = useLocale();
  const { permissions } = useAuth();
  const [params] = useSearchParams();
  const confirm = useConfirm();
  const client = useQueryClient();
  const list = useQuery({
    queryKey: ["scans", params.toString()],
    queryFn: ({ signal }) =>
      api<Envelope<PageData<Scan>>>(`/api/v1/scans?${params}`, { signal }),
    refetchInterval: 15_000,
  });
  const stats = useQuery({
    queryKey: ["scan-stats"],
    queryFn: ({ signal }) =>
      api<
        Envelope<{
          total: number;
          by_status: Record<string, number>;
          avg_health_score: number | null;
        }>
      >("/api/v1/scans/stats", { signal }),
  });
  const trigger = useMutation({
    mutationFn: () => action("/api/v1/scans/trigger"),
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: ["scans"] });
      void client.invalidateQueries({ queryKey: ["scan-stats"] });
      toast.success(
        t("已提交 {0} 个仓库扫描", "Submitted {0} repository scans", [
          result.data.count ?? 0,
        ]),
      );
    },
    onError: () =>
      toast.error(
        t(
          "无法启动扫描，请检查仓库授权、冷却期与扫描配置。",
          "Could not start a scan. Check repository access, cooldowns and scan settings.",
        ),
      ),
  });
  return (
    <div>
      <PageHeader
        title={t("仓库扫描", "Repository scans")}
        description={t(
          "查看全仓库检查的进度、报告与发现。",
          "Track full repository checks, reports and findings.",
        )}
        actions={
          <>
            <RefreshButton
              busy={list.isFetching}
              onClick={() => {
                void list.refetch();
                void stats.refetch();
              }}
            />
            {permissions.super_admin && (
              <button
                className="button primary"
                disabled={trigger.isPending}
                onClick={async () => {
                  if (
                    await confirm({
                      title: t(
                        "启动符合条件的仓库扫描？",
                        "Start eligible repository scans?",
                      ),
                      description: t(
                        "扫描将使用现有 AI 配置，并按当前配额与计费规则执行。",
                        "Scans use the current AI configuration, quotas and charging policy.",
                      ),
                    })
                  )
                    trigger.mutate();
                }}
              >
                <Play size={16} />
                {t("启动扫描", "Start scans")}
              </button>
            )}
          </>
        }
      />
      <div className="scan-links">
        <Link to="/repositories">{t("仓库", "Repositories")}</Link>
        {permissions.super_admin && (
          <Link to="/settings/labels">{t("扫描设置", "Scan settings")}</Link>
        )}
      </div>
      {stats.data && (
        <div className="scan-stats">
          <span>
            {t("总计", "Total")} <strong>{stats.data.data.total}</strong>
          </span>
          <span>
            {t("已完成", "Completed")}{" "}
            <strong>{stats.data.data.by_status.completed ?? 0}</strong>
          </span>
          <span>
            {t("失败", "Failed")}{" "}
            <strong>{stats.data.data.by_status.failed ?? 0}</strong>
          </span>
          <span>
            {t("平均健康评分", "Average health score")}{" "}
            <strong>{stats.data.data.avg_health_score ?? "—"}</strong>
          </span>
        </div>
      )}
      {stats.isError && (
        <p className="muted" role="status">
          {t(
            "扫描统计暂时不可用。列表仍可独立查看。",
            "Scan statistics are unavailable. The list remains available independently.",
          )}
        </p>
      )}
      <Filters placeholder={t("搜索仓库", "Search repositories")}>
        <FilterSelect
          name="status"
          label={t("全部状态", "All statuses")}
          options={[
            ["pending", t("等待中", "Pending")],
            ["indexing", t("建立索引", "Indexing")],
            ["analyzing", t("分析中", "Analyzing")],
            ["reporting", t("生成报告", "Reporting")],
            ["completed", t("已完成", "Completed")],
            ["failed", t("失败", "Failed")],
            ["cancelled", t("已取消", "Cancelled")],
          ]}
        />
      </Filters>
      {list.isPending ? (
        <LoadingState />
      ) : list.isError ? (
        <ErrorState retry={() => void list.refetch()} />
      ) : !list.data.data.items.length ? (
        <EmptyState title={t("暂无扫描记录", "No scans yet")} />
      ) : (
        <>
          <div className="wf-table-wrap">
            <table className="wf-table">
              <thead>
                <tr>
                  <th>{t("仓库", "Repository")}</th>
                  <th>{t("状态", "Status")}</th>
                  <th>{t("发现", "Findings")}</th>
                  <th>{t("健康评分", "Health score")}</th>
                  <th>{t("创建时间", "Created")}</th>
                </tr>
              </thead>
              <tbody>
                {list.data.data.items.map((scan) => (
                  <tr key={scan.id}>
                    <td>
                      <Link
                        className="wf-record-title"
                        to={`/scans/${scan.id}`}
                      >
                        {scan.repo_owner}/{scan.repo_name}
                      </Link>
                      <div className="wf-record-meta">
                        #{scan.id} · {scan.trigger_type}
                      </div>
                    </td>
                    <td>
                      <StatusBadge value={scan.status} />
                      {activeStatuses.includes(scan.status) && (
                        <progress
                          className="scan-progress"
                          value={scan.progress || 0}
                          max={100}
                          aria-label={t("扫描进度", "Scan progress")}
                        />
                      )}
                    </td>
                    <td>{scan.total_findings}</td>
                    <td>{scan.overall_health_score ?? "—"}</td>
                    <td>
                      <Timestamp value={scan.created_at} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination data={list.data.data} busy={list.isFetching} />
        </>
      )}
    </div>
  );
}

function ScanView({ id }: { id: string }) {
  const { t } = useLocale();
  const { permissions } = useAuth();
  const client = useQueryClient();
  const confirm = useConfirm();
  const [severity, setSeverity] = useState("");
  const query = useQuery({
    queryKey: ["scan", id],
    queryFn: ({ signal }) =>
      api<Envelope<ScanDetail>>(`/api/v1/scans/${id}`, { signal }),
    refetchInterval: (current) =>
      current.state.data &&
      activeStatuses.includes(current.state.data.data.status)
        ? 5000
        : false,
  });
  const mutation = useMutation({
    mutationFn: (operation: string) =>
      action(`/api/v1/scans/${id}/${operation}`),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["scan", id] });
      void client.invalidateQueries({ queryKey: ["scans"] });
      toast.success(t("操作已提交", "Action submitted"));
    },
    onError: () =>
      toast.error(
        t(
          "操作未完成，请刷新扫描状态后重试。",
          "The action did not complete. Refresh the scan status and try again.",
        ),
      ),
  });
  if (query.isPending) return <LoadingState />;
  if (query.isError) return <ErrorState retry={() => void query.refetch()} />;
  const scan = query.data.data;
  const report = safeExternalUrl(scan.report_issue_url);
  const findings = scan.findings.filter(
    (finding) => !severity || finding.severity === severity,
  );
  return (
    <div>
      <Link className="wf-back" to="/scans">
        <ArrowLeft size={16} />
        {t("所有扫描", "All scans")}
      </Link>
      <PageHeader
        title={`${scan.repo_owner}/${scan.repo_name}`}
        description={t("扫描 #{0}", "Scan #{0}", [scan.id])}
        actions={
          <>
            <RefreshButton
              busy={query.isFetching}
              onClick={() => void query.refetch()}
            />
            {permissions.super_admin && scan.status === "failed" && (
              <button
                className="button"
                disabled={mutation.isPending}
                onClick={async () => {
                  if (
                    await confirm({
                      title: t("重新执行扫描？", "Retry this scan?"),
                      description: t(
                        "这会开始新一轮扫描，并重新执行配额和计费准入。",
                        "This starts a new scan execution with quota and billing admission.",
                      ),
                    })
                  )
                    mutation.mutate("retry");
                }}
              >
                <RotateCcw size={15} />
                {t("重试", "Retry")}
              </button>
            )}
            {permissions.super_admin &&
              activeStatuses.includes(scan.status) && (
                <button
                  className="button danger-text"
                  disabled={mutation.isPending}
                  onClick={async () => {
                    if (
                      await confirm({
                        title: t("取消扫描？", "Cancel this scan?"),
                        description: t(
                          "停止当前扫描执行。",
                          "Stop the current scan execution.",
                        ),
                        destructive: true,
                      })
                    )
                      mutation.mutate("cancel");
                  }}
                >
                  <Square size={15} />
                  {t("取消扫描", "Cancel scan")}
                </button>
              )}
          </>
        }
      />
      <div className="scan-stats">
        <StatusBadge value={scan.status} />
        <span>
          {scan.total_findings} {t("个发现", "findings")}
        </span>
        <span>
          {t("健康评分", "Health score")}{" "}
          <strong>{scan.overall_health_score ?? "—"}</strong>
        </span>
        {report && (
          <a href={report} target="_blank" rel="noreferrer">
            {t("打开报告", "Open report")} <ArrowUpRight size={14} />
          </a>
        )}
      </div>
      {scan.error_message && (
        <p className="scan-error" role="alert">
          {scan.error_message}
        </p>
      )}
      {scan.summary && (
        <Section title={t("扫描总结", "Scan summary")}>
          <Markdown>{scan.summary}</Markdown>
        </Section>
      )}
      {activeStatuses.includes(scan.status) && (
        <div className="scan-live">
          <span>
            {scan.current_phase || t("正在处理", "Processing")} ·{" "}
            {scan.progress}%
          </span>
          <progress
            value={scan.progress || 0}
            max={100}
            aria-label={t("扫描进度", "Scan progress")}
          />
        </div>
      )}
      <Section
        title={t("扫描发现", "Findings")}
        aside={
          <select
            aria-label={t("严重程度", "Severity")}
            value={severity}
            onChange={(event) => setSeverity(event.target.value)}
          >
            <option value="">{t("全部严重程度", "All severities")}</option>
            {["critical", "major", "minor", "suggestion"].map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        }
      >
        {findings.length ? (
          findings.map((finding) => (
            <article className="scan-finding" key={finding.id}>
              <header>
                <StatusBadge value={finding.severity} />
                <strong>{finding.title}</strong>
              </header>
              <p className="muted">
                <code>
                  {finding.file_path}
                  {finding.line_start
                    ? `:${finding.line_start}${finding.line_end && finding.line_end !== finding.line_start ? `–${finding.line_end}` : ""}`
                    : ""}
                </code>{" "}
                · {finding.category}
              </p>
              <Markdown>{finding.description}</Markdown>
              {finding.code_snippet && (
                <Disclosure title={t("相关代码", "Relevant code")}>
                  <pre className="agent-pre">
                    <code>{finding.code_snippet}</code>
                  </pre>
                </Disclosure>
              )}
              {finding.suggestion && (
                <Disclosure title={t("修改建议", "Suggested change")}>
                  <Markdown>{finding.suggestion}</Markdown>
                </Disclosure>
              )}
            </article>
          ))
        ) : (
          <EmptyState
            title={t("暂无符合筛选条件的发现", "No findings match this filter")}
          />
        )}
      </Section>
      <Disclosure title={t("执行详情", "Execution details")}>
        <dl className="scan-details">
          <div>
            <dt>{t("执行轮次", "Execution rounds")}</dt>
            <dd>{scan.scan_rounds ?? "—"}</dd>
          </div>
          <div>
            <dt>{t("输入 / 输出 Token", "Input / output tokens")}</dt>
            <dd>
              {scan.prompt_tokens ?? "—"} / {scan.completion_tokens ?? "—"}
            </dd>
          </div>
          <div>
            <dt>{t("触发来源", "Triggered by")}</dt>
            <dd>{scan.triggered_by || scan.trigger_type}</dd>
          </div>
          <div>
            <dt>Commit</dt>
            <dd>
              <code>{scan.commit_sha || "—"}</code>
            </dd>
          </div>
          <div>
            <dt>{t("代码文件", "Code files")}</dt>
            <dd>
              {scan.code_file_count} / {scan.file_count}
            </dd>
          </div>
          <div>
            <dt>{t("完成时间", "Completed")}</dt>
            <dd>
              <Timestamp value={scan.completed_at} />
            </dd>
          </div>
        </dl>
      </Disclosure>
    </div>
  );
}
