import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, ApiError } from "../../api/client";
import { useLocale } from "../../app/locale";
import { PublicShell } from "../../components/layout/PublicShell";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import {
  Disclosure,
  ErrorState,
  LoadingState,
} from "../../components/workflow/Workflow";
import "./setup.css";
import "../billing/billing.css";

type Meta = {
  state: string;
  configured?: Record<string, boolean>;
  missing_fields?: unknown;
};
type Result = {
  success: boolean;
  message?: string;
  setup_values?: Record<string, string>;
  total_count?: number;
};
type Field = [string, string, string, ("secret" | "text" | "multiline")?];
const groups: { id: string; label: [string, string]; fields: Field[] }[] = [
  {
    id: "database",
    label: ["连接", "Connections"],
    fields: [
      ["DATABASE_URL", "数据库连接地址", "Database URL", "secret"],
      ["REDIS_URL", "Redis 连接地址", "Redis URL", "secret"],
    ],
  },
  {
    id: "github",
    label: ["GitHub", "GitHub"],
    fields: [
      ["GITHUB_APP_ID", "GitHub App ID", "GitHub App ID"],
      [
        "GITHUB_PRIVATE_KEY",
        "GitHub App 私钥",
        "GitHub App private key",
        "multiline",
      ],
      ["GITHUB_WEBHOOK_SECRET", "Webhook 密钥", "Webhook secret", "secret"],
      [
        "GITHUB_OAUTH_CLIENT_ID",
        "登录 OAuth Client ID",
        "Sign-in OAuth Client ID",
      ],
      [
        "GITHUB_OAUTH_CLIENT_SECRET",
        "登录 OAuth Client Secret",
        "Sign-in OAuth Client Secret",
        "secret",
      ],
      ["GITHUB_OAUTH_REDIRECT_URI", "OAuth 回调地址", "OAuth callback URL"],
    ],
  },
  {
    id: "admin",
    label: ["管理员", "Administrator"],
    fields: [
      [
        "ADMIN_GITHUB_USERNAME",
        "管理员 GitHub 用户名",
        "Administrator GitHub username",
      ],
      [
        "ADMIN_TELEGRAM_ID",
        "管理员 Telegram ID（可选）",
        "Administrator Telegram ID (optional)",
      ],
      ["APP_DOMAIN", "应用域名", "Application domain"],
      ["APP_PORT", "应用端口", "Application port"],
      [
        "APP_TIMEZONE",
        "应用时区（留空跟随系统）",
        "Application timezone (blank uses system)",
      ],
    ],
  },
  {
    id: "notifications",
    label: ["通知（可选）", "Notifications (optional)"],
    fields: [
      [
        "TELEGRAM_ENABLED",
        "启用 Telegram（true / false）",
        "Enable Telegram (true / false)",
      ],
      [
        "TELEGRAM_BOT_TOKEN",
        "Telegram Bot Token",
        "Telegram Bot Token",
        "secret",
      ],
      [
        "EMAIL_ENABLED",
        "启用邮件（true / false）",
        "Enable email (true / false)",
      ],
      ["SMTP_HOST", "SMTP 主机", "SMTP host"],
      ["SMTP_PORT", "SMTP 端口", "SMTP port"],
      ["SMTP_USERNAME", "SMTP 用户名", "SMTP username"],
      ["SMTP_PASSWORD", "SMTP 密码", "SMTP password", "secret"],
      ["SMTP_FROM", "发件地址", "Sender address"],
      ["SMTP_FROM_NAME", "发件人名称", "Sender name"],
      ["SMTP_SECURITY", "SMTP 安全方式", "SMTP security mode"],
    ],
  },
  {
    id: "rag",
    label: ["检索（可选）", "Retrieval (optional)"],
    fields: [
      ["EMBEDDING_PROVIDER", "嵌入模型供应商", "Embedding provider"],
      ["EMBEDDING_MODEL", "嵌入模型", "Embedding model"],
      ["EMBEDDING_API_KEY", "嵌入 API Key", "Embedding API key", "secret"],
      ["EMBEDDING_BASE_URL", "嵌入接口地址", "Embedding endpoint"],
      ["EMBEDDING_DIMENSION", "嵌入维度", "Embedding dimensions"],
      ["RERANK_PROVIDER", "重排模型供应商", "Rerank provider"],
      ["RERANK_MODEL", "重排模型", "Rerank model"],
      ["RERANK_API_KEY", "重排 API Key", "Rerank API key", "secret"],
      ["RERANK_BASE_URL", "重排接口地址", "Rerank endpoint"],
    ],
  },
];

export default function SetupPage() {
  return (
    <PublicShell>
      <SetupWizard />
    </PublicShell>
  );
}
function SetupWizard() {
  const { t } = useLocale();
  const confirm = useConfirm();
  const credential = useRef("");
  const [token, setToken] = useState("");
  const [authorized, setAuthorized] = useState(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const [group, setGroup] = useState(0);
  const [backup, setBackup] = useState<string | null>(null);
  const [importCount, setImportCount] = useState(0);
  const [complete, setComplete] = useState(false);
  const [message, setMessage] = useState("");
  const [databaseReport, setDatabaseReport] = useState<Record<
    string,
    unknown
  > | null>(null);
  const [repairUser, setRepairUser] = useState("");
  const [repairPassword, setRepairPassword] = useState("");
  const headers = () => ({ "X-Setup-Token": credential.current });
  const state = useQuery({
    queryKey: ["setup-browser-state", authorized],
    queryFn: ({ signal }) =>
      api<Meta>("/api/v1/setup/browser", { headers: headers(), signal }),
    retry: false,
    gcTime: 0,
  });
  const mutation = useMutation({
    mutationFn: (operation: () => Promise<void>) => operation(),
    gcTime: 0,
  });
  async function run(operation: () => Promise<void>) {
    setMessage("");
    try {
      await mutation.mutateAsync(operation);
    } catch (error) {
      const body =
        error instanceof ApiError &&
        error.body &&
        typeof error.body === "object"
          ? (error.body as Record<string, unknown>)
          : {};
      setMessage(
        typeof body.message === "string"
          ? body.message
          : typeof body.error === "string"
            ? body.error
            : t(
                "操作未完成，请检查输入后重试。",
                "The action did not complete. Check the values and try again.",
              ),
      );
    } finally {
      mutation.reset();
    }
  }
  function verify(event: FormEvent) {
    event.preventDefault();
    credential.current = token.trim();
    void run(async () => {
      await api("/api/v1/setup/browser/verify", {
        method: "POST",
        headers: headers(),
      });
      setToken("");
      setAuthorized(true);
    });
  }
  async function databaseAction(repair: boolean) {
    if (
      repair &&
      !(await confirm({
        title: t(
          "修复数据库初始化权限？",
          "Repair database initialization privileges?",
        ),
        description: t(
          "使用临时管理员凭据完成初始化所需操作；凭据不会保存。",
          "Use temporary administrator credentials for initialization. These credentials are not saved.",
        ),
        destructive: true,
      }))
    )
      return;
    await run(async () => {
      try {
        const result = await api<{ data?: Record<string, unknown> }>(
          `/api/v1/setup/database-${repair ? "repair" : "preflight"}`,
          {
            method: "POST",
            headers: headers(),
            body: JSON.stringify({
              database_url: values.DATABASE_URL || "",
              ...(repair
                ? {
                    username: repairUser,
                    password: repairPassword,
                    confirmed: true,
                  }
                : {}),
            }),
          },
        );
        setDatabaseReport(result.data ?? {});
      } catch (error) {
        if (
          error instanceof ApiError &&
          error.body &&
          typeof error.body === "object" &&
          "data" in error.body
        )
          setDatabaseReport(error.body.data as Record<string, unknown>);
        throw error;
      } finally {
        if (repair) {
          setRepairUser("");
          setRepairPassword("");
        }
      }
    });
  }
  async function importBackup(file?: File) {
    if (!file) return;
    await run(async () => {
      const content = await file.text();
      const result = await api<Result>("/api/v1/setup/browser/backup", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ content }),
      });
      if (!result.success) throw new Error("invalid_backup");
      setBackup(content);
      setImportCount(result.total_count ?? 0);
      setValues((current) => {
        const next = { ...current };
        for (const [key, value] of Object.entries(result.setup_values ?? {})) {
          const name = key.toUpperCase();
          if (!next[name] && !state.data?.configured?.[name])
            next[name] = value;
        }
        return next;
      });
    });
  }
  async function finish() {
    if (
      !(await confirm({
        title: t(
          "保存配置并启动 Sakura AI？",
          "Save configuration and start Sakura AI?",
        ),
        description: t(
          "系统将保存配置、创建管理员并重启应用。",
          "The system will save configuration, create the administrator and restart the application.",
        ),
      }))
    )
      return;
    await run(async () => {
      const result = await api<Result>("/api/v1/setup/browser/complete", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({
          ...values,
          ...(backup ? { CONFIG_BACKUP: backup } : {}),
        }),
      });
      if (!result.success) {
        setMessage(result.message || t("配置未完成", "Setup did not complete"));
        return;
      }
      credential.current = "";
      setValues({});
      setBackup(null);
      setComplete(true);
    });
  }
  if (complete || state.data?.state === "completed")
    return (
      <main className="public-auth-card">
        <h1>{t("配置已完成", "Setup complete")}</h1>
        <p>
          {t(
            "应用正在准备登录入口。",
            "The application is preparing the sign-in page.",
          )}
        </p>
        <a className="button primary" href="/app/login">
          {t("前往登录", "Continue to sign in")}
        </a>
      </main>
    );
  if (!authorized)
    return (
      <main className="public-auth-card">
        <h1>{t("初始化 Sakura AI", "Set up Sakura AI")}</h1>
        <p className="muted">
          {t(
            "输入配置目录中 .setup-token 文件里的初始化凭据。",
            "Enter the setup credential from the .setup-token file in the configuration directory.",
          )}
        </p>
        <form className="billing-form" onSubmit={verify}>
          <label>
            {t("初始化凭据", "Setup credential")}
            <input
              type="password"
              autoComplete="off"
              required
              value={token}
              onChange={(event) => setToken(event.target.value)}
            />
          </label>
          {message && (
            <p className="danger-text" role="alert">
              {message}
            </p>
          )}
          <button className="button primary" disabled={mutation.isPending}>
            {t("继续", "Continue")}
          </button>
        </form>
      </main>
    );
  if (state.isPending) return <LoadingState />;
  if (state.isError)
    return (
      <main className="public-auth-card">
        <ErrorState retry={() => void state.refetch()} />
      </main>
    );
  const selected = groups[group];
  return (
    <main className="setup-workspace">
      <h1>{t("配置工作空间", "Configure your workspace")}</h1>
      <p className="muted">
        {t(
          "完成基础连接与管理员配置后即可开始使用。AI 账号可在设置中管理。",
          "Configure connections and your administrator to get started. Manage AI accounts in Settings.",
        )}
      </p>
      <div className="setup-layout">
        <nav>
          {groups.map((item, index) => (
            <button
              key={item.id}
              className={group === index ? "active" : ""}
              onClick={() => setGroup(index)}
            >
              <span>{index + 1}</span>
              {t(...item.label)}
            </button>
          ))}
        </nav>
        <section>
          <h2>{t(...selected.label)}</h2>
          <div className="billing-form">
            {selected.fields.map(([key, zh, en, kind]) => (
              <label key={key}>
                {t(zh, en)}
                {kind === "multiline" ? (
                  <textarea
                    rows={7}
                    value={values[key] || ""}
                    onChange={(event) =>
                      setValues({ ...values, [key]: event.target.value })
                    }
                  />
                ) : (
                  <input
                    type={kind === "secret" ? "password" : "text"}
                    autoComplete="off"
                    value={values[key] || ""}
                    placeholder={
                      state.data?.configured?.[key]
                        ? t(
                            "已配置，留空沿用",
                            "Configured; leave blank to keep",
                          )
                        : ""
                    }
                    onChange={(event) =>
                      setValues({ ...values, [key]: event.target.value })
                    }
                  />
                )}
              </label>
            ))}
          </div>
          {selected.id === "database" && (
            <div className="setup-diagnostics">
              <button
                className="button"
                disabled={mutation.isPending || !values.DATABASE_URL}
                onClick={() => void databaseAction(false)}
              >
                {t("检查数据库连接", "Check database connection")}
              </button>
              {databaseReport && (
                <p role="status">
                  {String(databaseReport.message || databaseReport.code || "")}
                </p>
              )}
              {databaseReport && databaseReport.success !== true && (
                <Disclosure
                  title={t(
                    "使用临时管理员修复初始化",
                    "Repair initialization with a temporary administrator",
                  )}
                >
                  <div className="billing-form">
                    <label>
                      {t(
                        "数据库管理员用户名",
                        "Database administrator username",
                      )}
                      <input
                        autoComplete="off"
                        value={repairUser}
                        onChange={(event) => setRepairUser(event.target.value)}
                      />
                    </label>
                    <label>
                      {t("数据库管理员密码", "Database administrator password")}
                      <input
                        type="password"
                        autoComplete="off"
                        value={repairPassword}
                        onChange={(event) =>
                          setRepairPassword(event.target.value)
                        }
                      />
                    </label>
                    <button
                      className="button"
                      disabled={
                        mutation.isPending || !repairUser || !repairPassword
                      }
                      onClick={() => void databaseAction(true)}
                    >
                      {t("修复初始化", "Repair initialization")}
                    </button>
                  </div>
                </Disclosure>
              )}
            </div>
          )}
          {message && (
            <p className="danger-text" role="alert">
              {message}
            </p>
          )}
          <div className="confirm-actions">
            {group > 0 && (
              <button className="button" onClick={() => setGroup(group - 1)}>
                {t("上一步", "Back")}
              </button>
            )}
            {group < groups.length - 1 && (
              <button className="button" onClick={() => setGroup(group + 1)}>
                {t("下一步", "Next")}
              </button>
            )}
            <button
              className="button primary"
              disabled={mutation.isPending}
              onClick={() => void finish()}
            >
              {t("完成配置", "Finish setup")}
            </button>
          </div>
        </section>
      </div>
      <Disclosure title={t("从备份恢复", "Restore from backup")}>
        <p>
          {t(
            "选择配置备份。当前部署的连接设置会保留。",
            "Choose a configuration backup. Existing deployment connection settings are preserved.",
          )}
        </p>
        <input
          type="file"
          accept=".json"
          onChange={(event) => void importBackup(event.target.files?.[0])}
        />
        {backup && (
          <p>
            {t("已载入 {0} 项配置。", "Loaded {0} configuration entries.", [
              importCount,
            ])}
          </p>
        )}
      </Disclosure>
    </main>
  );
}
