import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowUpRight,
  BookOpen,
  Code2,
  ExternalLink,
  FolderGit2,
  LockKeyhole,
  MessageSquare,
  ScanLine,
  Search,
} from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { toast } from "sonner";
import { api, ApiError } from "../../api/client";
import { useAuth } from "../../app/auth";
import { useLocale } from "../../app/locale";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  RefreshButton,
  Section,
  Timestamp,
  safeExternalUrl,
} from "../../components/workflow/Workflow";
import type { Envelope } from "../../components/workflow/types";
import "./repositories.css";

interface Repository {
  full_name: string;
  name: string;
  private: boolean;
  html_url: string;
  pr_count: number;
  issue_count: number;
  last_activity: string | null;
}
interface Installation {
  installation_id: number;
  account_login: string;
  account_type: string;
  repos: Repository[];
}
type Operation = "index-docs" | "index-code" | "index-issues" | "scan";

export default function RepositoriesPage() {
  const { t } = useLocale();
  const { owner, name } = useParams();
  const [search, setSearch] = useState("");
  const repos = useQuery({
    queryKey: ["repositories"],
    queryFn: ({ signal }) =>
      api<Envelope<Installation[]>>("/api/v1/repos", { signal }),
  });
  const selected =
    owner && name
      ? repos.data?.data
          .flatMap((installation) => installation.repos)
          .find((repo) => repo.full_name === `${owner}/${name}`)
      : undefined;
  if (owner && name)
    return (
      <div>
        <Link className="wf-back" to="/repositories">
          <ArrowLeft size={15} />
          {t("所有仓库", "All repositories")}
        </Link>
        {repos.isPending ? (
          <LoadingState />
        ) : repos.isError ? (
          <ErrorState retry={() => void repos.refetch()} />
        ) : selected ? (
          <RepositoryDetail repo={selected} />
        ) : (
          <EmptyState
            title={t("未找到仓库", "Repository not found")}
            description={t(
              "该仓库可能不在当前 GitHub App 的授权范围内。",
              "This repository may no longer be included in your GitHub App installation.",
            )}
          />
        )}
      </div>
    );
  const filtered = repos.data?.data
    .map((installation) => ({
      ...installation,
      repos: installation.repos.filter((repo) =>
        repo.full_name.toLocaleLowerCase().includes(search.toLocaleLowerCase()),
      ),
    }))
    .filter((installation) => installation.repos.length > 0);
  return (
    <div>
      <PageHeader
        title={t("仓库", "Repositories")}
        description={t(
          "管理已授权仓库的索引，查看审查与分析活动。",
          "Manage indexes and follow review activity across your connected repositories.",
        )}
        actions={
          <>
            <Link className="wf-button" to="/scans">{t('扫描记录', 'Scan history')}</Link>
            <Link className="wf-button" to="/github-app">
              {t("GitHub App 授权", "GitHub App access")}
              <ArrowUpRight size={16} />
            </Link>
            <RefreshButton
              busy={repos.isFetching}
              onClick={() => void repos.refetch()}
            />
          </>
        }
      />
      <div className="wf-filterbar">
        <label className="wf-search">
          <Search size={17} />
          <input
            aria-label={t("搜索仓库", "Search repositories")}
            placeholder={t(
              "按所有者或仓库名称搜索",
              "Search by owner or repository name",
            )}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
      </div>
      {repos.isPending ? (
        <LoadingState />
      ) : repos.isError ? (
        <ErrorState retry={() => void repos.refetch()} />
      ) : filtered?.length ? (
        filtered.map((installation) => (
          <Section
            key={installation.installation_id}
            title={installation.account_login}
            aside={
              <span className="wf-muted repository-count">
                {installation.repos.length} {t("个仓库", "repositories")}
              </span>
            }
          >
            <div>
              {installation.repos.map((repo) => (
                <div className="repository-row" key={repo.full_name}>
                  <FolderGit2 size={20} />
                  <div className="repository-identity">
                    <Link
                      className="wf-record-title"
                      to={`/repositories/${repo.full_name.split("/").map(encodeURIComponent).join("/")}`}
                    >
                      {repo.full_name}
                    </Link>
                    <div className="wf-record-meta">
                      {repo.private && (
                        <span className="repository-private">
                          <LockKeyhole size={11} />
                          {t("私有", "Private")}
                        </span>
                      )}
                      <span>
                        <Timestamp value={repo.last_activity} />
                      </span>
                    </div>
                  </div>
                  <div className="repository-counts">
                    <Link
                      to={`/reviews?repo=${encodeURIComponent(repo.full_name)}`}
                    >
                      {repo.pr_count} PR
                    </Link>
                    <Link
                      to={`/issues?repo_name=${encodeURIComponent(repo.name)}&repo_owner=${encodeURIComponent(repo.full_name.split("/")[0])}`}
                    >
                      {repo.issue_count} Issue
                    </Link>
                  </div>
                  <Link
                    className="wf-button wf-button-quiet"
                    aria-label={t("管理 {0}", "Manage {0}", [repo.full_name])}
                    to={`/repositories/${repo.full_name.split("/").map(encodeURIComponent).join("/")}`}
                  >
                    <ArrowUpRight size={18} />
                  </Link>
                </div>
              ))}
            </div>
          </Section>
        ))
      ) : (
        <EmptyState
          title={
            search
              ? t("没有匹配的仓库", "No matching repositories")
              : t("尚未连接仓库", "No repositories connected")
          }
          description={
            search
              ? t("试试其他关键词。", "Try another search.")
              : t(
                  "通过 GitHub App 授权仓库后，即可在此查看。",
                  "Repositories appear here once they are authorized through your GitHub App.",
                )
          }
        />
      )}
    </div>
  );
}

function RepositoryDetail({ repo }: { repo: Repository }) {
  const { t } = useLocale();
  const { permissions } = useAuth();
  const confirm = useConfirm();
  const [lastSubmitted, setLastSubmitted] = useState<Operation | null>(null);
  const operations: Array<{
    key: Operation;
    label: string;
    description: string;
    icon: typeof BookOpen;
  }> = [
    {
      key: "index-docs",
      label: t("文档索引", "Documentation index"),
      description: t(
        "更新知识库中的仓库文档，用于检索增强。",
        "Refresh repository documentation used for retrieval.",
      ),
      icon: BookOpen,
    },
    {
      key: "index-code",
      label: t("代码索引", "Code index"),
      description: t(
        "更新代码检索索引，帮助定位相关实现。",
        "Refresh the code index to locate relevant implementations.",
      ),
      icon: Code2,
    },
    {
      key: "index-issues",
      label: t("Issue 索引", "Issue index"),
      description: t(
        "更新 Issue 语义索引，用于识别相关问题。",
        "Refresh semantic issue search to identify related discussions.",
      ),
      icon: MessageSquare,
    },
    ...(permissions.super_admin
      ? [
          {
            key: "scan" as const,
            label: t("仓库扫描", "Repository scan"),
            description: t(
              "对当前仓库发起新的完整扫描。",
              "Start a new full scan of this repository.",
            ),
            icon: ScanLine,
          },
        ]
      : []),
  ];
  const mutation = useMutation({
    mutationFn: (operation: Operation) =>
      api<Envelope<null>>(
        `/api/v1/repos/${repo.full_name.split("/").map(encodeURIComponent).join("/")}/${operation}`,
        { method: "POST" },
      ),
    retry: false,
    onSuccess: (_, operation) => {
      setLastSubmitted(operation);
      toast.success(t("任务已提交", "Task submitted"));
    },
  });
  async function run(operation: (typeof operations)[number]) {
    if (
      await confirm({
        title: `${operation.label} · ${repo.full_name}`,
        description:
          operation.key === "scan"
            ? t(
                "将创建新的扫描任务，并按当前配额与计费规则执行。",
                "This creates a new scan using the current quota and billing policy.",
              )
            : t(
                "将使用当前配置在后台更新索引。任务可能需要几分钟。",
                "The index will be updated in the background using the current configuration. This may take a few minutes.",
              ),
        confirmLabel: t("开始", "Start"),
      })
    )
      mutation.mutate(operation.key);
  }
  const externalUrl = safeExternalUrl(repo.html_url);
  const body =
    mutation.error instanceof ApiError
      ? (mutation.error.body as { error?: unknown })
      : null;
  return (
    <div>
      <PageHeader
        title={repo.full_name}
        description={
          repo.private
            ? t("私有仓库", "Private repository")
            : t("公开仓库", "Public repository")
        }
        actions={
          externalUrl && (
            <a
              className="wf-button"
              target="_blank"
              rel="noopener noreferrer"
              href={externalUrl}
            >
              <ExternalLink size={16} />
              GitHub
            </a>
          )
        }
      />
      <div className="repository-overview">
        <Link to={`/reviews?repo=${encodeURIComponent(repo.full_name)}`}>
          <span>{t("PR 审查", "PR reviews")}</span>
          <strong>{repo.pr_count}</strong>
          <ArrowUpRight size={18} />
        </Link>
        <Link
          to={`/issues?repo_name=${encodeURIComponent(repo.name)}&repo_owner=${encodeURIComponent(repo.full_name.split("/")[0])}`}
        >
          <span>{t("Issue 分析", "Issue analyses")}</span>
          <strong>{repo.issue_count}</strong>
          <ArrowUpRight size={18} />
        </Link>
        <div>
          <span>{t("最近活动", "Last activity")}</span>
          <strong className="repository-date">
            <Timestamp value={repo.last_activity} />
          </strong>
        </div>
      </div>
      {mutation.isError && (
        <div className="wf-notice" role="alert">
          {typeof body?.error === "string"
            ? body.error
            : t(
                "无法确认任务状态，请检查后再重试。",
                "The task status could not be confirmed. Check before trying again.",
              )}
        </div>
      )}
      {lastSubmitted && (
        <div className="wf-notice" role="status">
          {
            operations.find((operation) => operation.key === lastSubmitted)
              ?.label
          }{" "}
          ·{" "}
          {t(
            "任务已提交，后台正在处理。",
            "Submitted for background processing.",
          )}
          {lastSubmitted === "scan" && (
            <>
              {" "}
              <Link to="/scans">{t("查看扫描任务", "View scan tasks")}</Link>
            </>
          )}
        </div>
      )}
      <Section title={t("索引与扫描", "Indexes and scans")}>
        <div>
          {operations.map((operation) => (
            <div className="repository-operation" key={operation.key}>
              <operation.icon size={20} />
              <div>
                <h3>{operation.label}</h3>
                <p>{operation.description}</p>
              </div>
              <button
                className="wf-button"
                disabled={mutation.isPending}
                onClick={() => void run(operation)}
              >
                {mutation.isPending && mutation.variables === operation.key
                  ? t("提交中…", "Submitting…")
                  : t("开始", "Start")}
              </button>
            </div>
          ))}
        </div>
      </Section>
    </div>
  );
}
