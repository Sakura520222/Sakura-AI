import { useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { useLocale } from "../../app/locale";
import { PermissionGuard } from "../../components/common/PermissionGuard";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  Markdown,
  PageHeader,
  Timestamp,
} from "../../components/workflow/Workflow";
import { MutationError } from "../billing/BillingCommon";
import "./memory.css";
import { useUnsavedChanges } from "../../components/common/useUnsavedChanges";

interface Repository {
  repo: string;
  reflections: number;
  updated_at: string;
  extraction: { status: string; detail: string };
}
interface FileEntry {
  path: string;
  name: string;
  type: string;
  size?: number;
}
const base = "/api/v1/console/memory";
export default function MemoryPage() {
  return (
    <PermissionGuard permission="super_admin">
      <MemoryWorkspace />
    </PermissionGuard>
  );
}

function MemoryWorkspace() {
  const { t } = useLocale();
  const [params, setParams] = useSearchParams();
  const repo = params.get("repo") || "",
    file = params.get("file") || "";
  const confirm = useConfirm();
  const repositories = useQuery({
    queryKey: ["memory-repositories"],
    queryFn: ({ signal }) => api<{ items: Repository[] }>(base, { signal }),
  });
  const files = useQuery({
    queryKey: ["memory-files", repo],
    queryFn: ({ signal }) =>
      api<{ files: FileEntry[] }>(
        `${base}/files?repo=${encodeURIComponent(repo)}`,
        { signal },
      ),
    enabled: !!repo,
  });
  const operation = useMutation({
    mutationFn: (action: string) =>
      api(`${base}/${action}?repo=${encodeURIComponent(repo)}`, {
        method: "POST",
      }),
    onSuccess: () => {
      void files.refetch();
      void repositories.refetch();
    },
  });
  return (
    <div>
      <PageHeader
        title={t("仓库记忆", "Repository memory")}
        description={t(
          "查看与维护 .sakura 中的反思、知识和仓库规则。",
          "Browse and maintain reflections, knowledge and repository instructions in .sakura.",
        )}
      />
      {repositories.isPending ? (
        <LoadingState />
      ) : repositories.isError ? (
        <ErrorState retry={() => void repositories.refetch()} />
      ) : (
        <div className="memory-layout">
          <aside>
            <label>
              {t("仓库", "Repository")}
              <select
                value={repo}
                onChange={(event) =>
                  setParams(
                    event.target.value ? { repo: event.target.value } : {},
                  )
                }
              >
                <option value="">{t("选择仓库", "Choose repository")}</option>
                {repositories.data.items.map((item) => (
                  <option value={item.repo} key={item.repo}>
                    {item.repo}
                  </option>
                ))}
              </select>
            </label>
            {repo && (
              <>
                <div className="memory-actions">
                  {[
                    {
                      id: "consolidate",
                      label: t("合并反思", "Consolidate reflections"),
                    },
                    {
                      id: "extract",
                      label: t("提取知识", "Extract knowledge"),
                    },
                  ].map((action) => (
                    <button
                      className="button"
                      key={action.id}
                      disabled={operation.isPending}
                      onClick={async () => {
                        if (
                          await confirm({
                            title: action.label,
                            description: repo,
                          })
                        )
                          operation.mutate(action.id);
                      }}
                    >
                      {action.label}
                    </button>
                  ))}
                </div>
                <MutationError error={operation.error} />
                {files.isPending ? (
                  <LoadingState />
                ) : files.isError ? (
                  <ErrorState retry={() => void files.refetch()} />
                ) : (
                  <nav>
                    {files.data?.files
                      .filter((item) => item.type === "file")
                      .map((item) => (
                        <button
                          key={item.path}
                          className={file === item.path ? "active" : ""}
                          onClick={() => setParams({ repo, file: item.path })}
                        >
                          {item.path}
                        </button>
                      ))}
                  </nav>
                )}
              </>
            )}
          </aside>
          <section>
            {repo && file ? (
              <MemoryEditor
                key={`${repo}/${file}`}
                repo={repo}
                file={file}
                onDeleted={() => {
                  setParams({ repo });
                  void files.refetch();
                }}
              />
            ) : (
              <EmptyState
                title={t("选择一个记忆文件", "Select a memory file")}
                description={
                  repo ||
                  t("先选择要管理的仓库。", "Choose the repository to manage.")
                }
              />
            )}
            {repo && (
              <p className="muted">
                <Timestamp
                  value={
                    repositories.data.items.find((item) => item.repo === repo)
                      ?.updated_at
                  }
                />
              </p>
            )}
          </section>
        </div>
      )}
    </div>
  );
}

function MemoryEditor({
  repo,
  file,
  onDeleted,
}: {
  repo: string;
  file: string;
  onDeleted: () => void;
}) {
  const { t } = useLocale();
  const confirm = useConfirm();
  const [draft, setDraft] = useState<string | null>(null);
  const [preview, setPreview] = useState(false);
  const saved = useRef(false);
  useUnsavedChanges(draft !== null, saved);
  const query = useQuery({
    queryKey: ["memory-content", repo, file],
    queryFn: ({ signal }) =>
      api<{ content: string }>(
        `${base}/files?repo=${encodeURIComponent(repo)}&file=${encodeURIComponent(file)}`,
        { signal },
      ),
  });
  const mutation = useMutation({
    mutationFn: (remove: boolean) =>
      api(
        remove
          ? `${base}/file?repo=${encodeURIComponent(repo)}&file=${encodeURIComponent(file)}`
          : `${base}/file`,
        {
          method: remove ? "DELETE" : "PUT",
          ...(remove
            ? {}
            : {
                body: JSON.stringify({
                  repo,
                  file,
                  content: draft ?? query.data?.content ?? "",
                }),
              }),
        },
      ),
    onSuccess: (_, removed) => {
      if (removed) {
        saved.current = true;
        onDeleted();
      } else {
        setDraft(null);
        void query.refetch();
      }
    },
  });
  if (query.isPending) return <LoadingState />;
  if (query.isError) return <ErrorState retry={() => void query.refetch()} />;
  return (
    <>
      <div className="wf-section-heading">
        <h2>{file}</h2>
        <div className="wf-actions">
          <button className="button" onClick={() => setPreview(!preview)}>
            {preview ? t("编辑", "Edit") : t("预览", "Preview")}
          </button>
          <button
            className="button primary"
            disabled={mutation.isPending || draft === null}
            onClick={async () => {
              if (
                await confirm({
                  title: t("保存到仓库？", "Save to repository?"),
                  description: `${repo}/.sakura/${file}`,
                })
              )
                mutation.mutate(false);
            }}
          >
            {t("保存", "Save")}
          </button>
          <button
            className="button danger-text"
            disabled={mutation.isPending}
            onClick={async () => {
              if (
                await confirm({
                  title: t("删除此文件？", "Delete this file?"),
                  description: `${repo}/.sakura/${file}`,
                  destructive: true,
                })
              )
                mutation.mutate(true);
            }}
          >
            {t("删除", "Delete")}
          </button>
        </div>
      </div>
      <MutationError error={mutation.error} />
      {preview ? (
        <Markdown>{draft ?? query.data.content}</Markdown>
      ) : (
        <textarea
          className="memory-editor"
          aria-label={t("文件内容", "File content")}
          value={draft ?? query.data.content}
          disabled={mutation.isPending}
          maxLength={50000}
          onChange={(event) => setDraft(event.target.value)}
        />
      )}
      {draft !== null && (
        <p className="muted">
          {t("修改尚未保存。", "Your changes have not been saved.")}
        </p>
      )}
    </>
  );
}
