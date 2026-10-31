import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  Download,
  FileJson,
  FolderOpen,
  Info,
  LoaderCircle,
  Upload,
  X,
} from "lucide-react";
import { api, ApiError } from "../../api/client";
import { useLocale } from "../../app/locale";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import type { SettingsStore } from "./state";

interface Metadata {
  scopes: string[];
  config_max_bytes: number;
  users_max_bytes: number;
}
interface RestoreResult {
  persisted: boolean;
  kind: "config" | "users";
  sections: string[];
  created: number;
  updated: number;
  deleted: number;
  unchanged: number;
  requires_restart: boolean;
  runtime_refresh_ok: boolean;
  audit_recorded: boolean;
  recovery_codes_portable: boolean | null;
}
interface Preview {
  format: string;
  sections: string[];
  count: number | null;
}

export function backupPreview(document: unknown): Preview {
  if (!document || typeof document !== "object" || Array.isArray(document))
    throw new Error("invalid_backup");
  const value = document as Record<string, unknown>;
  if (typeof value.format !== "string") throw new Error("invalid_backup");
  const sections =
    value.sections && typeof value.sections === "object"
      ? Object.keys(value.sections)
      : [];
  const counts =
    value.sections && typeof value.sections === "object"
      ? Object.values(value.sections).map((section: unknown) =>
          section && typeof section === "object"
            ? (section as { count?: unknown }).count
            : undefined,
        )
      : [];
  return {
    format: value.format,
    sections,
    count:
      typeof value.user_count === "number"
        ? value.user_count
        : counts.length &&
            counts.every(
              (count) =>
                typeof count === "number" &&
                Number.isInteger(count) &&
                count >= 0,
            )
          ? (counts as number[]).reduce((total, count) => total + count, 0)
          : null,
  };
}

export function BackupPanel({ store }: { store: SettingsStore }) {
  const { language, t } = useLocale();
  const confirm = useConfirm();
  const client = useQueryClient();
  const [scope, setScope] = useState("all");
  const [kind, setKind] = useState<"config" | "users">("config");
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState("");
  const selection = useRef(0);
  const fileInput = useRef<HTMLInputElement>(null);
  const metadata = useQuery({
    queryKey: ["backup-metadata"],
    queryFn: () => api<Metadata>("/api/v1/webui/backup"),
  });
  const maxBytes =
    kind === "config"
      ? metadata.data?.config_max_bytes
      : metadata.data?.users_max_bytes;
  const labels: Record<string, string> = {
    all: t("完整配置", "All configuration"),
    global: t("全局设置", "General settings"),
    ai: t("AI 账号与模型", "AI accounts & models"),
    system: t("系统设置", "System settings"),
    users: t("用户与安全信息", "Users & security"),
    strategy: t("审查策略", "Review strategies"),
    label: t("标签", "Labels"),
  };
  const download = useMutation({
    mutationFn: async () => {
      const result = await api<unknown>(
        `/api/v1/webui/backup/export/${encodeURIComponent(scope)}?language=${language}`,
        { method: "POST" },
      );
      if (!result || typeof result !== "object")
        throw new Error("invalid_download");
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(result, null, 2)], {
          type: "application/json",
        }),
      );
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `sakura-ai-${scope}-backup.json`;
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    },
  });
  const restore = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error("missing_file");
      const body = new FormData();
      body.append("backup_file", file);
      body.append("kind", kind);
      return api<RestoreResult>("/api/v1/webui/backup/import", {
        method: "POST",
        body,
      });
    },
    onSuccess: () => {
      setFile(null);
      setPreview(null);
      if (fileInput.current) fileInput.current.value = "";
      void client.invalidateQueries({ queryKey: ["settings"] });
      void client.invalidateQueries({ queryKey: ["ai-accounts"] });
    },
    onError: (failure) => {
      const code =
        failure instanceof ApiError
          ? (failure.body as { error?: { code?: string } })?.error?.code
          : "";
      setError(
        code === "invalid_backup"
          ? t(
              "备份未通过校验。请检查文件类型、版本和连接配置，当前数据未恢复。",
              "Backup validation failed. Check its type, version and connection settings. No restore was applied.",
            )
          : code === "backup_too_large"
            ? t("文件超过允许的大小。", "The file exceeds the size limit.")
            : t(
                "无法确认恢复结果。请先刷新并核对当前设置，再决定是否重试。",
                "Could not confirm the restore outcome. Refresh and inspect current settings before retrying.",
              ),
      );
    },
  });
  useEffect(() => {
    store.setExternalDirty("backup", !!file || restore.isPending);
    return () => store.setExternalDirty("backup", false);
  }, [file, restore.isPending, store]);
  const choose = async (selected: File | null) => {
    const generation = ++selection.current;
    setFile(null);
    setPreview(null);
    setError("");
    restore.reset();
    if (!selected) return;
    if (maxBytes == null || selected.size > maxBytes) {
      setError(t("文件超过允许的大小。", "The file exceeds the size limit."));
      return;
    }
    try {
      const parsed = backupPreview(
        JSON.parse(await selected.text()) as unknown,
      );
      if (generation === selection.current) {
        setFile(selected);
        setPreview(parsed);
      }
    } catch {
      if (generation === selection.current)
        setError(
          t(
            "请选择 Sakura AI 导出的有效 JSON 备份。",
            "Choose a valid JSON backup exported by Sakura AI.",
          ),
        );
    }
  };
  return (
    <div className="settings-backup">
      <div className="settings-backup-note">
        <Info size={18} />
        <p>
          {t(
            "备份可能包含服务凭据和账户安全信息，请妥善保管。恢复运行中的系统不会替换当前数据库与 Redis 的部署连接。",
            "Backups may contain service credentials and account security information. Store them securely. Restoring a running deployment preserves its database and Redis connections.",
          )}
        </p>
      </div>
      {metadata.isError && (
        <div className="settings-alert" role="alert">
          {t("无法加载备份配置。", "Could not load backup options.")}
          <button className="button" onClick={() => void metadata.refetch()}>
            {t("重试", "Retry")}
          </button>
        </div>
      )}
      <section className="settings-operation-section">
        <header>
          <Download size={20} />
          <div>
            <h3>{t("导出备份", "Export backup")}</h3>
            <p>
              {t(
                "保存当前配置，或为迁移准备备份。",
                "Save the current configuration or prepare for a migration.",
              )}
            </p>
          </div>
        </header>
        <div className="settings-operation-body">
          <label className="settings-operation-label">
            {t("备份范围", "Backup scope")}
            <select
              value={scope}
              onChange={(event) => setScope(event.target.value)}
              disabled={!metadata.data || download.isPending}
            >
              {metadata.data?.scopes.map((item) => (
                <option key={item} value={item}>
                  {labels[item] ?? item}
                </option>
              ))}
            </select>
          </label>
          <button
            className="button primary"
            disabled={!metadata.data || download.isPending}
            onClick={() => download.mutate()}
          >
            {download.isPending ? (
              <LoaderCircle size={16} className="spin" />
            ) : (
              <Download size={16} />
            )}
            {download.isPending
              ? t("正在导出…", "Exporting…")
              : t("下载备份", "Download backup")}
          </button>
        </div>
        {download.isError && (
          <p
            className="settings-operation-feedback settings-error"
            role="alert"
          >
            {t("导出失败，请稍后重试。", "Export failed. Please try again.")}
          </p>
        )}
        {download.isSuccess && (
          <p
            className="settings-operation-feedback settings-status-saved"
            role="status"
          >
            {t(
              "备份已生成，请在浏览器下载中查看。",
              "Backup generated. Check your browser downloads.",
            )}
          </p>
        )}
      </section>
      <section className="settings-operation-section">
        <header>
          <Upload size={20} />
          <div>
            <h3>{t("恢复备份", "Restore backup")}</h3>
            <p>
              {t(
                "仅恢复备份包含的分类；用户备份按现有规则合并。",
                "Restore the categories included in the backup. User backups follow the existing merge rules.",
              )}
            </p>
          </div>
        </header>
        <div className="settings-backup-import">
          <label className="settings-operation-label">
            {t("备份类型", "Backup type")}
            <select
              value={kind}
              disabled={restore.isPending}
              onChange={(event) => {
                setKind(event.target.value as "config" | "users");
                ++selection.current;
                setFile(null);
                setPreview(null);
                setError("");
                restore.reset();
                if (fileInput.current) fileInput.current.value = "";
              }}
            >
              <option value="config">
                {t("应用配置", "Application configuration")}
              </option>
              <option value="users">
                {t("用户与安全信息", "Users & security")}
              </option>
            </select>
          </label>
          <label className="settings-file-picker">
            <FolderOpen size={26} />
            <strong>
              {file?.name ?? t("选择备份文件", "Choose a backup file")}
            </strong>
            <span>
              {maxBytes
                ? `${t("JSON 文件，最大", "JSON file, up to")} ${Math.floor(maxBytes / 1024 / 1024)} MB`
                : t("正在加载…", "Loading…")}
            </span>
            <input
              ref={fileInput}
              type="file"
              accept="application/json,.json"
              disabled={
                metadata.isPending || metadata.isError || restore.isPending
              }
              onChange={(event) => void choose(event.target.files?.[0] ?? null)}
              aria-label={t("选择备份文件", "Choose a backup file")}
            />
          </label>
          {file && preview && (
            <div className="settings-backup-preview">
              <FileJson size={19} />
              <div>
                <strong>{file.name}</strong>
                <p>
                  {preview.sections
                    .map((section) => labels[section] ?? section)
                    .join(" · ") || labels.users}
                  {preview.count !== null
                    ? ` · ${preview.count} ${t("条记录", "records")}`
                    : ""}
                </p>
              </div>
              <button
                className="icon-button"
                disabled={restore.isPending}
                aria-label={t("移除文件", "Remove file")}
                onClick={() => {
                  void choose(null);
                  if (fileInput.current) fileInput.current.value = "";
                }}
              >
                <X size={17} />
              </button>
            </div>
          )}
          {error && (
            <p className="settings-error" role="alert">
              {error}
            </p>
          )}
          <div className="settings-dialog-actions">
            <button
              className="button primary"
              disabled={!file || !preview || restore.isPending}
              onClick={async () => {
                if (
                  await confirm({
                    title: t("恢复此备份？", "Restore this backup?"),
                    description: t(
                      `将恢复“${file?.name}”中的${kind === "config" ? "配置分类，替换这些分类的当前配置" : "用户与安全信息"}。建议先导出当前备份。`,
                      `Restore ${kind === "config" ? "the configuration categories, replacing their current configuration" : "users and security information"} from “${file?.name}”. Export a current backup first.`,
                    ),
                    confirmLabel: t("恢复备份", "Restore backup"),
                    destructive: true,
                  })
                ) {
                  setError("");
                  restore.mutate();
                }
              }}
            >
              {restore.isPending ? (
                <LoaderCircle size={16} className="spin" />
              ) : (
                <Upload size={16} />
              )}
              {restore.isPending
                ? t("正在恢复…", "Restoring…")
                : t("恢复备份", "Restore backup")}
            </button>
          </div>
        </div>
      </section>
      {restore.data?.persisted && (
        <section className="settings-restore-result" role="status">
          <CheckCircle2 size={23} />
          <div>
            <h3>{t("备份已恢复", "Backup restored")}</h3>
            <p>
              {t("新增 {0} · 更新 {1} · 删除 {2} · 未变 {3}", "Created {0} · Updated {1} · Removed {2} · Unchanged {3}", [restore.data.created, restore.data.updated, restore.data.deleted, restore.data.unchanged])}
            </p>
            {restore.data.requires_restart && (
              <p>
                {t(
                  "部分配置需要重启应用后生效。",
                  "Some settings require an application restart.",
                )}
              </p>
            )}
            {!restore.data.audit_recorded && (
              <p>
                {t(
                  "恢复已提交，但审计记录写入失败，请检查运行日志。",
                  "Restore committed, but its audit record could not be saved. Check operational logs.",
                )}
              </p>
            )}
            {restore.data.recovery_codes_portable === false && (
              <p>
                {t(
                  "部分恢复码不能迁移，请为受影响账号重新生成恢复码。",
                  "Some recovery codes cannot be migrated. Regenerate codes for affected accounts.",
                )}
              </p>
            )}
          </div>
        </section>
      )}
    </div>
  );
}
