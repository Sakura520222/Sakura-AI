import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { FolderGit2, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { api } from "../../api/client";
import { useLocale } from "../../app/locale";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import {
  Disclosure,
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  RefreshButton,
  StatusBadge,
  Timestamp,
} from "../../components/workflow/Workflow";
import { ManagementGuard, ManagementNav, formatBytes } from "./Management";

interface Workspace {
  repo_owner: string;
  repo_name: string;
  repo_full_name: string;
  path: string;
  file_count: number;
  total_size_bytes: number;
  modified_at: string | null;
  has_git: boolean;
  worktree_count: number;
}
interface Worktree {
  dir_name: string;
  task_id: number | null;
  branch_slug: string;
  file_count: number;
  total_size_bytes: number;
  modified_at: string | null;
  task_status: string | null;
  is_active: boolean;
}
interface Result {
  success: boolean;
  cleaned?: number;
  message?: string;
}
const base = "/api/v1/agent/workspaces";
function workspacePath(item: Workspace) {
  return `${base}/${encodeURIComponent(item.repo_owner)}/${encodeURIComponent(item.repo_name)}`;
}
async function change(path: string, method: string) {
  const result = await api<Result>(path, { method });
  if (!result.success) throw new Error("Workspace operation declined");
  return result;
}

export default function WorkspacesPage() {
  return (
    <ManagementGuard>
      <WorkspaceManager />
    </ManagementGuard>
  );
}

function WorkspaceManager() {
  const { t } = useLocale();
  const confirm = useConfirm();
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["agent-workspaces"],
    queryFn: ({ signal }) =>
      api<{ root: string; items: Workspace[]; total_size_bytes: number }>(
        base,
        { signal },
      ),
  });
  const remove = useMutation({
    mutationFn: (item: Workspace) => change(workspacePath(item), "DELETE"),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["agent-workspaces"] });
      toast.success(t("工作区已删除", "Workspace deleted"));
    },
    onError: () =>
      toast.error(
        t(
          "无法删除工作区。请确认相关任务已经结束。",
          "Could not delete the workspace. Check that its tasks have finished.",
        ),
      ),
  });
  return (
    <div>
      <PageHeader
        title={t("Agent 工作区", "Agent workspaces")}
        description={t(
          "查看仓库副本与任务工作树，清理已结束任务的本地文件。",
          "Inspect repository copies and task worktrees, and clean up files after tasks finish.",
        )}
        actions={
          <RefreshButton
            busy={query.isFetching}
            onClick={() => void query.refetch()}
          />
        }
      />
      <ManagementNav />
      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState retry={() => void query.refetch()} />
      ) : (
        <>
          <div className="management-summary">
            <span>
              {query.data.items.length} {t("个仓库", "repositories")} ·{" "}
              {formatBytes(query.data.total_size_bytes)}
            </span>
            <code>{query.data.root}</code>
          </div>
          {!query.data.items.length ? (
            <EmptyState
              title={t("还没有工作区", "No workspaces yet")}
              description={t(
                "Agent 开始处理仓库后会创建工作区。",
                "Workspaces are created when an Agent starts working on a repository.",
              )}
            />
          ) : (
            query.data.items.map((workspace) => (
              <article
                className="management-item"
                key={workspace.repo_full_name}
              >
                <header>
                  <span className="management-icon">
                    <FolderGit2 size={20} />
                  </span>
                  <div>
                    <h2>{workspace.repo_full_name}</h2>
                    <p>
                      {workspace.file_count} {t("个文件", "files")} ·{" "}
                      {formatBytes(workspace.total_size_bytes)} ·{" "}
                      <Timestamp value={workspace.modified_at} />
                    </p>
                  </div>
                  <button
                    className="button danger-text"
                    disabled={remove.isPending}
                    onClick={async () => {
                      if (
                        await confirm({
                          title: t(
                            "删除仓库工作区？",
                            "Delete repository workspace?",
                          ),
                          description: t("将删除 {0} 的所有本地副本与工作树。不会删除远程仓库。", "This deletes all local copies and worktrees for {0}. The remote repository is unaffected.", [workspace.repo_full_name]),
                          destructive: true,
                        })
                      )
                        remove.mutate(workspace);
                    }}
                  >
                    <Trash2 size={15} />
                    {t("删除", "Delete")}
                  </button>
                </header>
                <code className="management-path">{workspace.path}</code>
                <Disclosure
                  title={t("工作树 · {0}", "Worktrees · {0}", [workspace.worktree_count])}
                >
                  <Worktrees workspace={workspace} />
                </Disclosure>
              </article>
            ))
          )}
        </>
      )}
    </div>
  );
}

function Worktrees({ workspace }: { workspace: Workspace }) {
  const { t } = useLocale();
  const client = useQueryClient();
  const confirm = useConfirm();
  const path = workspacePath(workspace);
  const query = useQuery({
    queryKey: ["agent-worktrees", workspace.repo_full_name],
    queryFn: ({ signal }) =>
      api<{ items: Worktree[] }>(path + "/worktrees", { signal }),
  });
  const mutation = useMutation({
    mutationFn: (item: Worktree | null) =>
      change(
        item
          ? `${path}/worktrees/${encodeURIComponent(item.dir_name)}`
          : `${path}/clean`,
        item ? "DELETE" : "POST",
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["agent-worktrees"] });
      void client.invalidateQueries({ queryKey: ["agent-workspaces"] });
      toast.success(t("工作树已清理", "Worktrees cleaned"));
    },
    onError: () =>
      toast.error(
        t(
          "清理未完成，请刷新列表后检查任务状态。",
          "Cleanup did not complete. Refresh the list and check task status.",
        ),
      ),
  });
  if (query.isPending) return <LoadingState />;
  if (query.isError) return <ErrorState retry={() => void query.refetch()} />;
  return (
    <div>
      {query.data.items.some((item) => !item.is_active) && (
        <button
          className="button"
          disabled={mutation.isPending}
          onClick={async () => {
            if (
              await confirm({
                title: t("清理不再运行的工作树？", "Clean inactive worktrees?"),
                description: t(
                  "将删除没有活跃任务使用的本地工作树，包括其中未提交的文件。",
                  "This removes local worktrees not used by active tasks, including uncommitted files.",
                ),
                destructive: true,
              })
            )
              mutation.mutate(null);
          }}
        >
          {t("清理闲置工作树", "Clean inactive worktrees")}
        </button>
      )}
      <div className="management-worktrees">
        {query.data.items.length ? (
          query.data.items.map((item) => (
            <div key={item.dir_name}>
              <div>
                <strong>{item.branch_slug}</strong>
                <p>
                  {item.task_id ? (
                    <Link to={`/agent/${item.task_id}`}>
                      {t("任务", "Task")} #{item.task_id}
                    </Link>
                  ) : (
                    t("没有关联任务", "No linked task")
                  )}{" "}
                  · {formatBytes(item.total_size_bytes)}
                </p>
              </div>
              <StatusBadge value={item.task_status} />
              <button
                className="icon-button danger-text"
                disabled={mutation.isPending || item.is_active}
                aria-label={t("删除工作树 {0}", "Delete worktree {0}", [item.dir_name])}
                onClick={async () => {
                  if (
                    await confirm({
                      title: t("删除工作树？", "Delete worktree?"),
                      description: `${item.dir_name} · ${t("本地未提交的修改也会被删除。", "Uncommitted local changes will also be removed.")}`,
                      destructive: true,
                    })
                  )
                    mutation.mutate(item);
                }}
              >
                <Trash2 size={16} />
              </button>
            </div>
          ))
        ) : (
          <p className="muted">{t("没有工作树", "No worktrees")}</p>
        )}
      </div>
    </div>
  );
}
