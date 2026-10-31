import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import {
  CheckCircle2,
  Circle,
  KeyRound,
  LoaderCircle,
  Pencil,
  Plus,
  Radio,
  RefreshCw,
  Server,
  Trash2,
} from "lucide-react";
import { api, ApiError } from "../../api/client";
import { useLocale } from "../../app/locale";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import { Dialog } from "../../components/ui/Dialog";
import type { Account } from "./types";
import type { SettingsStore } from "./state";

interface SavedAccount extends Account {
  protocol: string;
  api_base: string;
  has_key: boolean;
  region: string;
  notes: string;
  revision: number;
  updated_at: number;
}
interface Provider {
  id: string;
  label: string;
  family: string;
  base_url: string;
  endpoints: Record<string, string>;
  default_models: string[];
  supported_families: string[];
  models: { model_id: string; display_name: string }[];
  supports_model_discovery: boolean;
}
interface Envelope<T> {
  success: boolean;
  data: T;
  error?: string;
  code?: string;
}
async function request<T>(path: string, init?: RequestInit) {
  const result = await api<Envelope<T>>(path, init);
  if (result.success === false) throw new ApiError(422, result);
  return result.data;
}

const accountSchema = z.object({
  name: z.string().trim().min(1),
  provider_id: z.string().min(1),
  protocol: z.string().min(1),
  api_base: z.string().trim(),
  api_key: z
    .string()
    .refine((value) => !value.includes("****") && value !== "[REDACTED]"),
  region: z.string(),
  default_model: z.string().trim(),
  models_text: z.string(),
  enabled: z.boolean(),
  notes: z.string(),
});
type AccountForm = z.infer<typeof accountSchema>;

export function accountPayload(values: AccountForm, account?: SavedAccount) {
  const { models_text, ...payload } = values;
  return {
    ...payload,
    models: [
      ...new Set(
        models_text
          .split(/[\n,]/)
          .map((value) => value.trim())
          .filter(Boolean),
      ),
    ],
    ...(account
      ? {
          id: account.id,
          revision: account.revision,
          updated_at: account.updated_at,
        }
      : {}),
  };
}

export function AccountsPanel({ store }: { store: SettingsStore }) {
  const { t } = useLocale();
  const confirm = useConfirm();
  const client = useQueryClient();
  const [editor, setEditor] = useState<SavedAccount | "new" | null>(null);
  const [feedback, setFeedback] = useState<{
    id: string;
    ok: boolean;
    text: string;
  } | null>(null);
  const accounts = useQuery({
    queryKey: ["ai-accounts"],
    queryFn: () =>
      api<Envelope<{ accounts: SavedAccount[] }>>("/api/v1/config/ai/accounts"),
  });
  const catalog = useQuery({
    queryKey: ["ai-catalog"],
    queryFn: () =>
      api<Envelope<{ providers: Provider[] }>>("/api/v1/config/ai/catalog"),
  });
  const probe = useMutation({
    mutationFn: async (account: SavedAccount) =>
      request<{
        ok?: boolean;
        success?: boolean;
        models?: string[];
        message?: string;
        error?: string;
        status?: string;
      }>(`/api/v1/config/ai/accounts/${encodeURIComponent(account.id)}/test`, {
        method: "POST",
      }),
    onSuccess: (result, account) =>
      setFeedback({
        id: account.id,
        ok: result.ok === true || result.success === true,
        text:
          result.ok === true || result.success === true
            ? t("连接成功", "Connection successful")
            : t(
                "连接未通过。请检查地址、凭据和默认模型。",
                "Connection failed. Check the endpoint, credential and default model.",
              ),
      }),
    onError: (_error, account) =>
      setFeedback({
        id: account.id,
        ok: false,
        text: t(
          "无法完成连接测试，请稍后重试。",
          "Could not complete the connection test. Try again later.",
        ),
      }),
  });
  const remove = useMutation({
    mutationFn: (account: SavedAccount) =>
      request(`/api/v1/config/ai/accounts/${encodeURIComponent(account.id)}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["ai-accounts"] });
      void client.invalidateQueries({ queryKey: ["settings", "ai"] });
    },
    onError: (_error, account) =>
      setFeedback({
        id: account.id,
        ok: false,
        text: t(
          "无法删除。请先移除此账号的模型绑定，并检查计费配置。",
          "Could not delete. Remove role bindings and check billing configuration first.",
        ),
      }),
  });
  const rows = accounts.data?.data.accounts ?? [];
  return (
    <section className="settings-accounts-panel">
      <header>
        <div>
          <h3>{t("AI 账号", "AI accounts")}</h3>
          <p>
            {t(
              "连接供应商后，在下方为不同任务选择模型。",
              "Connect a provider, then assign models to your workflows below.",
            )}
          </p>
        </div>
        <button
          className="button primary"
          disabled={!catalog.data || catalog.isError}
          onClick={() => setEditor("new")}
        >
          <Plus size={16} />
          {t("添加账号", "Add account")}
        </button>
      </header>
      {(accounts.isPending || catalog.isPending) && (
        <p className="settings-panel-status" role="status">
          <LoaderCircle size={16} className="spin" />
          {t("正在加载账号…", "Loading accounts…")}
        </p>
      )}
      {(accounts.isError || catalog.isError) && (
        <div className="settings-panel-status" role="alert">
          {t(
            "无法加载账号或供应商目录。",
            "Could not load accounts or provider catalog.",
          )}
          <button
            className="button"
            onClick={() => {
              void accounts.refetch();
              void catalog.refetch();
            }}
          >
            <RefreshCw size={14} />
            {t("重试", "Retry")}
          </button>
        </div>
      )}
      {!accounts.isPending && !accounts.isError && rows.length === 0 && (
        <div className="settings-accounts-empty">
          <Server size={28} />
          <h4>
            {t("连接你的第一个 AI 账号", "Connect your first AI account")}
          </h4>
          <p>
            {t(
              "支持多供应商、多个账号和有序的备用模型。",
              "Use multiple providers, accounts and ordered fallback models.",
            )}
          </p>
        </div>
      )}
      {rows.map((account) => (
        <div className="settings-account-row" key={account.id}>
          <div className="settings-account-mark">
            <Server size={19} />
          </div>
          <div className="settings-account-info">
            <strong>{account.name}</strong>
            <p>
              {catalog.data?.data.providers.find(
                (provider) => provider.id === account.provider_id,
              )?.label ?? account.provider_id}
              <span>·</span>
              {account.default_model || t("未设置默认模型", "No default model")}
            </p>
            <div className="settings-account-meta">
              <span>
                {account.enabled ? (
                  <CheckCircle2 size={12} />
                ) : (
                  <Circle size={12} />
                )}
                {account.enabled
                  ? t("已启用", "Enabled")
                  : t("已停用", "Disabled")}
              </span>
              <span>
                <KeyRound size={12} />
                {account.has_key
                  ? t("已配置凭据", "Credential configured")
                  : t("未配置凭据", "No credential")}
              </span>
            </div>
            {feedback?.id === account.id && (
              <p
                className={
                  feedback.ok ? "settings-status-saved" : "settings-error"
                }
                role="status"
              >
                {feedback.text}
              </p>
            )}
          </div>
          <div className="settings-account-actions">
            <button
              type="button"
              className="icon-button"
              title={t("测试连接", "Test connection")}
              aria-label={`${t("测试连接", "Test connection")}: ${account.name}`}
              disabled={probe.isPending || remove.isPending}
              onClick={() => probe.mutate(account)}
            >
              {probe.isPending && probe.variables.id === account.id ? (
                <LoaderCircle size={17} className="spin" />
              ) : (
                <Radio size={17} />
              )}
            </button>
            <button
              type="button"
              className="icon-button"
              aria-label={`${t("编辑", "Edit")}: ${account.name}`}
              disabled={remove.isPending}
              onClick={() => setEditor(account)}
            >
              <Pencil size={17} />
            </button>
            <button
              type="button"
              className="icon-button"
              aria-label={`${t("删除", "Delete")}: ${account.name}`}
              disabled={remove.isPending || probe.isPending}
              onClick={async () => {
                if (
                  await confirm({
                    title: t("删除 AI 账号？", "Delete AI account?"),
                    description: t("将删除“{0}”。仍被任务角色引用的账号无法删除。", "Delete “{0}”? Accounts still assigned to a role cannot be deleted.", [account.name]),
                    destructive: true,
                    confirmLabel: t("删除", "Delete"),
                  })
                )
                  remove.mutate(account);
              }}
            >
              <Trash2 size={17} />
            </button>
          </div>
        </div>
      ))}
      {editor && catalog.data && (
        <AccountEditor
          key={editor === "new" ? "new" : editor.id}
          account={editor === "new" ? undefined : editor}
          providers={catalog.data.data.providers}
          store={store}
          onClose={() => setEditor(null)}
        />
      )}
    </section>
  );
}

function AccountEditor({
  account,
  providers,
  store,
  onClose,
}: {
  account?: SavedAccount;
  providers: Provider[];
  store: SettingsStore;
  onClose: () => void;
}) {
  const { t } = useLocale();
  const confirm = useConfirm();
  const client = useQueryClient();
  const first = providers[0];
  const [error, setError] = useState("");
  const form = useForm<AccountForm>({
    resolver: zodResolver(accountSchema),
    defaultValues: {
      name: account?.name ?? "",
      provider_id: account?.provider_id ?? first?.id ?? "",
      protocol: account?.protocol ?? first?.family ?? "",
      api_base: account?.api_base ?? first?.base_url ?? "",
      api_key: "",
      region: account?.region ?? "",
      default_model: account?.default_model ?? first?.default_models[0] ?? "",
      models_text: account?.models.join("\n") ?? "",
      enabled: account?.enabled ?? true,
      notes: account?.notes ?? "",
    },
  });
  const providerId = form.watch("provider_id");
  const protocol = form.watch("protocol");
  const selected = providers.find((provider) => provider.id === providerId);
  const save = useMutation({
    mutationFn: (values: AccountForm) =>
      request<{ account: SavedAccount }>("/api/v1/config/ai/accounts", {
        method: "POST",
        body: JSON.stringify(accountPayload(values, account)),
      }),
    gcTime: 0,
    onSuccess: () => {
      form.reset();
      void client.invalidateQueries({ queryKey: ["ai-accounts"] });
      void client.invalidateQueries({ queryKey: ["settings", "ai"] });
      onClose();
    },
    onError: (failure) =>
      setError(
        failure instanceof ApiError && failure.status === 409
          ? t(
              "此账号已被其他人修改。请关闭编辑器并重新加载后再试。",
              "This account changed elsewhere. Close the editor and reload before trying again.",
            )
          : t(
              "保存失败。请检查供应商地址、协议、模型与计费配置。",
              "Could not save. Check the endpoint, protocol, model and billing configuration.",
            ),
      ),
  });
  const discovery = useMutation({
    mutationFn: () =>
      request<{ ok?: boolean; models?: (string | { id: string })[] }>(
        `/api/v1/config/ai/accounts/${encodeURIComponent(account?.id ?? "")}/models`,
        { method: "POST" },
      ),
    onSuccess: (result) => {
      if (result.models?.length)
        form.setValue(
          "models_text",
          result.models
            .map((model) => (typeof model === "string" ? model : model.id))
            .join("\n"),
          { shouldDirty: true },
        );
      else
        setError(
          t(
            "未发现可用模型。你仍可以手动输入模型 ID。",
            "No models discovered. You can still enter model IDs manually.",
          ),
        );
    },
    onError: () =>
      setError(
        t(
          "无法获取模型列表，请检查已保存的连接信息。",
          "Could not discover models. Check the saved connection settings.",
        ),
      ),
  });
  useEffect(() => {
    store.setExternalDirty("account", form.formState.isDirty || save.isPending);
    return () => store.setExternalDirty("account", false);
  }, [store, form.formState.isDirty, save.isPending]);
  const close = async () => {
    if (save.isPending) return;
    if (
      form.formState.isDirty &&
      !(await confirm({
        title: t("放弃账号修改？", "Discard account changes?"),
        description: t(
          "尚未保存的账号设置将被丢弃。",
          "Unsaved account settings will be lost.",
        ),
        destructive: true,
      }))
    )
      return;
    onClose();
  };
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) void close();
      }}
      title={
        account
          ? t("编辑 AI 账号", "Edit AI account")
          : t("添加 AI 账号", "Add AI account")
      }
      description={t(
        "凭据只用于连接服务，已保存的密钥不会显示。",
        "Credentials are used to connect to the service. Saved secrets are never displayed.",
      )}
    >
      <form
        className="settings-account-form"
        onSubmit={form.handleSubmit((values) => {
          setError("");
          save.mutate(values);
        })}
      >
        <fieldset disabled={save.isPending}>
          <label>
            {t("账号名称", "Account name")}
            <input
              autoFocus
              {...form.register("name")}
              placeholder={t("例如：团队的主账号", "e.g. Team primary account")}
              aria-invalid={!!form.formState.errors.name}
            />
          </label>
          <div className="settings-form-columns">
            <label>
              {t("供应商", "Provider")}
              <select
                {...form.register("provider_id", {
                  onChange: (event: { target: { value: string } }) => {
                    const provider = providers.find(
                      (item) => item.id === event.target.value,
                    );
                    if (provider) {
                      form.setValue("protocol", provider.family, {
                        shouldDirty: true,
                      });
                      form.setValue("api_base", provider.base_url, {
                        shouldDirty: true,
                      });
                      form.setValue(
                        "default_model",
                        provider.default_models[0] ?? "",
                        { shouldDirty: true },
                      );
                    }
                  },
                })}
              >
                {providers.map((provider) => (
                  <option key={provider.id} value={provider.id}>
                    {provider.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              {t("协议", "Protocol")}
              <select
                {...form.register("protocol", {
                  onChange: (event: { target: { value: string } }) => {
                    const endpoint = selected?.endpoints[event.target.value];
                    if (endpoint)
                      form.setValue("api_base", endpoint, {
                        shouldDirty: true,
                      });
                  },
                })}
              >
                {(selected?.supported_families ?? [protocol]).map((family) => (
                  <option key={family} value={family}>
                    {family}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <label>
            {t("服务地址", "API endpoint")}
            <input
              {...form.register("api_base")}
              type="url"
              placeholder={selected?.base_url}
            />
          </label>
          <label>
            {t("API 密钥", "API key")}
            <input
              {...form.register("api_key")}
              type="password"
              autoComplete="new-password"
              aria-invalid={!!form.formState.errors.api_key}
              placeholder={
                account?.has_key
                  ? t(
                      "留空保留已保存的密钥",
                      "Leave blank to keep the saved key",
                    )
                  : t("输入 API 密钥", "Enter API key")
              }
            />
          </label>
          <label>
            {t("默认模型", "Default model")}
            <input
              {...form.register("default_model")}
              list="account-model-suggestions"
            />
            <datalist id="account-model-suggestions">
              {selected?.models.map((model) => (
                <option key={model.model_id} value={model.model_id}>
                  {model.display_name}
                </option>
              ))}
            </datalist>
          </label>
          <details className="settings-account-advanced">
            <summary>{t("更多选项", "More options")}</summary>
            <label>
              {t("模型列表（每行一个）", "Model list (one per line)")}
              <textarea {...form.register("models_text")} rows={4} />
            </label>
            {account && selected?.supports_model_discovery && (
              <button
                type="button"
                className="button"
                disabled={discovery.isPending}
                onClick={() => discovery.mutate()}
              >
                <RefreshCw size={14} />
                {discovery.isPending
                  ? t("正在获取…", "Discovering…")
                  : t("获取已保存账号的模型", "Discover saved account models")}
              </button>
            )}
            <label>
              {t("区域", "Region")}
              <input {...form.register("region")} />
            </label>
            <label>
              {t("备注", "Notes")}
              <textarea {...form.register("notes")} rows={2} />
            </label>
          </details>
          <label className="settings-checkbox">
            <input {...form.register("enabled")} type="checkbox" />
            {t("启用此账号", "Enable this account")}
          </label>
        </fieldset>
        {Object.keys(form.formState.errors).length > 0 && (
          <p className="settings-error" role="alert">
            {t(
              "请填写账号名称，并检查凭据格式。",
              "Enter an account name and check the credential format.",
            )}
          </p>
        )}
        {error && (
          <p className="settings-error" role="alert">
            {error}
          </p>
        )}
        <div className="settings-dialog-actions">
          <button
            type="button"
            className="button"
            disabled={save.isPending}
            onClick={() => void close()}
          >
            {t("取消", "Cancel")}
          </button>
          <button className="button primary" disabled={save.isPending}>
            {save.isPending ? (
              <LoaderCircle size={15} className="spin" />
            ) : (
              <CheckCircle2 size={15} />
            )}
            {save.isPending
              ? t("正在保存…", "Saving…")
              : t("保存账号", "Save account")}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
