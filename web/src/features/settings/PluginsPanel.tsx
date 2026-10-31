import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Info, LoaderCircle, Plug, RotateCcw } from "lucide-react";
import { api } from "../../api/client";
import { useLocale } from "../../app/locale";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import { expandSchema, same, schemaError, type SettingsStore } from "./state";
import type { Json, Schema } from "./types";
import { StructuredEditor } from "./StructuredEditor";

interface PluginState {
  plugins: Json;
  profile: string;
  schema: Schema;
  profiles: string[];
  messages: Record<string, string>;
}
export function PluginsPanel({ store }: { store: SettingsStore }) {
  const location = useLocation();
  const panel = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    if (
      (location.hash === "#plugins" ||
        new URLSearchParams(location.search).get("panel") === "plugins") &&
      panel.current
    ) {
      panel.current.open = true;
      panel.current.scrollIntoView?.({ block: "start" });
    }
  }, [location.hash, location.search]);
  const { language, t } = useLocale();
  const confirm = useConfirm();
  const client = useQueryClient();
  const [draft, setDraft] = useState<{ plugins: Json; profile: string } | null>(
    null,
  );
  const [baseline, setBaseline] = useState<{
    plugins: Json;
    profile: string;
  } | null>(null);
  const [raw, setRaw] = useState<string | null>(null);
  const [rawError, setRawError] = useState(false);
  const [validation, setValidation] = useState("");
  const query = useQuery({
    queryKey: ["agent-plugins", language],
    queryFn: () =>
      api<PluginState>(`/api/v1/webui/plugins?language=${language}`),
  });
  const server = query.data
    ? { plugins: query.data.plugins, profile: query.data.profile }
    : null;
  const active = draft ?? server;
  const dirty = rawError || !!(draft && baseline && !same(draft, baseline));
  const conflict = !!(dirty && baseline && server && !same(server, baseline));
  const schema = useMemo(
    () => expandSchema(query.data?.schema ?? {}),
    [query.data?.schema],
  );
  const message = (key: string) => query.data?.messages[key] ?? key;
  const save = useMutation({
    mutationFn: async () => {
      if (!active) return;
      const result = await api<{
        plugins: Json;
        profile: string;
        persisted: boolean;
        audit_recorded: boolean;
      }>("/api/v1/webui/plugins", {
        method: "PUT",
        body: JSON.stringify(active),
      });
      setDraft(null);
      setBaseline(null);
      setRaw(null);
      setRawError(false);
      client.setQueryData<PluginState>(["agent-plugins", language], (old) =>
        old
          ? { ...old, plugins: result.plugins, profile: result.profile }
          : old,
      );
      return { audit_recorded: result.audit_recorded };
    },
    gcTime: 0,
  });
  useEffect(() => {
    store.setExternalDirty("plugins", dirty || save.isPending);
    return () => store.setExternalDirty("plugins", false);
  }, [dirty, save.isPending, store]);
  const edit = (next: { plugins: Json; profile: string }) => {
    if (!baseline && server) setBaseline(server);
    setDraft(next);
    setValidation("");
    save.reset();
  };
  const reset = async () => {
    if (
      dirty &&
      !(await confirm({
        title: t("放弃插件修改？", "Discard plugin changes?"),
        description: t(
          "重新加载服务器上保存的插件与权限配置。",
          "Reload the plugin and permission configuration saved on the server.",
        ),
        destructive: true,
      }))
    )
      return;
    const fresh = await query.refetch();
    if (fresh.isSuccess) {
      setDraft(null);
      setBaseline(null);
      setRaw(null);
      setRawError(false);
      setValidation("");
    }
  };
  return (
    <details className="settings-plugins-panel" id="plugins" ref={panel}>
      <summary>
        <Plug size={18} />
        <span>{t("插件与运行权限", "Plugins & runtime permissions")}</span>
      </summary>
      <div className="settings-plugins-body">
        <p className="settings-hint">
          {t(
            "管理 MCP 服务、生命周期钩子和工具权限。已保存的请求头凭据保持隐藏；修改服务地址后必须重新填写对应凭据。",
            "Manage MCP services, lifecycle hooks and tool permissions. Saved header credentials stay hidden; re-enter them after changing the service address.",
          )}
        </p>
        {query.isPending && <p role="status">{t("正在加载…", "Loading…")}</p>}
        {query.isError && (
          <div className="settings-alert" role="alert">
            {t("无法加载插件配置。", "Could not load plugin configuration.")}
            <button className="button" onClick={() => void query.refetch()}>
              {t("重试", "Retry")}
            </button>
          </div>
        )}
        {active && query.data && (
          <>
            <label className="settings-operation-label">
              {t("运行权限", "Runtime permissions")}
              <select
                value={active.profile}
                disabled={save.isPending}
                onChange={(event) =>
                  edit({ ...active, profile: event.target.value })
                }
              >
                {query.data.profiles.map((profile) => (
                  <option key={profile} value={profile}>
                    {profile === "read_only"
                      ? t("只读", "Read only")
                      : profile === "workspace_write"
                        ? t("允许写入工作区", "Workspace write")
                        : profile === "full_access"
                          ? t("完整访问", "Full access")
                          : profile}
                  </option>
                ))}
              </select>
            </label>
            <StructuredEditor
              value={active.plugins}
              schema={schema}
              label={t("插件配置", "Plugin configuration")}
              message={message}
              disabled={save.isPending}
              onChange={(plugins) => {
                edit({ ...active, plugins });
                setRaw(null);
                setRawError(false);
              }}
            />
            <details className="settings-raw-editor">
              <summary>
                {t(
                  "高级：导入或批量编辑 JSON",
                  "Advanced: import or bulk-edit JSON",
                )}
              </summary>
              <textarea
                aria-label={t("插件 JSON", "Plugin JSON")}
                rows={12}
                spellCheck={false}
                disabled={save.isPending}
                value={raw ?? JSON.stringify(active.plugins, null, 2)}
                onChange={(event) => {
                  const value = event.target.value;
                  setRaw(value);
                  try {
                    const plugins = JSON.parse(value) as Json;
                    edit({ ...active, plugins });
                    setRawError(false);
                  } catch {
                    setRawError(true);
                  }
                }}
              />
            </details>
            {(rawError || validation) && (
              <p className="settings-error" role="alert">
                {rawError
                  ? t(
                      "JSON 格式不完整，请修正后保存。",
                      "JSON is incomplete. Correct it before saving.",
                    )
                  : message(validation)}
              </p>
            )}
            {conflict && (
              <div className="settings-alert" role="alert">
                <Info size={18} />
                {t(
                  "服务器上的插件配置已更新。重新加载后再修改，避免覆盖其他人的变更。",
                  "The plugin configuration changed on the server. Reload before editing to avoid overwriting changes.",
                )}
              </div>
            )}
            {save.isError && (
              <p className="settings-error" role="alert">
                {t(
                  "未能保存。请检查服务 ID、地址、权限和凭据引用；环境凭据只支持 SAKURA_MCP_*。",
                  "Could not save. Check service IDs, addresses, permissions and credential references; environment credentials must use SAKURA_MCP_*.",
                )}
              </p>
            )}
            {save.isSuccess && (
              <p role="status" className="settings-status-saved">
                {save.data?.audit_recorded === false
                  ? t(
                      "配置已保存，但审计记录写入失败。",
                      "Configuration saved, but the audit record could not be written.",
                    )
                  : t("插件配置已保存", "Plugin configuration saved")}
              </p>
            )}
            <div className="settings-dialog-actions">
              <button
                className="button"
                disabled={save.isPending}
                onClick={() => void reset()}
              >
                <RotateCcw size={15} />
                {t("重新加载", "Reload")}
              </button>
              <button
                className="button primary"
                disabled={!dirty || rawError || conflict || save.isPending}
                onClick={async () => {
                  const invalid = schemaError(active.plugins, schema);
                  if (invalid) {
                    setValidation(invalid);
                    return;
                  }
                  if (
                    await confirm({
                      title: t(
                        "更新插件与运行权限？",
                        "Update plugins and runtime permissions?",
                      ),
                      description: t(
                        "新任务将使用保存的工具、钩子和权限配置。",
                        "New tasks will use the saved tools, hooks and permission configuration.",
                      ),
                      confirmLabel: t("保存配置", "Save configuration"),
                    })
                  )
                    save.mutate();
                }}
              >
                {save.isPending ? (
                  <LoaderCircle size={15} className="spin" />
                ) : (
                  <Check size={15} />
                )}
                {t("保存配置", "Save configuration")}
              </button>
            </div>
          </>
        )}
      </div>
    </details>
  );
}
