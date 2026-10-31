import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { FileText, Plus, Power, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { api, ApiError } from "../../api/client";
import type { components } from "../../api/generated";
import { useLocale } from "../../app/locale";
import { Dialog } from "../../components/ui/Dialog";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import {
  Disclosure,
  EmptyState,
  ErrorState,
  Filters,
  FilterSelect,
  LoadingState,
  PageHeader,
  RefreshButton,
  safeExternalUrl,
  Timestamp,
} from "../../components/workflow/Workflow";
import { ManagementGuard, ManagementNav, formatBytes } from "./Management";

type Skill = components["schemas"]["SkillView"];
type SkillList = components["schemas"]["SkillList"];
const base = "/api/v1/agent/skills";

export default function SkillsPage() {
  return (
    <ManagementGuard>
      <SkillManager />
    </ManagementGuard>
  );
}

function SkillManager() {
  const { t } = useLocale();
  const [params] = useSearchParams();
  const [installing, setInstalling] = useState(false);
  const client = useQueryClient();
  const confirm = useConfirm();
  const query = useQuery({
    queryKey: ["agent-skills", params.toString()],
    queryFn: ({ signal }) => api<SkillList>(`${base}?${params}`, { signal }),
  });
  const mutate = useMutation({
    mutationFn: ({ skill, remove }: { skill: Skill; remove: boolean }) =>
      api(`${base}/${skill.id}`, {
        method: remove ? "DELETE" : "PATCH",
        ...(remove
          ? {}
          : { body: JSON.stringify({ enabled: !skill.enabled }) }),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["agent-skills"] });
      toast.success(t("技能已更新", "Skill updated"));
    },
    onError: () =>
      toast.error(
        t(
          "未能更新技能，请刷新后重试。",
          "Could not update the skill. Refresh and try again.",
        ),
      ),
  });
  async function remove(skill: Skill) {
    if (
      await confirm({
        title: t("删除技能？", "Delete skill?"),
        description: t("将删除 {0} 的安装文件及记录。", "This deletes the installed files and record for {0}.", [skill.name]),
        destructive: true,
      })
    )
      mutate.mutate({ skill, remove: true });
  }
  return (
    <div>
      <PageHeader
        title={t("Agent 技能", "Agent skills")}
        description={t(
          "为 Agent 安装可复用的工作方法和工具说明。",
          "Install reusable workflows and tool instructions for your Agents.",
        )}
        actions={
          <>
            <RefreshButton
              busy={query.isFetching}
              onClick={() => void query.refetch()}
            />
            <button
              className="button primary"
              onClick={() => setInstalling(true)}
            >
              <Plus size={16} />
              {t("安装技能", "Install skill")}
            </button>
          </>
        }
      />
      <ManagementNav />
      {query.data && (
        <div className="management-summary">
          <span>
            {query.data.total} {t("个技能", "skills")} · {query.data.enabled}{" "}
            {t("个已启用", "enabled")}
          </span>
          <code>{query.data.root}</code>
        </div>
      )}
      <Filters
        placeholder={t(
          "搜索技能、用途或工具",
          "Search skills, use cases, or tools",
        )}
      >
        <FilterSelect
          name="status"
          label={t("全部状态", "All statuses")}
          options={[
            ["enabled", t("已启用", "Enabled")],
            ["disabled", t("已停用", "Disabled")],
          ]}
        />
        <FilterSelect
          name="source"
          label={t("全部来源", "All sources")}
          options={(query.data?.sources ?? []).map((source) => [
            source,
            source,
          ])}
        />
      </Filters>
      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState retry={() => void query.refetch()} />
      ) : !query.data.items.length ? (
        <EmptyState
          title={t("没有匹配的技能", "No matching skills")}
          description={t(
            "从 GitHub 安装技能，或上传 SKILL.md / ZIP 文件。",
            "Install from GitHub or upload a SKILL.md / ZIP file.",
          )}
        />
      ) : (
        <div className="management-list">
          {query.data.items.map((skill) => (
            <article key={skill.id} className="management-item">
              <header>
                <span className="management-icon">
                  <FileText size={20} />
                </span>
                <div>
                  <h2>{skill.name}</h2>
                  <p>
                    <code>{skill.slug}</code>
                    {skill.version && ` · ${skill.version}`} ·{" "}
                    {skill.source_type}
                  </p>
                </div>
                <span
                  className={`management-state ${skill.enabled ? "enabled" : ""}`}
                >
                  {skill.enabled
                    ? t("已启用", "Enabled")
                    : t("已停用", "Disabled")}
                </span>
                <button
                  className="icon-button"
                  disabled={mutate.isPending}
                  aria-label={
                    skill.enabled
                      ? t("停用 {0}", "Disable {0}", [skill.name])
                      : t("启用 {0}", "Enable {0}", [skill.name])
                  }
                  onClick={() => mutate.mutate({ skill, remove: false })}
                >
                  <Power size={17} />
                </button>
                <button
                  className="icon-button danger-text"
                  disabled={mutate.isPending}
                  aria-label={t("删除 {0}", "Delete {0}", [skill.name])}
                  onClick={() => void remove(skill)}
                >
                  <Trash2 size={17} />
                </button>
              </header>
              <p>{skill.description || skill.when_to_use}</p>
              {skill.has_error && (
                <p className="danger-text" role="status">
                  {t(
                    "此技能需要检查，请确认安装文件完整。",
                    "This skill needs attention. Check its installed files.",
                  )}
                </p>
              )}
              <Disclosure title={t("使用说明与文件", "Usage and files")}>
                <dl className="management-details">
                  <div>
                    <dt>{t("适用场景", "When to use")}</dt>
                    <dd>{skill.when_to_use || "—"}</dd>
                  </div>
                  <div>
                    <dt>{t("允许的工具", "Allowed tools")}</dt>
                    <dd>{skill.allowed_tools.join(", ") || "—"}</dd>
                  </div>
                  <div>
                    <dt>{t("参数", "Arguments")}</dt>
                    <dd>{skill.arguments.join(", ") || "—"}</dd>
                  </div>
                  <div>
                    <dt>{t("前置条件", "Requirements")}</dt>
                    <dd>{skill.requires || "—"}</dd>
                  </div>
                  <div>
                    <dt>{t("安装位置", "Installed at")}</dt>
                    <dd>
                      <code>{skill.install_path}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>{t("更新时间", "Updated")}</dt>
                    <dd>
                      <Timestamp value={skill.updated_at} />
                    </dd>
                  </div>
                  {safeExternalUrl(skill.source_url) && (
                    <div>
                      <dt>{t("来源", "Source")}</dt>
                      <dd>
                        <a
                          href={safeExternalUrl(skill.source_url)}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {skill.source_url}
                        </a>
                      </dd>
                    </div>
                  )}
                </dl>
                <SkillFiles id={skill.id} />
              </Disclosure>
            </article>
          ))}
        </div>
      )}
      <InstallSkill
        open={installing}
        onOpenChange={setInstalling}
        uploadLimit={query.data?.upload_limit}
        markdownLimit={query.data?.markdown_limit}
      />
    </div>
  );
}

function SkillFiles({ id }: { id: number }) {
  const { t } = useLocale();
  const files = useQuery({
    queryKey: ["agent-skill-files", id],
    queryFn: ({ signal }) =>
      api<{ files: string[] }>(`${base}/${id}/files`, { signal }),
  });
  return files.isPending ? (
    <LoadingState />
  ) : files.isError ? (
    <ErrorState retry={() => void files.refetch()} />
  ) : (
    <div
      className="management-file-list"
      aria-label={t("技能文件", "Skill files")}
    >
      {files.data.files.map((file) => (
        <code key={file}>{file}</code>
      ))}
    </div>
  );
}

function InstallSkill({
  open,
  onOpenChange,
  uploadLimit,
  markdownLimit,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  uploadLimit?: number;
  markdownLimit?: number;
}) {
  const { t } = useLocale();
  const client = useQueryClient();
  const [method, setMethod] = useState("github");
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const mutation = useMutation({
    mutationFn: async () => {
      if (method === "github")
        return api(base + "/github", {
          method: "POST",
          body: JSON.stringify({ url: url.trim() }),
        });
      if (!file) throw new Error("No file selected");
      const body = new FormData();
      body.set("skill_file", file);
      body.set("name", name);
      return api(base + "/upload", { method: "POST", body });
    },
    onSuccess: () => {
      onOpenChange(false);
      setUrl("");
      setName("");
      setFile(null);
      void client.invalidateQueries({ queryKey: ["agent-skills"] });
      toast.success(t("技能已安装", "Skill installed"));
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    mutation.mutate();
  }
  const selectedLimit = file?.name.toLowerCase().endsWith('.md') ? markdownLimit : uploadLimit;
  const oversized = file && selectedLimit && file.size > selectedLimit;
  return (
    <Dialog
      open={open}
      onOpenChange={(value) => {
        if (!mutation.isPending) onOpenChange(value);
      }}
      title={t("安装 Agent 技能", "Install Agent skill")}
    >
      <form className="management-form" onSubmit={submit}>
        <label>
          {t("安装方式", "Install from")}
          <select
            value={method}
            onChange={(event) => {
              setMethod(event.target.value);
              mutation.reset();
            }}
          >
            <option value="github">GitHub</option>
            <option value="upload">{t("本地文件", "Local file")}</option>
          </select>
        </label>
        {method === "github" ? (
          <label>
            {t("GitHub SKILL.md 链接", "GitHub SKILL.md URL")}
            <input
              type="url"
              required
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="https://github.com/owner/repo/blob/main/skill/SKILL.md"
            />
          </label>
        ) : (
          <>
            <label>
              {t("SKILL.md 或 ZIP 文件", "SKILL.md or ZIP file")}
              <input
                type="file"
                accept=".md,.zip"
                required
                onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              />
            </label>
            <label>
              {t("显示名称（可选）", "Display name (optional)")}
              <input
                value={name}
                maxLength={255}
                onChange={(event) => setName(event.target.value)}
              />
            </label>
            {selectedLimit && (
              <p className="muted">
                {t("最大上传大小", "Maximum upload size")}:{" "}
                {formatBytes(selectedLimit)}
              </p>
            )}
          </>
        )}
        {oversized && (
          <p className="danger-text" role="alert">
            {t("文件超过上传限制。", "The file exceeds the upload limit.")}
          </p>
        )}
        {mutation.isError && (
          <p className="danger-text" role="alert">
            {mutation.error instanceof ApiError && mutation.error.status === 422
              ? t(
                  "请检查来源地址或技能文件格式。",
                  "Check the source URL or skill package format.",
                )
              : t(
                  "安装未完成，请检查连接后重试。",
                  "Installation could not complete. Check your connection and try again.",
                )}
          </p>
        )}
        <div className="confirm-actions">
          <button
            className="button"
            type="button"
            disabled={mutation.isPending}
            onClick={() => onOpenChange(false)}
          >
            {t("取消", "Cancel")}
          </button>
          <button
            className="button primary"
            disabled={mutation.isPending || !!oversized}
          >
            {mutation.isPending
              ? t("正在安装…", "Installing…")
              : t("安装", "Install")}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
