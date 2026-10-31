import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Link,
  useBlocker,
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import {
  ArrowRight,
  Bot,
  Check,
  ChevronDown,
  DatabaseBackup,
  GitPullRequest,
  Info,
  LoaderCircle,
  MessageSquare,
  RotateCcw,
  Search,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Tags,
  UserRound,
  X,
} from "lucide-react";
import { api, setCsrfToken } from "../../api/client";
import { useLocale } from "../../app/locale";
import { useAuth } from "../../app/auth";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import { LoadingState } from "../../components/common/states";
import { BindingEditor, StructuredEditor } from "./StructuredEditor";
import { AccountsPanel } from "./AccountsPanel";
import { BackupPanel } from "./BackupPanel";
import { SystemPanel } from "./SystemPanel";
import SecurityPanel from "../security/SecurityPanel";
import NotificationsPanel from "../notifications/NotificationsPanel";
import { PluginsPanel } from "./PluginsPanel";
import { isDirty, SettingsStore } from "./state";
import type {
  Account,
  Category,
  ConfigField,
  Json,
  SettingsResponse,
} from "./types";
import "./settings.css";

const AboutPanel = lazy(() => import("./AboutPanel"));

const categoryItems = [
  {
    id: "personal",
    zh: "个人偏好",
    en: "Personal",
    icon: UserRound,
    description: [
      "界面、语言与个人体验",
      "Appearance, language and your experience",
    ],
  },
  {
    id: "ai",
    zh: "AI 与模型",
    en: "AI & models",
    icon: Sparkles,
    description: [
      "连接 AI 账号，选择模型和调用策略",
      "Connect accounts, choose models and tune requests",
    ],
  },
  {
    id: "review",
    zh: "代码审查",
    en: "Code review",
    icon: GitPullRequest,
    description: [
      "审查策略、上下文与输出规则",
      "Review strategies, context and output rules",
    ],
  },
  {
    id: "issue",
    zh: "Issue 分析",
    en: "Issue analysis",
    icon: MessageSquare,
    description: [
      "分析流程、分类与回复内容",
      "Analysis, classification and responses",
    ],
  },
  {
    id: "agent",
    zh: "Agent",
    en: "Agents",
    icon: Bot,
    description: [
      "任务执行、工具与搜索能力",
      "Task execution, tools and search",
    ],
  },
  {
    id: "labels",
    zh: "标签与扫描",
    en: "Labels & scans",
    icon: Tags,
    description: [
      "标签定义、推荐与仓库扫描",
      "Label definitions, recommendations and repository scans",
    ],
  },
  {
    id: "system",
    zh: "系统",
    en: "System",
    icon: Settings2,
    description: [
      "应用、身份验证与服务配置",
      "Application, authentication and service configuration",
    ],
  },
  {
    id: "backup",
    zh: "备份与恢复",
    en: "Backup & restore",
    icon: DatabaseBackup,
    description: [
      "导出配置或从备份中恢复",
      "Export configuration or restore a backup",
    ],
  },
  {
    id: "about",
    zh: "关于",
    en: "About",
    icon: Info,
  },
] as const;

function safeHash(hash: string) {
  try {
    return decodeURIComponent(hash.slice(1));
  } catch {
    return "";
  }
}

export default function SettingsPage() {
  const { category = "personal" } = useParams();
  const { language, t } = useLocale();
  const { user } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const search = searchParams.get("q") ?? "";
  const index = useQuery({
    queryKey: ["settings-index", language],
    queryFn: () =>
      api<SettingsResponse>(
        `/api/v1/config-center?search=true&language=${language}`,
      ),
    staleTime: 60_000,
  });
  const allowed = categoryItems.filter(
    (item) =>
      item.id === "personal" ||
      item.id === "about" ||
      user.role === "super_admin",
  );
  const current = allowed.find((item) => item.id === category);
  const matches = useMemo(() => {
    const terms = search
      .trim()
      .toLocaleLowerCase()
      .split(/\s+/)
      .filter(Boolean);
    return terms.length
      ? (index.data?.fields ?? []).filter((field) =>
          terms.every((term) =>
            (
              field.searchText ??
              `${field.label} ${field.description} ${field.id}`
            )
              .toLocaleLowerCase()
              .includes(term),
          ),
        )
      : [];
  }, [search, index.data]);
  return (
    <div className="settings-page">
      <header className="settings-page-heading">
        <h1>{t("设置", "Settings")}</h1>
        <div className="settings-search">
          <Search size={19} aria-hidden="true" />
          <input
            aria-label={t("搜索所有设置", "Search all settings")}
            placeholder={t(
              "搜索设置、功能或关键词…",
              "Search settings, features or keywords…",
            )}
            value={search}
            onChange={(event) =>
              setSearchParams(
                event.target.value ? { q: event.target.value } : {},
                { replace: true },
              )
            }
          />
          {search && (
            <button
              className="icon-button"
              aria-label={t("清除搜索", "Clear search")}
              onClick={() => setSearchParams({}, { replace: true })}
            >
              <X size={17} />
            </button>
          )}
        </div>
      </header>
      <div className="settings-layout">
        <nav
          className="settings-navigation"
          aria-label={t("设置分类", "Settings categories")}
        >
          {allowed.map(({ id, zh, en, icon: Icon }) => (
            <Link
              key={id}
              to={`/settings/${id}`}
              aria-current={category === id && !search ? "page" : undefined}
            >
              <Icon size={18} />
              <span>{t(zh, en)}</span>
            </Link>
          ))}
        </nav>
        <div className="settings-main">
          {search.trim() ? (
            <section className="settings-search-results">
              <h2>
                {t("搜索结果", "Search results")} <span>{matches.length}</span>
              </h2>
              {index.isPending && (
                <p role="status">{t("正在搜索…", "Searching…")}</p>
              )}
              {index.isError && (
                <div className="settings-alert" role="alert">
                  {t("无法加载设置索引。", "Could not load settings.")}
                  <button
                    className="button button-secondary"
                    onClick={() => void index.refetch()}
                  >
                    {t("重试", "Retry")}
                  </button>
                </div>
              )}
              {!index.isPending && !index.isError && matches.length === 0 && (
                <div className="settings-empty">
                  <Search size={30} />
                  <h3>{t("没有找到相关设置", "No matching settings")}</h3>
                  <p>
                    {t(
                      "尝试使用功能名称，或更短的关键词。",
                      "Try a feature name or a shorter keyword.",
                    )}
                  </p>
                </div>
              )}
              {matches.slice(0, 80).map((field) => (
                <Link
                  className="settings-search-result"
                  key={field.id}
                  to={`/settings/${field.category}#${encodeURIComponent(field.id)}`}
                >
                  <div>
                    <span className="settings-hint">
                      {
                        categoryItems.find(
                          (item) => item.id === field.category,
                        )?.[language === "en" ? "en" : "zh"]
                      }
                    </span>
                    <strong>{field.label}</strong>
                    <p>{field.description}</p>
                  </div>
                  <ArrowRight size={18} />
                </Link>
              ))}
              {matches.length > 80 && (
                <p className="settings-hint">
                  {t(
                    "显示前 80 项，请缩小搜索范围。",
                    "Showing the first 80 results. Refine your search for more.",
                  )}
                </p>
              )}
            </section>
          ) : current?.id === "about" ? (
            <Suspense fallback={<LoadingState />}>
              <AboutPanel />
            </Suspense>
          ) : current ? (
            <CategoryPanel
              key={category}
              category={current.id}
              title={t(current.zh, current.en)}
            />
          ) : (
            <div className="settings-empty">
              <ShieldCheck size={32} />
              <h2>{t("此设置不可用", "Settings unavailable")}</h2>
              <p>
                {t(
                  "该分类不存在，或你的账号没有访问权限。",
                  "This category does not exist or your account cannot access it.",
                )}
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function CategoryPanel({
  category,
  title,
}: {
  category: Category;
  title: string;
}) {
  const { language, t, setLanguage } = useLocale();
  const confirm = useConfirm();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const location = useLocation();
  const requestedTab = new URLSearchParams(location.search).get("tab");
  const personalTab =
    requestedTab === "security" || requestedTab === "notifications"
      ? requestedTab
      : "preferences";
  const showFields = category !== "personal" || personalTab === "preferences";
  const [store] = useState(() => new SettingsStore());
  useSyncExternalStore(store.subscribe, store.snapshot);
  const [group, setGroup] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const key = ["settings", category, language] as const;
  const read = useCallback(async () => {
    const response = await api<SettingsResponse>(
      `/api/v1/config-center?category=${category}&language=${language}`,
    );
    setCsrfToken(response.csrf);
    return response;
  }, [category, language]);
  const query = useQuery({
    queryKey: key,
    queryFn: read,
    refetchInterval: (query) =>
      query.state.data?.fields.some((field) => field.effect === "pending")
        ? Math.max(1000, query.state.data.syncIntervalMs)
        : false,
  });
  const accounts = useQuery({
    queryKey: ["ai-accounts"],
    queryFn: () =>
      api<{ data: { accounts: Account[] } }>("/api/v1/config/ai/accounts"),
    enabled: category === "ai",
  });
  useEffect(() => {
    if (query.data) store.reconcile(query.data.fields);
  }, [query.data, store]);
  useEffect(() => {
    if (typeof BroadcastChannel === "undefined") return;
    const channel = new BroadcastChannel("sakura-settings");
    channel.onmessage = () =>
      void queryClient.invalidateQueries({ queryKey: ["settings", category] });
    return () => channel.close();
  }, [category, queryClient]);
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      store.dirty &&
      (currentLocation.pathname !== nextLocation.pathname ||
        currentLocation.search !== nextLocation.search),
  );
  useEffect(() => {
    if (blocker.state !== "blocked") return;
    let active = true;
    void confirm({
      title: t("离开设置？", "Leave settings?"),
      description: t(
        "有未保存的修改。放弃修改并离开？",
        "You have unsaved changes. Discard them and leave?",
      ),
      destructive: true,
    }).then((accepted) => {
      if (!active) return;
      if (accepted) {
        store.discardAll();
        blocker.proceed();
      } else blocker.reset();
    });
    return () => {
      active = false;
    };
  }, [blocker, store, t, confirm]);
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (store.dirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    const externalNavigation = (event: MouseEvent) => {
      if (
        !store.dirty ||
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      )
        return;
      const link = (event.target as Element).closest<HTMLAnchorElement>(
        "a[href]",
      );
      if (
        !link ||
        link.target === "_blank" ||
        (link.origin !== window.location.origin &&
          !link.href.startsWith("http"))
      )
        return;
      if (link.pathname.startsWith("/app/")) return;
      event.preventDefault();
      event.stopPropagation();
      void confirm({
        title: t("离开设置？", "Leave settings?"),
        description: t(
          "有未保存的修改。放弃修改并离开？",
          "You have unsaved changes. Discard them and leave?",
        ),
        destructive: true,
      }).then((accepted) => {
        if (accepted) {
          store.discardAll();
          window.location.assign(link.href);
        }
      });
    };
    window.addEventListener("beforeunload", beforeUnload);
    document.addEventListener("click", externalNavigation, true);
    return () => {
      window.removeEventListener("beforeunload", beforeUnload);
      document.removeEventListener("click", externalNavigation, true);
    };
  }, [store, t, confirm]);
  const message = (code: string) => query.data?.messages[code] ?? code;
  const groups = [
    ...new Set(query.data?.fields.map((field) => field.group) ?? []),
  ];
  const hash = safeHash(location.hash);
  const targeted = query.data?.fields.find((field) => field.id === hash);
  const selectedGroup =
    targeted?.group ?? (groups.includes(group) ? group : groups[0]);
  const visible = (query.data?.fields ?? []).filter(
    (field) => field.group === selectedGroup,
  );
  const showAdvanced =
    advanced ||
    targeted?.advanced === true ||
    visible.every((field) => field.advanced);
  const targetId = targeted?.id;
  useEffect(() => {
    if (targetId)
      document
        .getElementById(`setting-${targetId}`)
        ?.scrollIntoView?.({ block: "center" });
  }, [targetId]);
  const refresh = async () => {
    const fresh = await read();
    queryClient.setQueryData(key, fresh);
    return fresh.fields;
  };
  const save = async (field: ConfigField, reset = false) => {
    if (
      (field.sensitive || reset) &&
      !(await confirm({
        title: reset
          ? t("恢复默认值？", "Restore default?")
          : t("更新敏感配置？", "Update sensitive setting?"),
        description: message(reset ? "confirm_reset" : "confirm_secret"),
        destructive: reset,
      }))
    )
      return;
    await store.save(
      field.id,
      async (current, value, resetting) => {
        const response = await api<{ field: ConfigField }>(
          `/api/v1/config-center${resetting ? "/reset" : ""}`,
          {
            method: resetting ? "POST" : "PATCH",
            body: JSON.stringify({
              field: current.id,
              version: current.version,
              ...(resetting ? {} : { value, confirmed: current.sensitive }),
            }),
          },
        );
        if (typeof BroadcastChannel !== "undefined") {
          const channel = new BroadcastChannel("sakura-settings");
          channel.postMessage({ changed: true });
          channel.close();
        }
        if (current.id.startsWith("personal/")) {
          void queryClient.invalidateQueries({ queryKey: ["bootstrap"] });
          if (current.id === "personal/theme")
            window.dispatchEvent(
              new CustomEvent("sakura-theme", { detail: response.field.value }),
            );
          if (
            current.id === "personal/language" &&
            (response.field.value === "en" || response.field.value === "zh-CN")
          )
            setLanguage(response.field.value);
        }
        return response.field;
      },
      refresh,
      reset,
    );
  };
  const reload = async (field: ConfigField) => {
    if (
      !(await confirm({
        title: t("重新加载设置？", "Reload setting?"),
        description: message("confirm_discard"),
        destructive: true,
      }))
    )
      return;
    try {
      const fresh = await refresh();
      const server = fresh.find((item) => item.id === field.id);
      if (server) store.accept(server);
    } catch {
      /* Query owns the visible retry error state. */
    }
  };
  return (
    <>
      <div className="settings-category-heading">
        <h2>{title}</h2>
      </div>
      {category === "personal" && (
        <div
          className="settings-personal-tabs"
          role="tablist"
          aria-label={t("个人设置", "Personal settings")}
        >
          {[
            { id: "preferences", zh: "界面偏好", en: "Appearance" },
            { id: "security", zh: "账号安全", en: "Security" },
            { id: "notifications", zh: "通知与绑定", en: "Notifications" },
          ].map((tab) => (
            <button
              role="tab"
              aria-selected={personalTab === tab.id}
              key={tab.id}
              onClick={() =>
                void navigate({
                  pathname: location.pathname,
                  search: tab.id === "preferences" ? "" : `?tab=${tab.id}`,
                })
              }
            >
              {t(tab.zh, tab.en)}
            </button>
          ))}
        </div>
      )}
      {category === "personal" && personalTab === "security" && (
        <SecurityPanel store={store} />
      )}
      {category === "personal" && personalTab === "notifications" && (
        <NotificationsPanel store={store} />
      )}
      {category === "ai" && <AccountsPanel store={store} />}
      {category === "backup" && <BackupPanel store={store} />}
      {showFields && query.isPending && (
        <div
          className="settings-skeleton"
          role="status"
          aria-label={t("正在加载设置", "Loading settings")}
        >
          {[1, 2, 3].map((item) => (
            <div key={item} />
          ))}
        </div>
      )}
      {showFields && query.isError && (
        <div className="settings-alert" role="alert">
          <Info size={19} />
          <div>
            <strong>{t("暂时无法加载设置", "Could not load settings")}</strong>
            <p>
              {t(
                "请检查连接后重试。你的修改不会被清除。",
                "Check your connection and retry. Your changes are kept.",
              )}
            </p>
          </div>
          <button
            className="button button-secondary"
            onClick={() => void query.refetch()}
          >
            {t("重试", "Retry")}
          </button>
        </div>
      )}
      {showFields && groups.length > 1 && (
        <div className="settings-group-picker">
          <label htmlFor="settings-group">
            <SlidersHorizontal size={16} />
            {t("设置分组", "Settings group")}
          </label>
          <select
            id="settings-group"
            value={selectedGroup}
            onChange={(event) => {
              setGroup(event.target.value);
              setAdvanced(false);
              if (location.hash)
                void navigate(
                  { pathname: location.pathname, hash: "" },
                  { replace: true },
                );
            }}
          >
            {groups.map((name) => (
              <option key={name} value={name}>
                {query.data?.groups[name] ?? message(name)}
              </option>
            ))}
          </select>
          <span>
            {visible.length} {t("项设置", "settings")}
          </span>
        </div>
      )}
      {showFields && !!visible.length && (
        <section
          className="settings-section"
          aria-label={query.data?.groups[selectedGroup] ?? title}
        >
          {visible
            .filter((field) => showAdvanced || !field.advanced)
            .map((field) => (
              <FieldRow
                key={field.id}
                field={field}
                store={store}
                message={message}
                accounts={accounts.data?.data.accounts ?? []}
                onSave={save}
                onReload={reload}
                targeted={field.id === hash}
              />
            ))}
          {!showAdvanced && visible.some((field) => field.advanced) && (
            <button
              className="settings-advanced"
              onClick={() => setAdvanced(true)}
            >
              <ChevronDown size={17} />
              {t("显示高级设置", "Show advanced settings")}
              <span>{visible.filter((field) => field.advanced).length}</span>
            </button>
          )}
        </section>
      )}
      {category === "ai" && <ModelSettings store={store} />}
      {category === "agent" && <PluginsPanel store={store} />}
      {category === "system" && (
        <SystemPanel
          store={store}
          onApplySlug={(field, value) => {
            store.edit(field.id, value);
            void navigate({
              pathname: location.pathname,
              hash: encodeURIComponent(field.id),
            });
          }}
        />
      )}
      {store.dirtyCount > 0 && (
        <div className="settings-dirty-bar" role="status">
          <span>
            <span className="settings-unsaved-dot" />
            {store.dirtyCount} {t("项未保存的修改", "unsaved changes")}
          </span>
          <button
            className="button button-secondary"
            disabled={[...store.entries.values()].some((entry) => entry.saving)}
            onClick={async () => {
              if (
                await confirm({
                  title: t("放弃修改？", "Discard changes?"),
                  description: message("confirm_discard"),
                  destructive: true,
                })
              )
                store.discardAll();
            }}
          >
            {t("放弃修改", "Discard changes")}
          </button>
        </div>
      )}
    </>
  );
}

function FieldRow({
  field,
  store,
  message,
  accounts,
  onSave,
  onReload,
  targeted = false,
}: {
  field: ConfigField;
  store: SettingsStore;
  message: (key: string) => string;
  accounts: Account[];
  onSave: (field: ConfigField, reset?: boolean) => Promise<void>;
  onReload: (field: ConfigField) => Promise<void>;
  targeted?: boolean;
}) {
  const { t } = useLocale();
  const entry = store.entries.get(field.id);
  if (!entry) return null;
  const current = entry.field;
  const dirty = isDirty(entry);
  const id = `input-${field.id}`;
  const disabled = !field.writable || entry.saving;
  const edit = (value: Json) => store.edit(field.id, value);
  const structured = field.type === "array" || field.type === "object";
  let control;
  if (field.id.startsWith("binding/"))
    control = (
      <BindingEditor
        field={field}
        value={entry.draft}
        onChange={edit}
        accounts={accounts}
        message={message}
        disabled={disabled}
      />
    );
  else if (structured)
    control = (
      <StructuredEditor
        value={entry.draft}
        schema={field.editorSchema ?? { type: field.type }}
        label={field.label}
        onChange={edit}
        message={message}
        disabled={disabled}
      />
    );
  else if (field.constraints.enum)
    control = (
      <select
        id={id}
        value={JSON.stringify(entry.draft)}
        disabled={disabled}
        onChange={(event) => edit(JSON.parse(event.target.value) as Json)}
      >
        {field.constraints.enum.map((option) => (
          <option key={JSON.stringify(option)} value={JSON.stringify(option)}>
            {field.optionLabels[String(option)] ?? String(option)}
          </option>
        ))}
      </select>
    );
  else if (field.type === "boolean")
    control = (
      <label className="settings-switch">
        <input
          id={id}
          type="checkbox"
          role="switch"
          checked={entry.draft === true}
          disabled={disabled}
          onChange={(event) => edit(event.target.checked)}
        />
        <span aria-hidden="true" />
      </label>
    );
  else if (
    /prompt|template/.test(field.path.at(-1) ?? "") ||
    (typeof entry.draft === "string" &&
      (entry.draft.includes("\n") || entry.draft.length > 160))
  )
    control = (
      <textarea
        id={id}
        rows={5}
        disabled={disabled}
        value={String(entry.draft ?? "")}
        onChange={(event) => edit(event.target.value)}
      />
    );
  else
    control = (
      <input
        id={id}
        disabled={disabled}
        type={
          field.sensitive
            ? "password"
            : ["number", "integer"].includes(field.type)
              ? "number"
              : "text"
        }
        value={
          typeof entry.draft === "string" || typeof entry.draft === "number"
            ? entry.draft
            : ""
        }
        placeholder={
          field.sensitive
            ? message(field.hasValue ? "credential" : "credential_empty")
            : undefined
        }
        autoComplete={field.sensitive ? "new-password" : undefined}
        step={field.type === "integer" ? 1 : "any"}
        min={field.constraints.ge}
        max={field.constraints.le}
        aria-invalid={!!entry.error}
        aria-describedby={`description-${field.id}`}
        onChange={(event) =>
          edit(
            ["number", "integer"].includes(field.type) &&
              event.target.value !== ""
              ? Number(event.target.value)
              : event.target.value,
          )
        }
      />
    );
  return (
    <div
      className={`settings-field ${structured ? "settings-field-wide" : ""} ${targeted ? "settings-field-targeted" : ""}`}
      id={`setting-${field.id}`}
    >
      <div className="settings-field-copy">
        <label htmlFor={id}>
          {field.label}
          {current.overridden && (
            <span
              className="settings-custom-dot"
              title={t("已自定义", "Customized")}
            />
          )}
        </label>
        {!field.writable && <p>{message("deployment_owned")}</p>}
        <details className="settings-field-details">
          <summary>{t("详情", "Details")}</summary>
          <p id={`description-${field.id}`}>{field.description}</p>
          <code>{field.id}</code>
          <dl>
            <dt>{t("来源", "Source")}</dt>
            <dd>{message(current.source)}</dd>
            {!field.sensitive && (
              <>
                <dt>{t("默认值", "Default")}</dt>
                <dd>
                  <pre>{JSON.stringify(current.default, null, 2)}</pre>
                </dd>
              </>
            )}
          </dl>
          {current.resettable && (
            <button
              className="button button-secondary"
              disabled={disabled || entry.conflict}
              onClick={() => void onSave(current, true)}
            >
              <RotateCcw size={14} />
              {message("reset")}
            </button>
          )}
        </details>
      </div>
      <div className="settings-field-control">
        {control}
        <div className="settings-field-feedback" aria-live="polite">
          {entry.error && (
            <p className="settings-error" role="alert">
              {message(entry.error)}
            </p>
          )}
          {!entry.error &&
            entry.state !== "unchanged" &&
            entry.state !== "unsaved" && (
              <span
                className={`settings-status settings-status-${entry.state}`}
              >
                {entry.saving ? (
                  <LoaderCircle size={14} className="spin" />
                ) : entry.state === "saved" ? (
                  <Check size={14} />
                ) : (
                  <Info size={14} />
                )}
                {message(entry.state)}
              </span>
            )}
          {entry.conflict ? (
            <button
              className="button button-secondary"
              onClick={() => void onReload(current)}
            >
              {message("reload_field")}
            </button>
          ) : (
            dirty && (
              <div className="settings-field-actions">
                <button
                  className="button button-secondary"
                  disabled={entry.saving}
                  onClick={() => store.accept(current)}
                >
                  {t("取消", "Cancel")}
                </button>
                <button
                  className="button primary"
                  disabled={entry.saving}
                  onClick={() => void onSave(current)}
                >
                  {entry.saving
                    ? t("保存中…", "Saving…")
                    : field.sensitive
                      ? t("确认更新", "Update securely")
                      : t("保存", "Save")}
                </button>
              </div>
            )
          )}
        </div>
      </div>
    </div>
  );
}

function ModelSettings({ store }: { store: SettingsStore }) {
  const { language, t } = useLocale();
  const confirm = useConfirm();
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [selection, setSelection] = useState<{
    provider: string;
    model: string;
  } | null>(null);
  const catalog = useQuery({
    queryKey: ["ai-catalog"],
    queryFn: () =>
      api<{ data: { providers: { id: string; label: string }[] } }>(
        "/api/v1/config/ai/catalog",
      ),
  });
  const read = async () => {
    const data = await api<SettingsResponse>(
      `/api/v1/config-center?category=ai&language=${language}&provider=${encodeURIComponent(selection?.provider ?? "")}&model=${encodeURIComponent(selection?.model ?? "")}`,
    );
    setCsrfToken(data.csrf);
    return data;
  };
  const query = useQuery({
    queryKey: ["settings-model", selection, language],
    queryFn: read,
    enabled: !!selection,
    refetchInterval: (query) =>
      query.state.data?.fields.some((field) => field.effect === "pending")
        ? Math.max(1000, query.state.data.syncIntervalMs)
        : false,
  });
  useEffect(() => {
    if (query.data) store.reconcile(query.data.fields);
  }, [query.data, selection, store]);
  const message = (code: string) => query.data?.messages[code] ?? code;
  const save = async (field: ConfigField, reset = false) => {
    if (
      reset &&
      !(await confirm({
        title: t("恢复默认值？", "Restore default?"),
        description: message("confirm_reset"),
        destructive: true,
      }))
    )
      return;
    await store.save(
      field.id,
      async (current, value, resetting) => {
        const result = await api<{ field: ConfigField }>(
          `/api/v1/config-center${resetting ? "/reset" : ""}`,
          {
            method: resetting ? "POST" : "PATCH",
            body: JSON.stringify({
              field: current.id,
              version: current.version,
              ...(resetting ? {} : { value }),
            }),
          },
        );
        return result.field;
      },
      async () => {
        const fresh = await query.refetch();
        return fresh.data?.fields ?? [];
      },
      reset,
    );
  };
  return (
    <details className="settings-model-panel">
      <summary>
        <SlidersHorizontal size={17} />
        <span>{t("单模型能力覆盖", "Per-model capabilities")}</span>
        <ChevronDown size={16} />
      </summary>
      <p>
        {t(
          "按供应商与模型调整能力和上下文限制。使用恢复默认值以继承模型元数据。",
          "Adjust capabilities and context limits for a specific model. Restore defaults to inherit model metadata.",
        )}
      </p>
      <form
        className="settings-model-picker"
        onSubmit={async (event) => {
          event.preventDefault();
          const modelEntries = [...store.entries.values()].filter((entry) =>
            entry.field.id.startsWith("model/"),
          );
          if (modelEntries.some((entry) => entry.saving)) return;
          if (
            modelEntries.some(isDirty) &&
            !(await confirm({
              title: t("切换模型？", "Switch model?"),
              description: t(
                "放弃当前模型的未保存修改？",
                "Discard unsaved changes for this model?",
              ),
              destructive: true,
            }))
          )
            return;
          store.forget("model/");
          setSelection({
            provider: provider || catalog.data?.data.providers[0]?.id || "",
            model: model.trim(),
          });
        }}
      >
        <label>
          {t("供应商", "Provider")}
          <select
            value={provider || catalog.data?.data.providers[0]?.id || ""}
            onChange={(event) => setProvider(event.target.value)}
          >
            {catalog.data?.data.providers.map((item) => (
              <option value={item.id} key={item.id}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("模型 ID", "Model ID")}
          <input
            value={model}
            required
            onChange={(event) => setModel(event.target.value)}
            placeholder={t("输入模型标识符", "Enter model identifier")}
          />
        </label>
        <button className="button button-secondary" disabled={!model.trim()}>
          {t("加载", "Load")}
        </button>
      </form>
      {query.isFetching && <p role="status">{t("正在加载…", "Loading…")}</p>}
      {query.isError && (
        <p className="settings-error" role="alert">
          {t(
            "无法加载该模型的设置，请检查模型 ID。",
            "Could not load these model settings. Check the model ID.",
          )}
        </p>
      )}
      {query.data?.fields.map((field) => (
        <FieldRow
          key={field.id}
          field={field}
          store={store}
          message={message}
          accounts={[]}
          onSave={save}
          onReload={async (field) => {
            if (
              await confirm({
                title: t("重新加载设置？", "Reload setting?"),
                description: message("confirm_discard"),
                destructive: true,
              })
            ) {
              const fresh = await query.refetch();
              const current = fresh.data?.fields.find(
                (item) => item.id === field.id,
              );
              if (current) store.accept(current);
            }
          }}
        />
      ))}
    </details>
  );
}
