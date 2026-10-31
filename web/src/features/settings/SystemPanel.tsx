import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Database,
  Eraser,
  GitBranch,
  Info,
  LoaderCircle,
  Power,
  Radio,
} from "lucide-react";
import { api } from "../../api/client";
import { useLocale } from "../../app/locale";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import { Dialog } from "../../components/ui/Dialog";
import type { ConfigField } from "./types";
import type { SettingsStore } from "./state";

interface OperationResult {
  success: boolean;
  restarting?: boolean;
  slug?: string;
}
export function SystemPanel({
  store,
  onApplySlug,
}: {
  store: SettingsStore;
  onApplySlug: (field: ConfigField, value: string) => void;
}) {
  const { language, t } = useLocale();
  const confirm = useConfirm();
  const [resetOpen, setResetOpen] = useState(false);
  const [confirmation, setConfirmation] = useState("");
  const metadata = useQuery({
    queryKey: ["system-operations"],
    queryFn: () =>
      api<{ database_reset_confirmation: string }>("/api/v1/webui/system"),
  });
  const operation = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: unknown }) =>
      api<OperationResult>(
        `/api/v1/webui/system/${path}?language=${language}`,
        {
          method: "POST",
          ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
        },
      ),
  });
  const reset = useMutation({
    mutationFn: () =>
      api<OperationResult>("/api/v1/webui/system/reset-database", {
        method: "POST",
        body: JSON.stringify({ confirmation, language }),
      }),
    onSuccess: (result) => {
      if (result.success) setResetOpen(false);
    },
  });
  const clientId = [...store.entries.values()].find(
    (entry) => entry.field.key === "star_aid_github_app_client_id",
  );
  const slug = [...store.entries.values()].find(
    (entry) => entry.field.key === "star_aid_github_app_slug",
  );
  const restarting = operation.data?.restarting || reset.data?.restarting;
  return (
    <section className="settings-maintenance">
      <h3>{t("系统维护", "System maintenance")}</h3>
      <p className="settings-hint">
        {t(
          "连接测试使用已保存的部署连接。服务操作会影响当前实例。",
          "Connection tests use saved deployment settings. Service operations affect this instance.",
        )}
      </p>
      <div className="settings-operation-section">
        <div className="settings-operation-row">
          <Database size={20} />
          <div>
            <strong>{t("数据库连接", "Database connection")}</strong>
            <p>
              {t(
                "检查当前数据库是否可达",
                "Check the active database connection",
              )}
            </p>
          </div>
          <button
            className="button"
            disabled={operation.isPending || restarting}
            onClick={() =>
              operation.mutate({
                path: "test-connection",
                body: { type: "database", use_saved: true },
              })
            }
          >
            <Radio size={15} />
            {t("测试", "Test")}
          </button>
        </div>
        <div className="settings-operation-row">
          <Database size={20} />
          <div>
            <strong>{t("Redis 连接", "Redis connection")}</strong>
            <p>
              {t(
                "检查当前缓存与队列连接",
                "Check the active cache and queue connection",
              )}
            </p>
          </div>
          <button
            className="button"
            disabled={operation.isPending || restarting}
            onClick={() =>
              operation.mutate({
                path: "test-connection",
                body: { type: "redis", use_saved: true },
              })
            }
          >
            <Radio size={15} />
            {t("测试", "Test")}
          </button>
        </div>
        <div className="settings-operation-row">
          <GitBranch size={20} />
          <div>
            <strong>{t("GitHub App 名称", "GitHub App slug")}</strong>
            <p>
              {t(
                "根据用户授权 App 的 Client ID 查找名称",
                "Look up the user authorization App using its Client ID",
              )}
            </p>
          </div>
          <button
            className="button"
            disabled={operation.isPending || !clientId?.draft || restarting}
            onClick={() =>
              operation.mutate({
                path: "github-app-slug",
                body: { clientId: clientId?.draft },
              })
            }
          >
            {t("查找", "Look up")}
          </button>
        </div>
        {operation.isPending && (
          <p className="settings-operation-feedback" role="status">
            <LoaderCircle size={15} className="spin" />
            {t("正在执行…", "Working…")}
          </p>
        )}
        {(operation.isError || operation.data?.success === false) && (
          <p
            className="settings-operation-feedback settings-error"
            role="alert"
          >
            {t(
              "操作未完成，请检查相关设置与运行日志后重试。",
              "Operation did not complete. Check the settings and operational logs before retrying.",
            )}
          </p>
        )}
        {operation.data?.success && !operation.data.restarting && (
          <p
            className="settings-operation-feedback settings-status-saved"
            role="status"
          >
            {operation.data.slug
              ? operation.data.slug
              : t("连接成功", "Connection successful")}
            {operation.data.slug && slug && (
              <button
                className="button"
                onClick={() =>
                  onApplySlug(slug.field, operation.data?.slug ?? "")
                }
              >
                {t("填入设置", "Use this value")}
              </button>
            )}
          </p>
        )}
      </div>
      <div className="settings-operation-section">
        <div className="settings-operation-row">
          <Power size={20} />
          <div>
            <strong>{t("重启应用", "Restart application")}</strong>
            <p>
              {t(
                "短暂中断服务，以应用需要重启的设置",
                "Briefly interrupt service to apply settings that require a restart",
              )}
            </p>
          </div>
          <button
            className="button"
            disabled={operation.isPending || restarting}
            onClick={async () => {
              if (
                await confirm({
                  title: t("重启 Sakura AI？", "Restart Sakura AI?"),
                  description: t(
                    "当前实例将暂时停止服务。请先保存你的修改。",
                    "This instance will briefly stop serving requests. Save your changes first.",
                  ),
                  confirmLabel: t("重启", "Restart"),
                })
              )
                operation.mutate({ path: "restart" });
            }}
          >
            {t("重启", "Restart")}
          </button>
        </div>
      </div>
      {restarting && (
        <div className="settings-alert" role="status">
          <Info size={19} />
          <div>
            <strong>{t("正在重启应用", "Application restarting")}</strong>
            <p>
              {t(
                "服务恢复后刷新此页面。重置数据库后将进入首次设置。",
                "Refresh this page after the service returns. A database reset returns to initial setup.",
              )}
            </p>
          </div>
          <button className="button" onClick={() => window.location.reload()}>
            {t("刷新", "Refresh")}
          </button>
        </div>
      )}
      <details className="settings-danger-zone">
        <summary>
          <Eraser size={17} />
          {t("重置数据库", "Reset database")}
        </summary>
        <p>
          {t(
            "删除全部应用数据与用户，重新进入首次设置。此操作无法撤销。",
            "Delete all application data and users, then return to initial setup. This cannot be undone.",
          )}
        </p>
        <button
          className="button danger-button"
          disabled={!metadata.data || restarting}
          onClick={() => {
            setConfirmation("");
            reset.reset();
            setResetOpen(true);
          }}
        >
          {t("重置数据库…", "Reset database…")}
        </button>
      </details>
      <Dialog
        open={resetOpen}
        onOpenChange={(open) => {
          if (!reset.isPending) setResetOpen(open);
        }}
        title={t("永久清空数据库", "Permanently erase the database")}
        description={t(
          "所有用户、配置和业务记录都将被删除。请确认你已准备好从首次设置重新开始。",
          "All users, settings and business records will be deleted. Confirm that you are ready to start again from initial setup.",
        )}
      >
        <div className="settings-reset-confirmation">
          <p>{t("请输入以下确认文字：", "Type the following confirmation:")}</p>
          <code>{metadata.data?.database_reset_confirmation}</code>
          <input
            aria-label={t("数据库重置确认", "Database reset confirmation")}
            value={confirmation}
            autoComplete="off"
            disabled={reset.isPending}
            onChange={(event) => setConfirmation(event.target.value)}
          />
          {reset.isError && (
            <p className="settings-error" role="alert">
              {t(
                "无法确认重置结果。请检查服务状态后再试。",
                "Could not confirm the reset outcome. Check service status before trying again.",
              )}
            </p>
          )}
          <div className="settings-dialog-actions">
            <button
              className="button"
              disabled={reset.isPending}
              onClick={() => setResetOpen(false)}
            >
              {t("取消", "Cancel")}
            </button>
            <button
              className="button danger-button"
              disabled={
                !metadata.data ||
                confirmation !== metadata.data.database_reset_confirmation ||
                reset.isPending
              }
              onClick={() => reset.mutate()}
            >
              {reset.isPending ? (
                <LoaderCircle size={15} className="spin" />
              ) : (
                <Eraser size={15} />
              )}
              {t("永久删除并重启", "Erase and restart")}
            </button>
          </div>
        </div>
      </Dialog>
    </section>
  );
}
