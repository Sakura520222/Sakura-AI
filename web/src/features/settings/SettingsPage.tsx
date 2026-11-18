import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import {
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";
import {
  Link,
  Navigate,
  useBlocker,
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Check,
  CheckCircle2,
  Clock,
  Info,
  ListTree,
  LoaderCircle,
  Power,
  RotateCcw,
  Search,
  ShieldCheck,
  SlidersHorizontal,
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
import { ChangesDialog } from "./ChangesDialog";
import {
  CATEGORY_ITEMS,
  LEGACY_CATEGORY_ALIASES,
  lookupSectionTitle,
  resolveCategorySections,
  sectionAnchorId,
  WORKFLOW_SEARCH_ITEMS,
  type ResolvedSection,
} from "./sectionMeta";
import { isDirty, SettingsStore, validateValue } from "./state";
import type {
  Account,
  Category,
  ConfigField,
  Json,
  SettingsResponse,
} from "./types";
import "./settings.css";

const AboutPanel = lazy(() => import("./AboutPanel"));

function safeHash(hash: string) {
  try {
    return decodeURIComponent(hash.slice(1));
  } catch {
    return "";
  }
}

type ModelSelection = { account: string; provider: string; model: string };
type AccountsQuery = UseQueryResult<{ data: { accounts: Account[] } }>;

function modelsFor(account: Account) {
  return [
    ...new Set(
      [account.default_model, ...account.models].filter((model) =>
        model?.trim(),
      ),
    ),
  ];
}

function locateSettingField(id: string) {
  const row = document.getElementById(`setting-${id}`);
  if (!row) return;
  row.scrollIntoView?.({ block: "center" });
  row.focus({ preventScroll: true });
}

interface SearchHit {
  kind: "field" | "workflow";
  id: string;
  to: string;
  categoryLabel: string;
  sectionLabel: string;
  title: string;
  description: string;
  matchBadge: string;
  requiresRestart?: boolean;
  readOnly?: boolean;
}

function inferFieldUnit(
  field: ConfigField,
  t: (zh: string, en: string) => string,
): string | null {
  const leaf = field.path.at(-1) ?? field.key;
  if (
    leaf.endsWith("_seconds") ||
    leaf.endsWith("_sec") ||
    leaf.endsWith("_timeout") ||
    field.key === "agent_team_candidate_cache_ttl"
  )
    return t("秒", "seconds");
  if (leaf.endsWith("_minutes")) return t("分钟", "minutes");
  if (leaf.endsWith("_hours")) return t("小时", "hours");
  if (leaf.endsWith("_days")) return t("天", "days");
  if (
    leaf.endsWith("_bytes") ||
    leaf === "max_file_size" ||
    leaf.endsWith("_download_size")
  )
    return t("字节", "bytes");
  if (
    leaf.endsWith("_chars") ||
    leaf.endsWith("_content_length") ||
    field.key === "code_chunk_size" ||
    field.key === "code_chunk_overlap"
  )
    return t("字符", "chars");
  if (leaf.endsWith("_tokens")) return "Token";
  return null;
}

function describeFieldConstraints(
  field: ConfigField,
  t: (zh: string, en: string, values?: readonly unknown[]) => string,
): string[] {
  const facts: string[] = [];
  const unit = inferFieldUnit(field, t);
  if (unit) facts.push(t("单位：{0}", "Unit: {0}", [unit]));

  const { ge, gt, le, lt, min_length, max_length } = field.constraints;
  if (ge != null && le != null)
    facts.push(t("范围：{0} – {1}", "Range: {0}–{1}", [ge, le]));
  else if (ge != null) facts.push(t("最小：{0}", "Min: {0}", [ge]));
  else if (gt != null) facts.push(t("大于 {0}", "> {0}", [gt]));
  else if (le != null) facts.push(t("最大：{0}", "Max: {0}", [le]));
  else if (lt != null) facts.push(t("小于 {0}", "< {0}", [lt]));

  if (min_length != null && max_length != null)
    facts.push(
      t("长度：{0} – {1}", "Length: {0}–{1}", [min_length, max_length]),
    );

  if (field.key === "ai_fallback_max_candidates")
    facts.push(t("0 = 完整回退链", "0 = full chain"));
  else if (field.key === "code_chunk_overlap")
    facts.push(t("需 ≤ 代码块大小", "Must be ≤ chunk size"));
  else if (field.key === "star_aid_min_interval_minutes")
    facts.push(t("需 ≤ 最长互助间隔", "Must be ≤ max interval"));
  else if (field.key === "pr_issue_related_confidence_threshold")
    facts.push(t("需 ≤ 关闭关系置信度阈值", "Must be ≤ closing confidence"));
  else if (
    [
      "stripe_enabled",
      "paddle_enabled",
      "alipay_enabled",
      "nowpayments_enabled",
      "tron_enabled",
    ].includes(field.key)
  )
    facts.push(t("开启前需配置渠道密钥", "Requires channel credentials"));

  if (
    !field.sensitive &&
    (typeof field.default === "number" ||
      typeof field.default === "boolean" ||
      (typeof field.default === "string" &&
        field.default.length > 0 &&
        field.default.length <= 24 &&
        !field.default.includes("\n")))
  ) {
    const defaultText =
      typeof field.default === "boolean"
        ? field.default
          ? t("开启", "On")
          : t("关闭", "Off")
        : (field.optionLabels[String(field.default)] ?? String(field.default));
    facts.push(t("默认：{0}", "Default: {0}", [defaultText]));
  }

  return facts;
}

export default function SettingsPage() {
  const { category: rawCategory = "personal" } = useParams();
  const { language, t, setLanguage } = useLocale();
  const { user } = useAuth();
  const confirm = useConfirm();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const [searchParams] = useSearchParams();
  const searchInputRef = useRef<HTMLInputElement>(null);
  const navigationRef = useRef<HTMLElement>(null);
  const mobileTocRef = useRef<HTMLDetailsElement>(null);
  const actionBarRef = useRef<HTMLDivElement>(null);
  const confirmingBatch = useRef(false);
  const settingsMounted = useRef(false);

  const aliasedCategory = LEGACY_CATEGORY_ALIASES[rawCategory];
  const category = aliasedCategory ?? rawCategory;

  // Identity-isolated in-memory store across all Settings categories and searches.
  const identityKey = `${user.id ?? ""}:${user.username ?? ""}:${user.role ?? ""}`;
  const storeRef = useRef<{ identity: string; store: SettingsStore } | null>(
    null,
  );
  if (!storeRef.current || storeRef.current.identity !== identityKey) {
    storeRef.current?.store.clearAll();
    storeRef.current = { identity: identityKey, store: new SettingsStore() };
  }
  const store = storeRef.current.store;
  useSyncExternalStore(store.subscribe, store.snapshot);

  useEffect(() => {
    settingsMounted.current = true;
    const clear = () => store.clearAll();
    window.addEventListener("sakura:session-expired", clear);
    return () => {
      settingsMounted.current = false;
      window.removeEventListener("sakura:session-expired", clear);
      store.clearAll();
    };
  }, [store]);

  const [changesOpen, setChangesOpen] = useState(false);
  const [sections, setSections] = useState<ResolvedSection[]>([]);
  const [activeSectionId, setActiveSectionId] = useState<string>("");
  const [activeSearchIdx, setActiveSearchIdx] = useState(0);
  const [recentSearch, setRecentSearch] = useState<string>("");
  const [modelSelection, setModelSelection] = useState<ModelSelection | null>(
    null,
  );
  const accounts = useQuery({
    queryKey: ["ai-accounts"],
    queryFn: ({ signal }) =>
      api<{ data: { accounts: Account[] } }>("/api/v1/config/ai/accounts", {
        signal,
      }),
    enabled:
      user.role === "super_admin" &&
      (category === "ai" || modelSelection !== null),
  });
  const selectedAccount = accounts.data?.data.accounts.find(
    (account) => account.id === modelSelection?.account,
  );
  const selectionAvailable =
    !!modelSelection &&
    !!selectedAccount?.enabled &&
    selectedAccount.provider_id === modelSelection.provider &&
    modelsFor(selectedAccount).includes(modelSelection.model);
  useEffect(() => {
    store.setPrefixUnavailable(
      "model/",
      !!modelSelection && !selectionAvailable,
    );
  }, [modelSelection, selectionAvailable, store]);

  const search = searchParams.get("q") ?? "";
  useEffect(() => {
    if (search.trim()) setRecentSearch(search.trim());
    setActiveSearchIdx(0);
  }, [search]);

  const fromSearchState =
    (location.state as { fromSearch?: string } | null)?.fromSearch ?? "";
  const returnSearchQuery = !search.trim() ? fromSearchState || "" : "";
  useEffect(() => {
    const navigation = navigationRef.current;
    if (!navigation) return;
    const revealCategory = () => {
      const active = navigation.querySelector('a[aria-current="page"]');
      if (!active || navigation.scrollWidth <= navigation.clientWidth) return;
      const viewport = navigation.getBoundingClientRect();
      const item = active.getBoundingClientRect();
      if (item.right > viewport.right)
        navigation.scrollLeft += item.right - viewport.right;
      else if (item.left < viewport.left)
        navigation.scrollLeft += item.left - viewport.left;
    };
    revealCategory();
    const observer =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(revealCategory);
    observer?.observe(navigation);
    return () => observer?.disconnect();
  }, [category, search, language]);
  const changeSearch = (query: string, replace = false) => {
    const params = new URLSearchParams(location.search);
    if (query) params.set("q", query);
    else params.delete("q");
    void navigate(
      {
        pathname: location.pathname,
        search: params.toString(),
        hash: location.hash,
      },
      { replace, state: location.state },
    );
  };

  // Guard ONLY when leaving /settings/* (preserve drafts freely within settings).
  const blocker = useBlocker(
    ({ nextLocation }) =>
      store.dirty && !nextLocation.pathname.startsWith("/settings"),
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
      if (
        link.getAttribute("href")?.startsWith("#") ||
        link.hasAttribute("data-discover") ||
        link.pathname.startsWith("/app/") ||
        link.pathname.startsWith("/settings/")
      )
        return;
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

  const index = useQuery({
    queryKey: ["settings-index", identityKey, language],
    queryFn: async () => {
      const data = await api<SettingsResponse>(
        `/api/v1/config-center?search=true&language=${language}`,
      );
      store.recordCatalogMetadata(data.groups, data.messages);
      return data;
    },
    staleTime: 60_000,
  });

  const allowed = useMemo(
    () =>
      CATEGORY_ITEMS.filter(
        (item) =>
          item.id === "personal" ||
          item.id === "about" ||
          user.role === "super_admin",
      ),
    [user.role],
  );
  const current = allowed.find((item) => item.id === category);

  // Per-category unsaved badges
  const dirtyByCategory = useMemo(() => {
    const counts = new Map<string, number>();
    for (const entry of store.dirtyEntries) {
      counts.set(
        entry.field.category,
        (counts.get(entry.field.category) ?? 0) + 1,
      );
    }
    return counts;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [store, store.snapshot()]);

  const dirtyByGroup = useMemo(() => {
    const counts = new Map<string, number>();
    for (const entry of store.dirtyEntries) {
      if (entry.field.category !== category) continue;
      const sectionKey = entry.field.id.startsWith("model/")
        ? "model-capabilities"
        : entry.field.group;
      counts.set(sectionKey, (counts.get(sectionKey) ?? 0) + 1);
    }
    return counts;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [store, category, store.snapshot()]);

  const searchHits = useMemo<SearchHit[]>(() => {
    const terms = search
      .trim()
      .toLocaleLowerCase()
      .split(/\s+/)
      .filter(Boolean);
    if (!terms.length) return [];
    const groupsMap = index.data?.groups ?? {};
    const hits: SearchHit[] = [];

    for (const workflow of WORKFLOW_SEARCH_ITEMS) {
      if (workflow.adminOnly && user.role !== "super_admin") continue;
      const title = t(workflow.title[0], workflow.title[1]);
      const description = t(workflow.description[0], workflow.description[1]);
      const sectionLabel = t(workflow.section[0], workflow.section[1]);
      const catMeta = CATEGORY_ITEMS.find(
        (item) => item.id === workflow.category,
      );
      const categoryLabel = catMeta
        ? t(catMeta.label[0], catMeta.label[1])
        : workflow.category;
      const haystack =
        `${title} ${description} ${sectionLabel} ${workflow.title[0]} ${workflow.title[1]} ${workflow.keywords}`.toLocaleLowerCase();
      if (terms.every((term) => haystack.includes(term))) {
        hits.push({
          kind: "workflow",
          id: workflow.id,
          to: workflow.to,
          categoryLabel,
          sectionLabel,
          title,
          description,
          matchBadge: t("功能面板", "Feature panel"),
        });
      }
    }

    for (const field of index.data?.fields ?? []) {
      const catMeta = CATEGORY_ITEMS.find((item) => item.id === field.category);
      const categoryLabel = catMeta
        ? t(catMeta.label[0], catMeta.label[1])
        : field.category;
      const sectionLabel = lookupSectionTitle(
        field.category,
        field.group,
        groupsMap,
        t,
      );
      const fullText = (
        field.searchText ??
        `${field.id} ${field.key} ${sectionLabel} ${field.label} ${field.description}`
      ).toLocaleLowerCase();
      if (!terms.every((term) => fullText.includes(term))) continue;

      const labelLow = field.label.toLocaleLowerCase();
      const keyLow = `${field.key} ${field.id}`.toLocaleLowerCase();
      const secLow = sectionLabel.toLocaleLowerCase();
      const descLow = field.description.toLocaleLowerCase();

      let matchBadge = t("命中别名 / 双语", "Alias / bilingual match");
      if (terms.some((term) => keyLow.includes(term)))
        matchBadge = `${t("配置键", "Key")}: ${field.path.length ? `${field.key}/${field.path.join("/")}` : field.key}`;
      else if (terms.every((term) => labelLow.includes(term)))
        matchBadge = t("名称匹配", "Name match");
      else if (terms.some((term) => secLow.includes(term)))
        matchBadge = t("分区匹配", "Section match");
      else if (terms.some((term) => descLow.includes(term)))
        matchBadge = t("用途说明匹配", "Description match");

      hits.push({
        kind: "field",
        id: field.id,
        to: `/settings/${field.category}#${encodeURIComponent(field.id)}`,
        categoryLabel,
        sectionLabel,
        title: field.label,
        description: field.description,
        matchBadge,
        requiresRestart: field.requiresRestart,
        readOnly: !field.writable,
      });
    }
    return hits;
  }, [search, index.data, user.role, t]);

  // Shared mutation & refresh callbacks supporting cross-category single & batch saves.
  const applyPersonalSideEffects = useCallback(
    (fieldId: string, newValue: Json) => {
      if (typeof BroadcastChannel !== "undefined") {
        const channel = new BroadcastChannel("sakura-settings");
        channel.postMessage({ changed: true });
        channel.close();
      }
      if (fieldId.startsWith("personal/")) {
        void queryClient.invalidateQueries({ queryKey: ["bootstrap"] });
        if (fieldId === "personal/theme")
          window.dispatchEvent(
            new CustomEvent("sakura-theme", { detail: newValue }),
          );
        if (
          fieldId === "personal/language" &&
          (newValue === "en" || newValue === "zh-CN")
        )
          setLanguage(newValue);
      }
    },
    [queryClient, setLanguage],
  );

  const refreshScope = useCallback(
    async (scope: string): Promise<ConfigField[]> => {
      if (scope.startsWith("model:")) {
        const [, provider, ...modelParts] = scope.split(":");
        const model = modelParts.join(":");
        const key = ["settings-model", provider, model];
        await queryClient.cancelQueries({ queryKey: key });
        const fresh = await api<SettingsResponse>(
          `/api/v1/config-center?category=ai&language=${language}&provider=${encodeURIComponent(provider)}&model=${encodeURIComponent(model)}`,
        );
        if (!settingsMounted.current) return [];
        setCsrfToken(fresh.csrf);
        store.recordCatalogMetadata(fresh.groups, fresh.messages);
        queryClient.setQueryData([...key, language], fresh);
        return fresh.fields;
      }
      const cat = scope.replace(/^category:/, "") as Category;
      const key = ["settings", cat];
      await queryClient.cancelQueries({ queryKey: key });
      const fresh = await api<SettingsResponse>(
        `/api/v1/config-center?category=${cat}&language=${language}`,
      );
      if (!settingsMounted.current) return [];
      setCsrfToken(fresh.csrf);
      store.recordCatalogMetadata(fresh.groups, fresh.messages);
      queryClient.setQueryData([...key, language], fresh);
      return fresh.fields;
    },
    [language, queryClient, store],
  );

  const mutateFieldApi = useCallback(
    async (currentField: ConfigField, value: Json, resetting = false) => {
      const response = await api<{ field: ConfigField }>(
        `/api/v1/config-center${resetting ? "/reset" : ""}`,
        {
          method: resetting ? "POST" : "PATCH",
          body: JSON.stringify({
            field: currentField.id,
            version: currentField.version,
            ...(resetting
              ? {}
              : { value, confirmed: currentField.sensitive || undefined }),
          }),
        },
      );
      if (!settingsMounted.current) return response.field;
      applyPersonalSideEffects(currentField.id, response.field.value);
      return response.field;
    },
    [applyPersonalSideEffects],
  );

  const reloadConflictField = useCallback(
    async (targetField: ConfigField) => {
      if (
        !(await confirm({
          title: t("重新加载设置？", "Reload setting?"),
          description:
            store.message("confirm_discard") !== "confirm_discard"
              ? store.message("confirm_discard")
              : t(
                  "放弃本地草稿并读取当前服务端值？",
                  "Discard your draft and load the current server value?",
                ),
          destructive: true,
        }))
      )
        return;
      try {
        const scope = targetField.id.startsWith("model/")
          ? (() => {
              const parts = targetField.id.split("/");
              return `model:${parts[1]}:${decodeURIComponent(parts[2] ?? "")}`;
            })()
          : `category:${targetField.category}`;
        const fresh = await refreshScope(scope);
        const server = fresh.find((item) => item.id === targetField.id);
        if (server) store.accept(server);
      } catch {
        /* Active query displays connection retry state. */
      }
    },
    [confirm, refreshScope, store, t],
  );

  const executeBatchSave = useCallback(
    async (onlyRetryable = false) => {
      if (store.savingAny || confirmingBatch.current) return;
      const snapshots = store.captureBatch(
        onlyRetryable ? store.retryableIds : undefined,
      );
      if (!snapshots.length) return;

      const validSensitive = snapshots.filter(
        (entry) =>
          entry.field.sensitive &&
          entry.field.writable &&
          !store.entries.get(entry.id)?.conflict &&
          !validateValue(entry.field, entry.draft),
      );
      if (validSensitive.length > 0) {
        const names = validSensitive.map((item) => item.field.label).join("、");
        confirmingBatch.current = true;
        try {
          if (
            !(await confirm({
              title: t("确认更新敏感配置？", "Confirm sensitive updates?"),
              description: t(
                "本次保存将更新 {0} 项敏感凭据（{1}），可能影响正在运行的服务。继续提交？",
                "This save will replace {0} sensitive credential(s) ({1}) and may affect running services. Continue?",
                [validSensitive.length, names],
              ),
              confirmLabel: t("确认并保存", "Confirm and save"),
            }))
          )
            return;
        } finally {
          confirmingBatch.current = false;
        }
      }

      if (!settingsMounted.current) return;
      const report = await store.saveBatch({
        snapshots,
        mutateField: (field, value) => mutateFieldApi(field, value, false),
        refreshScope,
      });
      if (
        report &&
        (report.counts.failed > 0 ||
          report.counts.conflict > 0 ||
          report.counts.skipped > 0)
      ) {
        setChangesOpen(true);
      }
    },
    [confirm, mutateFieldApi, refreshScope, store, t],
  );

  const jumpToSection = useCallback(
    (anchorId: string) => {
      if (mobileTocRef.current) mobileTocRef.current.open = false;
      void navigate(
        {
          pathname: location.pathname,
          search: location.search,
          hash: anchorId,
        },
        { state: location.state },
      );
      requestAnimationFrame(() => {
        const element = document.getElementById(anchorId);
        if (!element) return;
        element.scrollIntoView?.({ block: "start" });
        const heading = element.querySelector<HTMLElement>("h3, summary");
        heading?.focus({ preventScroll: true });
      });
    },
    [location.pathname, location.search, location.state, navigate],
  );

  const locateField = useCallback(
    (target: { id: string; category: Category }) => {
      const destination = `/settings/${target.category}#${encodeURIComponent(target.id)}`;
      void navigate(destination);
      requestAnimationFrame(() => {
        const element = document.getElementById(`setting-${target.id}`);
        element?.scrollIntoView?.({ block: "center" });
      });
    },
    [navigate],
  );

  const visibleSearchHits = searchHits.slice(0, 80);
  const showToc =
    !search.trim() &&
    !!current &&
    current.id !== "about" &&
    sections.length > 1;
  const activeSectionTitle =
    sections.find((sec) => sec.id === activeSectionId)?.title ??
    sections[0]?.title ??
    "";

  const report = store.lastBatchReport;
  const showActionBar = store.dirtyCount > 0 || !!report;
  useEffect(() => {
    const bar = actionBarRef.current;
    if (!showActionBar || !bar) return;
    const revealFocusedField = () => {
      const active = document.activeElement;
      if (
        !(active instanceof HTMLElement) ||
        !active.closest(".settings-field")
      )
        return;
      const field = active.getBoundingClientRect();
      const toolbar = bar.getBoundingClientRect();
      if (field.bottom > toolbar.top && field.top < toolbar.bottom)
        window.scrollBy({
          top: field.bottom - toolbar.top + 12,
          behavior: "instant",
        });
    };
    const frame = requestAnimationFrame(revealFocusedField);
    const observer =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(revealFocusedField);
    observer?.observe(bar);
    document.addEventListener("focusin", revealFocusedField);
    return () => {
      cancelAnimationFrame(frame);
      observer?.disconnect();
      document.removeEventListener("focusin", revealFocusedField);
    };
  }, [showActionBar]);

  if (aliasedCategory) {
    return (
      <Navigate
        to={{
          pathname: `/settings/${aliasedCategory}`,
          search: location.search,
          hash: location.hash,
        }}
        replace
      />
    );
  }

  return (
    <div className={`settings-page${showActionBar ? " has-action-bar" : ""}`}>
      <header className="settings-page-heading">
        <div>
          <h1>{t("设置", "Settings")}</h1>
        </div>
        <div className="settings-search">
          <Search size={18} aria-hidden="true" />
          <input
            ref={searchInputRef}
            aria-label={t("搜索所有设置", "Search all settings")}
            aria-controls={
              search.trim() ? "settings-search-results" : undefined
            }
            placeholder={t(
              "搜索设置名称、用途说明、配置键或中英文关键词…",
              "Search setting names, descriptions, keys or keywords…",
            )}
            value={search}
            onChange={(event) => {
              const nextValue = event.target.value;
              changeSearch(nextValue, Boolean(search));
            }}
            onKeyDown={(event) => {
              if (!search.trim()) return;
              if (event.key === "Escape") {
                event.preventDefault();
                changeSearch("", true);
                return;
              }
              if (!visibleSearchHits.length) return;
              if (["ArrowDown", "ArrowUp"].includes(event.key)) {
                event.preventDefault();
                const offset = event.key === "ArrowDown" ? 1 : -1;
                const next = Math.min(
                  visibleSearchHits.length - 1,
                  Math.max(0, activeSearchIdx + offset),
                );
                setActiveSearchIdx(next);
                document
                  .getElementById(`settings-search-hit-${next}`)
                  ?.scrollIntoView?.({ block: "nearest" });
              } else if (event.key === "Enter") {
                const chosen = visibleSearchHits[activeSearchIdx];
                if (chosen) {
                  event.preventDefault();
                  void navigate(chosen.to, {
                    state: { fromSearch: search.trim() },
                  });
                }
              }
            }}
          />
          {search ? (
            <button
              type="button"
              className="icon-button"
              aria-label={t("清除搜索", "Clear search")}
              onClick={() => {
                changeSearch("", true);
                searchInputRef.current?.focus();
              }}
            >
              <X size={16} />
            </button>
          ) : (
            recentSearch &&
            !returnSearchQuery && (
              <button
                type="button"
                className="settings-search-resume"
                onClick={() => changeSearch(recentSearch)}
              >
                {t("上次：{0}", "Recent: {0}", [
                  recentSearch.length > 14
                    ? `${recentSearch.slice(0, 14)}…`
                    : recentSearch,
                ])}
              </button>
            )
          )}
        </div>
      </header>

      <div className="settings-layout">
        <nav
          className="settings-navigation settings-sidebar"
          ref={navigationRef}
          aria-label={t("设置分类", "Settings categories")}
        >
          {allowed.map(({ id, label, icon: Icon }) => {
            const isActive = category === id && !search.trim();
            const catDirty = dirtyByCategory.get(id) ?? 0;
            return (
              <div key={id} className="settings-nav-group">
                <Link
                  to={`/settings/${id}`}
                  aria-current={isActive ? "page" : undefined}
                >
                  <Icon size={17} />
                  <span>{t(label[0], label[1])}</span>
                  {catDirty > 0 && (
                    <span
                      className="settings-nav-dirty-count"
                      title={t("{0} 项未保存", "{0} unsaved", [catDirty])}
                    >
                      {catDirty}
                    </span>
                  )}
                </Link>
                {isActive && showToc && (
                  <ul
                    className="settings-toc-desktop"
                    aria-label={t("本页目录", "On this page")}
                  >
                    {sections.map((sec) => {
                      const secDirty = dirtyByGroup.get(sec.id) ?? 0;
                      const currentSec =
                        (activeSectionId || sections[0]?.id) === sec.id;
                      return (
                        <li key={sec.id}>
                          <a
                            href={`#${sec.anchorId}`}
                            aria-current={currentSec ? "location" : undefined}
                            onClick={(event) => {
                              event.preventDefault();
                              jumpToSection(sec.anchorId);
                            }}
                          >
                            <span>{sec.title}</span>
                            {secDirty > 0 && (
                              <span
                                className="settings-toc-dot"
                                aria-label={t("{0} 项未保存", "{0} unsaved", [
                                  secDirty,
                                ])}
                              />
                            )}
                          </a>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </div>
            );
          })}
        </nav>

        <div className="settings-main">
          {returnSearchQuery && (
            <div className="settings-search-context-bar">
              <button
                type="button"
                className="settings-back-to-search"
                onClick={() => changeSearch(returnSearchQuery)}
              >
                <ArrowLeft size={15} />
                <span>
                  {t("返回“{0}”的搜索结果", "Back to results for “{0}”", [
                    returnSearchQuery,
                  ])}
                </span>
              </button>
              <button
                type="button"
                className="icon-button"
                aria-label={t("关闭搜索提示", "Dismiss search banner")}
                onClick={() =>
                  void navigate(
                    {
                      pathname: location.pathname,
                      search: location.search,
                      hash: location.hash,
                    },
                    { replace: true, state: null },
                  )
                }
              >
                <X size={15} />
              </button>
            </div>
          )}

          {showToc && (
            <details className="settings-toc-mobile" ref={mobileTocRef}>
              <summary>
                <ListTree size={16} />
                <span>{t("本页目录", "On this page")}</span>
                <span className="settings-toc-mobile-current">
                  {activeSectionTitle}
                </span>
              </summary>
              <nav aria-label={t("本页分区目录", "Page sections")}>
                {sections.map((sec) => {
                  const secDirty = dirtyByGroup.get(sec.id) ?? 0;
                  const currentSec =
                    (activeSectionId || sections[0]?.id) === sec.id;
                  return (
                    <a
                      key={sec.id}
                      href={`#${sec.anchorId}`}
                      aria-current={currentSec ? "location" : undefined}
                      onClick={(event) => {
                        event.preventDefault();
                        jumpToSection(sec.anchorId);
                      }}
                    >
                      <span>{sec.title}</span>
                      {secDirty > 0 && (
                        <span className="settings-nav-dirty-count">
                          {secDirty}
                        </span>
                      )}
                    </a>
                  );
                })}
              </nav>
            </details>
          )}

          {search.trim() ? (
            <section
              id="settings-search-results"
              className="settings-search-results"
              aria-label={t("搜索结果", "Search results")}
            >
              <header className="settings-search-results-header">
                <h2>
                  {t("搜索结果", "Search results")}{" "}
                  <span>{searchHits.length}</span>
                </h2>
                <button
                  type="button"
                  className="button button-secondary"
                  onClick={() => changeSearch("", true)}
                >
                  {t("返回当前分类", "Return to category")}
                </button>
              </header>
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
              {!index.isPending &&
                !index.isError &&
                searchHits.length === 0 && (
                  <div className="settings-empty">
                    <Search size={30} />
                    <h3>{t("没有找到相关设置", "No matching settings")}</h3>
                    <p>
                      {t(
                        "尝试输入功能词（如“回退”“并发”“SMTP”“配额”）或配置键名称。",
                        "Try a task keyword (such as fallback, concurrency, SMTP or quota) or a setting key.",
                      )}
                    </p>
                  </div>
                )}
              <div className="settings-search-list">
                <span className="sr-only" aria-live="polite">
                  {visibleSearchHits[activeSearchIdx] &&
                    `${activeSearchIdx + 1}/${visibleSearchHits.length} · ${visibleSearchHits[activeSearchIdx].title}`}
                </span>
                {visibleSearchHits.map((hit, idx) => (
                  <Link
                    id={`settings-search-hit-${idx}`}
                    className={`settings-search-result${idx === activeSearchIdx ? " is-highlighted" : ""}`}
                    key={hit.id}
                    to={hit.to}
                    state={{ fromSearch: search.trim() }}
                    onMouseEnter={() => setActiveSearchIdx(idx)}
                  >
                    <div className="settings-search-result-body">
                      <div className="settings-search-breadcrumb">
                        <span>
                          {hit.categoryLabel} › {hit.sectionLabel}
                        </span>
                        <span className="settings-badge settings-badge-muted">
                          {hit.matchBadge}
                        </span>
                        {hit.requiresRestart && (
                          <span className="settings-badge settings-badge-warn">
                            {t("需重启", "Restart required")}
                          </span>
                        )}
                        {hit.readOnly && (
                          <span className="settings-badge settings-badge-muted">
                            {t("部署只读", "Read-only")}
                          </span>
                        )}
                      </div>
                      <strong>{hit.title}</strong>
                      {hit.description && <p>{hit.description}</p>}
                    </div>
                    <ArrowRight size={17} />
                  </Link>
                ))}
              </div>
              {searchHits.length > 80 && (
                <p className="settings-hint settings-search-more">
                  {t(
                    "显示前 80 项（共 {0} 项），请补充关键词缩小范围。",
                    "Showing the first 80 of {0} results. Add another keyword to narrow down.",
                    [searchHits.length],
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
              category={current.id}
              title={t(current.label[0], current.label[1])}
              subtitle={
                current.description
                  ? t(current.description[0], current.description[1])
                  : undefined
              }
              store={store}
              accounts={accounts}
              modelSelection={modelSelection}
              setModelSelection={setModelSelection}
              selectionAvailable={selectionAvailable}
              mutateFieldApi={mutateFieldApi}
              refreshScope={refreshScope}
              onReloadConflict={reloadConflictField}
              onSectionsChange={setSections}
              onActiveSectionChange={setActiveSectionId}
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

          {showActionBar && (
            <div
              className="settings-dirty-bar"
              ref={actionBarRef}
              role="region"
              aria-label={t("未保存修改操作栏", "Unsaved changes bar")}
            >
              {store.dirtyCount > 0 ? (
                <>
                  <div className="settings-dirty-summary" role="status">
                    <span className="settings-unsaved-dot" aria-hidden="true" />
                    <strong>
                      {t("{0} 项未保存", "{0} unsaved", [store.dirtyCount])}
                    </strong>
                    {store.batchSaving && store.batchProgress && (
                      <span className="settings-hint">
                        {t("正在提交 {0}/{1}…", "Saving {0}/{1}…", [
                          store.batchProgress.completed,
                          store.batchProgress.total,
                        ])}
                      </span>
                    )}
                    {!store.batchSaving &&
                      report &&
                      (report.counts.failed > 0 ||
                        report.counts.conflict > 0 ||
                        report.counts.skipped > 0) && (
                        <span className="settings-dirty-warning">
                          <AlertTriangle size={14} />
                          {t(
                            "部分设置未保存成功，请核对原因",
                            "Some changes were not saved; review details",
                          )}
                        </span>
                      )}
                  </div>
                  <div className="settings-dirty-actions">
                    <button
                      type="button"
                      className="button button-secondary"
                      onClick={() => setChangesOpen(true)}
                    >
                      {t("查看修改", "Review changes")}
                    </button>
                    <button
                      type="button"
                      className="button button-secondary"
                      disabled={store.savingAny}
                      onClick={async () => {
                        if (
                          await confirm({
                            title: t("放弃修改？", "Discard changes?"),
                            description:
                              store.message("confirm_discard") !==
                              "confirm_discard"
                                ? store.message("confirm_discard")
                                : t(
                                    "放弃全部未保存的设置修改？",
                                    "Discard all unsaved setting changes?",
                                  ),
                            destructive: true,
                          })
                        )
                          store.discardAll();
                      }}
                    >
                      {t("放弃修改", "Discard changes")}
                    </button>
                    <button
                      type="button"
                      className="button primary"
                      disabled={store.savingAny}
                      onClick={() => void executeBatchSave(false)}
                    >
                      {store.batchSaving && (
                        <LoaderCircle size={15} className="spin" />
                      )}
                      {store.batchSaving
                        ? t("保存中…", "Saving…")
                        : t("保存修改", "Save changes")}
                    </button>
                  </div>
                </>
              ) : (
                report && (
                  <>
                    <div className="settings-dirty-summary" role="status">
                      <CheckCircle2
                        size={16}
                        className="settings-status-saved"
                      />
                      <span>
                        {report.counts.restartRequired > 0
                          ? t(
                              "已保存 {0} 项（其中 {1} 项需重启应用生效）",
                              "Saved {0} item(s) ({1} require application restart)",
                              [
                                report.counts.saved +
                                  report.counts.pending +
                                  report.counts.restartRequired,
                                report.counts.restartRequired,
                              ],
                            )
                          : report.counts.pending > 0
                            ? t(
                                "已保存 {0} 项（{1} 项等待后台同步生效）",
                                "Saved {0} item(s) ({1} pending runtime sync)",
                                [
                                  report.counts.saved + report.counts.pending,
                                  report.counts.pending,
                                ],
                              )
                            : t(
                                "已保存 {0} 项修改并生效",
                                "Saved {0} change(s)",
                                [report.counts.saved],
                              )}
                      </span>
                    </div>
                    <div className="settings-dirty-actions">
                      {(report.counts.restartRequired > 0 ||
                        report.counts.pending > 0) && (
                        <button
                          type="button"
                          className="button button-secondary"
                          onClick={() => setChangesOpen(true)}
                        >
                          {t("查看详情", "Details")}
                        </button>
                      )}
                      <button
                        type="button"
                        className="icon-button"
                        aria-label={t("关闭保存提示", "Dismiss save notice")}
                        onClick={() => store.clearBatchReport()}
                      >
                        <X size={16} />
                      </button>
                    </div>
                  </>
                )
              )}
            </div>
          )}
        </div>
      </div>

      <ChangesDialog
        open={changesOpen}
        onOpenChange={setChangesOpen}
        store={store}
        onLocate={locateField}
        onSaveAll={() => void executeBatchSave(false)}
        onRetryFailed={() => void executeBatchSave(true)}
        onReloadConflict={reloadConflictField}
      />
    </div>
  );
}

function CategoryPanel({
  category,
  title,
  subtitle,
  store,
  accounts,
  modelSelection,
  setModelSelection,
  selectionAvailable,
  mutateFieldApi,
  refreshScope,
  onReloadConflict,
  onSectionsChange,
  onActiveSectionChange,
}: {
  category: Category;
  title: string;
  subtitle?: string;
  store: SettingsStore;
  accounts: AccountsQuery;
  modelSelection: ModelSelection | null;
  setModelSelection: (selection: ModelSelection | null) => void;
  selectionAvailable: boolean;
  mutateFieldApi: (
    field: ConfigField,
    value: Json,
    reset?: boolean,
  ) => Promise<ConfigField>;
  refreshScope: (scope: string) => Promise<ConfigField[]>;
  onReloadConflict: (field: ConfigField) => Promise<void>;
  onSectionsChange: (sections: ResolvedSection[]) => void;
  onActiveSectionChange: (sectionId: string) => void;
}) {
  const { language, t } = useLocale();
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

  const key = ["settings", category, language] as const;
  const read = useCallback(
    async ({ signal }: { signal: AbortSignal }) => {
      const response = await api<SettingsResponse>(
        `/api/v1/config-center?category=${category}&language=${language}`,
        { signal },
      );
      setCsrfToken(response.csrf);
      store.recordCatalogMetadata(response.groups, response.messages);
      return response;
    },
    [category, language, store],
  );

  const query = useQuery({
    queryKey: key,
    queryFn: read,
    refetchInterval: (q) =>
      q.state.data?.fields.some((field) => field.effect === "pending")
        ? Math.max(1000, q.state.data.syncIntervalMs)
        : false,
  });

  useEffect(() => {
    if (query.data) {
      store.recordCatalogMetadata(query.data.groups, query.data.messages);
      store.reconcile(query.data.fields);
    }
  }, [query.data, store]);

  useEffect(() => {
    if (typeof BroadcastChannel === "undefined") return;
    const channel = new BroadcastChannel("sakura-settings");
    channel.onmessage = () =>
      void queryClient.invalidateQueries({ queryKey: ["settings", category] });
    return () => channel.close();
  }, [category, queryClient]);

  const message = useCallback(
    (code: string) => query.data?.messages[code] ?? store.message(code),
    [query.data?.messages, store],
  );

  const sections = useMemo(
    () =>
      resolveCategorySections(
        category,
        query.data?.fields ?? [],
        query.data?.groups ?? {},
        t,
        showFields,
      ),
    [category, query.data?.fields, query.data?.groups, t, showFields],
  );

  useEffect(() => {
    onSectionsChange(sections);
    if (sections.length) onActiveSectionChange(sections[0].id);
    else onActiveSectionChange("");
  }, [sections, onSectionsChange, onActiveSectionChange]);

  // Scroll-spy tracking for active section highlight in TOC.
  useEffect(() => {
    if (sections.length <= 1 || typeof IntersectionObserver === "undefined")
      return;
    const elements = sections
      .map((sec) => document.getElementById(sec.anchorId))
      .filter((el): el is HTMLElement => el !== null);
    if (!elements.length) return;
    const visible = new Set<string>();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) visible.add(entry.target.id);
          else visible.delete(entry.target.id);
        }
        const firstVisible = sections.find((sec) => visible.has(sec.anchorId));
        if (firstVisible) onActiveSectionChange(firstVisible.id);
      },
      { rootMargin: "-80px 0px -60% 0px", threshold: 0 },
    );
    for (const el of elements) observer.observe(el);
    return () => observer.disconnect();
  }, [sections, onActiveSectionChange]);

  // Deep-link hash scroll & keyboard focus for both fields and sections (including legacy ?tab=security|notifications).
  const legacyTab = new URLSearchParams(location.search).get("tab") ?? "";
  const rawHash =
    safeHash(location.hash) || (legacyTab ? `section-${legacyTab}` : "");
  const normalizedFieldHash = rawHash.replace(/^setting-/, "");
  const targetedFieldId = query.data?.fields.find(
    (field) => field.id === normalizedFieldHash,
  )?.id;
  const loaded = Boolean(query.data);
  useEffect(() => {
    if (!rawHash) return;
    if (targetedFieldId) {
      const frame = requestAnimationFrame(() =>
        locateSettingField(targetedFieldId),
      );
      return () => cancelAnimationFrame(frame);
    }
    const secCandidate =
      document.getElementById(rawHash) ??
      document.getElementById(sectionAnchorId(rawHash));
    if (secCandidate) {
      secCandidate.scrollIntoView?.({ block: "start" });
      const heading = secCandidate.querySelector<HTMLElement>("h3, summary");
      heading?.focus({ preventScroll: true });
    }
  }, [rawHash, targetedFieldId, loaded, location.key]);

  const saveSingle = async (field: ConfigField, reset = false) => {
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
      (current, value, resetting) => mutateFieldApi(current, value, resetting),
      () => refreshScope(`category:${category}`),
      reset,
    );
  };

  return (
    <>
      <div className="settings-category-heading">
        <div>
          <h2>{title}</h2>
          {subtitle && <p className="settings-category-subtitle">{subtitle}</p>}
        </div>
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
              type="button"
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

      <div className="settings-sections-stack">
        {sections.map((sec) => {
          if (sec.kind === "panel") {
            if (sec.panel === "accounts")
              return (
                <div
                  key={sec.id}
                  id={sec.anchorId}
                  className="settings-anchor-block"
                >
                  <AccountsPanel store={store} />
                </div>
              );
            if (sec.panel === "model-capabilities")
              return (
                <div
                  key={sec.id}
                  id={sec.anchorId}
                  className="settings-anchor-block"
                >
                  <ModelSettings
                    store={store}
                    selection={modelSelection}
                    setSelection={setModelSelection}
                    selectionAvailable={selectionAvailable}
                    fieldHash={rawHash.startsWith("model/") ? rawHash : ""}
                    accounts={accounts.data?.data.accounts ?? []}
                    accountsLoading={accounts.isPending}
                    accountsError={accounts.isError}
                    onRetryAccounts={() => void accounts.refetch()}
                    mutateFieldApi={mutateFieldApi}
                    refreshScope={refreshScope}
                  />
                </div>
              );
            if (sec.panel === "plugins")
              return (
                <div
                  key={sec.id}
                  id={sec.anchorId}
                  className="settings-anchor-block"
                >
                  <PluginsPanel store={store} />
                </div>
              );
            if (sec.panel === "system-maintenance")
              return (
                <div
                  key={sec.id}
                  id={sec.anchorId}
                  className="settings-anchor-block"
                >
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
                </div>
              );
            if (sec.panel === "backup")
              return (
                <div
                  key={sec.id}
                  id={sec.anchorId}
                  className="settings-anchor-block"
                >
                  <BackupPanel store={store} />
                </div>
              );
            return null;
          }

          return (
            <section
              className="settings-section"
              key={sec.id}
              id={sec.anchorId}
              aria-label={sec.title}
            >
              <header>
                <div>
                  <h3 tabIndex={-1}>{sec.title}</h3>
                  {sec.summary && <p>{sec.summary}</p>}
                </div>
              </header>
              {sec.fields.map((field) => (
                <FieldRow
                  key={field.id}
                  field={field}
                  store={store}
                  message={message}
                  accounts={accounts.data?.data.accounts ?? []}
                  onSave={saveSingle}
                  onReload={onReloadConflict}
                  targeted={field.id === targetedFieldId}
                />
              ))}
            </section>
          );
        })}
      </div>
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
  const descriptionId = `description-${field.id}`;
  const metaId = `meta-${field.id}`;
  const errorId = `error-${field.id}`;
  const describedBy = [
    field.description && descriptionId,
    metaId,
    entry.error && errorId,
  ]
    .filter(Boolean)
    .join(" ");
  const disabled = !current.writable;
  const actionsDisabled = disabled || entry.saving || store.batchSaving;
  const edit = (value: Json) => store.edit(field.id, value);
  const structured = field.type === "array" || field.type === "object";
  const isPromptOrMultiline =
    !field.sensitive &&
    !structured &&
    !field.constraints.enum &&
    field.type !== "boolean" &&
    (/prompt|template/.test(field.path.at(-1) ?? field.key) ||
      (typeof field.value === "string" &&
        (field.value.includes("\n") || field.value.length > 160)));
  const wide = structured || isPromptOrMultiline;

  const facts = describeFieldConstraints(current, t);

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
        aria-describedby={describedBy}
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
          aria-describedby={describedBy}
          onChange={(event) => edit(event.target.checked)}
        />
        <span aria-hidden="true" />
      </label>
    );
  else if (isPromptOrMultiline)
    control = (
      <textarea
        id={id}
        className="settings-prompt-textarea"
        rows={7}
        disabled={disabled}
        aria-invalid={!!entry.error}
        aria-describedby={describedBy}
        value={String(entry.draft ?? "")}
        onChange={(event) => edit(event.target.value)}
      />
    );
  else
    control = (
      <input
        id={id}
        className={
          ["number", "integer"].includes(field.type)
            ? "settings-number-input"
            : undefined
        }
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
            ? message(current.hasValue ? "credential" : "credential_empty")
            : undefined
        }
        autoComplete={field.sensitive ? "new-password" : undefined}
        step={field.type === "integer" ? 1 : "any"}
        min={field.constraints.ge}
        max={field.constraints.le}
        aria-invalid={!!entry.error}
        aria-describedby={describedBy}
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
      className={`settings-field${wide ? " settings-field-wide" : ""}${targeted ? " settings-field-targeted" : ""}${dirty ? " settings-field-dirty" : ""}`}
      id={`setting-${field.id}`}
      tabIndex={-1}
      role="group"
      aria-labelledby={`${id}-label`}
    >
      <div className="settings-field-copy">
        <div className="settings-field-title-row">
          <label id={`${id}-label`} htmlFor={structured ? undefined : id}>
            {field.label}
          </label>
          {!field.writable ? (
            <span className="settings-badge settings-badge-muted">
              {t("部署只读", "Deployment read-only")}
            </span>
          ) : current.overridden ? (
            <span className="settings-badge settings-badge-accent">
              {t("已自定义", "Customized")}
            </span>
          ) : current.source === "environment" ? (
            <span className="settings-badge settings-badge-muted">
              {t("环境变量", "From env")}
            </span>
          ) : null}
          {field.requiresRestart && (
            <span className="settings-badge settings-badge-warn">
              {t("修改需重启", "Restart required")}
            </span>
          )}
          {field.sensitive && (
            <span className="settings-badge settings-badge-muted">
              {current.hasValue
                ? t("凭据已设", "Secret set")
                : t("凭据未设", "Not set")}
            </span>
          )}
        </div>

        {field.description && (
          <p id={descriptionId} className="settings-field-description">
            {field.description}
          </p>
        )}

        {!field.writable && (
          <p className="settings-field-readonly-note">
            {message("deployment_owned")}
          </p>
        )}

        {facts.length > 0 && (
          <div id={metaId} className="settings-field-facts">
            {facts.map((fact) => (
              <span key={fact}>{fact}</span>
            ))}
          </div>
        )}

        <details className="settings-field-details">
          <summary>{t("详情", "Details")}</summary>
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
              type="button"
              className="button button-secondary"
              disabled={actionsDisabled || entry.conflict}
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
            <p className="settings-error" id={errorId} role="alert">
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
                ) : entry.state === "pending" ? (
                  <Clock size={14} />
                ) : entry.state === "restart_required" ? (
                  <Power size={14} />
                ) : (
                  <Info size={14} />
                )}
                {message(entry.state)}
              </span>
            )}
          {entry.conflict ? (
            <div className="settings-field-actions">
              <button
                type="button"
                className="button button-secondary"
                disabled={actionsDisabled}
                onClick={() => void onReload(current)}
              >
                {message("reload_field")}
              </button>
            </div>
          ) : (
            dirty && (
              <div className="settings-field-actions">
                <button
                  type="button"
                  className="button button-secondary"
                  disabled={entry.saving || store.batchSaving}
                  onClick={() => store.revert(current.id)}
                >
                  {t("取消", "Cancel")}
                </button>
                <button
                  type="button"
                  className="button primary"
                  disabled={entry.saving || store.batchSaving}
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

function ModelSettings({
  store,
  selection,
  setSelection,
  selectionAvailable,
  fieldHash,
  accounts,
  accountsLoading,
  accountsError,
  onRetryAccounts,
  mutateFieldApi,
  refreshScope,
}: {
  store: SettingsStore;
  selection: ModelSelection | null;
  setSelection: (selection: ModelSelection | null) => void;
  selectionAvailable: boolean;
  fieldHash: string;
  accounts: Account[];
  accountsLoading: boolean;
  accountsError: boolean;
  onRetryAccounts: () => void;
  mutateFieldApi: (
    field: ConfigField,
    value: Json,
    reset?: boolean,
  ) => Promise<ConfigField>;
  refreshScope: (scope: string) => Promise<ConfigField[]>;
}) {
  const { language, t } = useLocale();
  const confirm = useConfirm();
  const available = accounts.filter(
    (account) =>
      account.enabled && account.provider_id && modelsFor(account).length > 0,
  );
  const selectedAccount = accounts.find(
    (account) => account.id === selection?.account,
  );

  const getModelEntries = () =>
    [...store.entries.values()].filter((entry) =>
      entry.field.id.startsWith("model/"),
    );

  const confirmDiscardPrompt = () =>
    confirm({
      title: t("切换模型？", "Switch model?"),
      description: t(
        "放弃当前模型的未保存修改？",
        "Discard unsaved changes for this model?",
      ),
      destructive: true,
    });

  const clearSelection = async () => {
    if (!selection) return;
    const modelEntries = getModelEntries();
    if (modelEntries.some((entry) => entry.saving) || store.batchSaving) return;
    if (modelEntries.some(isDirty) && !(await confirmDiscardPrompt())) return;
    store.forget("model/");
    setSelection(null);
  };

  const select = async (account: Account, model: string) => {
    if (!account.enabled || !modelsFor(account).includes(model)) return;
    if (selection?.account === account.id && selection.model === model) return;
    const sameProviderModel =
      selection?.provider === account.provider_id && selection.model === model;
    if (!sameProviderModel) {
      const modelEntries = getModelEntries();
      if (modelEntries.some((entry) => entry.saving) || store.batchSaving)
        return;
      if (modelEntries.some(isDirty) && !(await confirmDiscardPrompt())) return;
      store.forget("model/");
    }
    setSelection({ account: account.id, provider: account.provider_id, model });
  };

  const read = async ({ signal }: { signal: AbortSignal }) => {
    const data = await api<SettingsResponse>(
      `/api/v1/config-center?category=ai&language=${language}&provider=${encodeURIComponent(selection?.provider ?? "")}&model=${encodeURIComponent(selection?.model ?? "")}`,
      { signal },
    );
    setCsrfToken(data.csrf);
    store.recordCatalogMetadata(data.groups, data.messages);
    return data;
  };

  // Keyed by (provider, model, language) — shared across accounts using the same provider+model!
  const query = useQuery({
    queryKey: [
      "settings-model",
      selection?.provider ?? "",
      selection?.model ?? "",
      language,
    ],
    queryFn: read,
    enabled: selectionAvailable,
    refetchInterval: (q) =>
      q.state.data?.fields.some((field) => field.effect === "pending")
        ? Math.max(1000, q.state.data.syncIntervalMs)
        : false,
  });

  useEffect(() => {
    if (query.data && selectionAvailable) {
      store.recordCatalogMetadata(query.data.groups, query.data.messages);
      store.reconcile(query.data.fields);
    }
  }, [query.data, selectionAvailable, selection, store]);

  const targeted =
    selectionAvailable &&
    query.data?.fields.some((field) => field.id === fieldHash);
  useEffect(() => {
    if (!targeted) return;
    const frame = requestAnimationFrame(() => locateSettingField(fieldHash));
    return () => cancelAnimationFrame(frame);
  }, [targeted, fieldHash]);

  const message = (code: string) =>
    query.data?.messages[code] ?? store.message(code);

  const save = async (field: ConfigField, reset = false) => {
    if (!selectionAvailable || !selection) return;
    if (
      reset &&
      !(await confirm({
        title: t("恢复默认值？", "Restore default?"),
        description: message("confirm_reset"),
        destructive: true,
      }))
    )
      return;
    const scope = `model:${selection.provider}:${selection.model}`;
    await store.save(
      field.id,
      (current, value, resetting) => mutateFieldApi(current, value, resetting),
      () => refreshScope(scope),
      reset,
    );
  };

  const hasDirtyModelEntries = [...store.entries.values()].some(
    (entry) => entry.field.id.startsWith("model/") && isDirty(entry),
  );

  return (
    <section
      className="settings-model-panel"
      aria-label={t("单模型能力覆盖", "Per-model capabilities")}
    >
      <header className="settings-model-header">
        <div>
          <h3 tabIndex={-1}>
            <SlidersHorizontal size={17} />{" "}
            {t(
              "3. 单模型能力与参数覆盖",
              "3. Per-model capabilities & parameters",
            )}
          </h3>
          <p>
            {t(
              "从已配置的启用账号选择具体模型。能力覆盖按“供应商 + 模型 ID”读写并由相同供应商模型的账号共享。",
              "Choose a model from an enabled account. Capability overrides are stored by provider + model ID and shared across matching accounts.",
            )}
          </p>
        </div>
      </header>

      {accountsLoading ? (
        <p className="settings-panel-status" role="status">
          {t("正在加载账号…", "Loading accounts…")}
        </p>
      ) : accountsError ? (
        <p className="settings-panel-status" role="alert">
          {t("无法加载 AI 账号。", "Could not load AI accounts.")}{" "}
          <button className="button" onClick={onRetryAccounts}>
            {t("重试", "Retry")}
          </button>
        </p>
      ) : (
        <>
          {!available.length && (
            <p className="settings-panel-status" role="status">
              {t(
                "暂无可用的账号和模型，请先在上方配置并启用 AI 账号。",
                "No available accounts and models. Configure and enable an AI account above first.",
              )}
            </p>
          )}
          <div className="settings-model-picker">
            <label>
              {t("AI 账号", "AI account")}
              <select
                value={selection?.account ?? ""}
                onChange={(event) => {
                  if (!event.target.value) {
                    void clearSelection();
                    return;
                  }
                  const account = available.find(
                    (item) => item.id === event.target.value,
                  );
                  if (account) void select(account, modelsFor(account)[0]);
                }}
              >
                <option value="">{t("选择账号", "Select account")}</option>
                {accounts.map((account) => (
                  <option
                    key={account.id}
                    value={account.id}
                    disabled={!available.includes(account)}
                  >
                    {account.name} · {account.provider_id}
                    {!account.enabled
                      ? ` · ${t("已禁用", "Disabled")}`
                      : !modelsFor(account).length
                        ? ` · ${t("未配置模型", "No models configured")}`
                        : ""}
                  </option>
                ))}
                {selection && !selectedAccount && (
                  <option value={selection.account} disabled>
                    {selection.account} ·{" "}
                    {t("账号不可用", "Account unavailable")}
                  </option>
                )}
              </select>
            </label>
            <label>
              {t("模型", "Model")}
              <select
                value={selection?.model ?? ""}
                disabled={!selectedAccount?.enabled}
                onChange={(event) => {
                  if (selectedAccount)
                    void select(selectedAccount, event.target.value);
                }}
              >
                {!selection && (
                  <option value="">{t("选择模型", "Select model")}</option>
                )}
                {selectedAccount &&
                  modelsFor(selectedAccount).map((model) => (
                    <option key={model} value={model}>
                      {model}
                    </option>
                  ))}
              </select>
            </label>
          </div>
        </>
      )}

      {selection && !selectionAvailable && (
        <div className="settings-model-unavailable" role="alert">
          <p className="settings-error">
            {t(
              "所选账号或模型已不可用。请选择可用模型；未保存的修改仍保留。",
              "The selected account or model is unavailable. Select an available model; unsaved changes are retained.",
            )}
          </p>
          {hasDirtyModelEntries && (
            <button
              type="button"
              className="button button-secondary"
              onClick={() => {
                store.forget("model/");
                setSelection(null);
              }}
            >
              {t("放弃此模型的修改", "Discard model changes")}
            </button>
          )}
        </div>
      )}

      {query.isFetching && (
        <p className="settings-panel-status" role="status">
          {t("正在加载…", "Loading…")}
        </p>
      )}
      {query.isError && (
        <p className="settings-panel-status settings-error" role="alert">
          {t(
            "无法加载该模型的设置，请检查模型 ID。",
            "Could not load these model settings. Check the model ID.",
          )}
        </p>
      )}
      {selectionAvailable &&
        query.data?.fields.map((field) => (
          <FieldRow
            key={field.id}
            field={field}
            store={store}
            message={message}
            accounts={[]}
            onSave={save}
            targeted={field.id === fieldHash}
            onReload={async (targetField) => {
              if (!selection) return;
              if (
                await confirm({
                  title: t("重新加载设置？", "Reload setting?"),
                  description: message("confirm_discard"),
                  destructive: true,
                })
              ) {
                const fresh = await refreshScope(
                  `model:${selection.provider}:${selection.model}`,
                );
                const current = fresh.find(
                  (item) => item.id === targetField.id,
                );
                if (current) store.accept(current);
              }
            }}
          />
        ))}
    </section>
  );
}
