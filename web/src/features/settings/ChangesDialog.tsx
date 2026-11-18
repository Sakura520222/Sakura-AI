import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Clock,
  KeyRound,
  LoaderCircle,
  LocateFixed,
  Power,
  RotateCcw,
  Undo2,
} from "lucide-react";
import { useLocale } from "../../app/locale";
import { Dialog } from "../../components/ui/Dialog";
import { CATEGORY_ITEMS, lookupSectionTitle } from "./sectionMeta";
import type {
  BatchItemOutcome,
  BatchSaveReport,
  Entry,
  SettingsStore,
} from "./state";
import type { Category, ConfigField, Json } from "./types";

function formatPreviewValue(
  field: ConfigField,
  value: Json,
  t: (zh: string, en: string, values?: readonly unknown[]) => string,
): string {
  if (value === null || value === undefined) return t("未设置", "Not set");
  if (typeof value === "boolean")
    return value ? t("开启", "On") : t("关闭", "Off");
  if (typeof value === "number") return String(value);
  if (typeof value === "string") {
    if (field.optionLabels[value]) return field.optionLabels[value];
    if (value === "") return t("（空字符串）", "(empty string)");
    if (value.includes("\n") || value.length > 72) {
      const lines = value.split("\n").length;
      const snippet = value.replace(/\s+/g, " ").trim().slice(0, 48);
      return t("{0} 行 · {1} 字符（{2}…）", "{0} lines · {1} chars ({2}…)", [
        lines,
        value.length,
        snippet,
      ]);
    }
    return value;
  }
  if (
    field.id.startsWith("binding/") &&
    typeof value === "object" &&
    !Array.isArray(value)
  ) {
    const binding = value as {
      primary?: { account?: string; provider?: string; model?: string };
      fallback?: { account?: string; provider?: string; model?: string }[];
    };
    const primaryAccount =
      binding.primary?.account || binding.primary?.provider || "—";
    const primaryModel = binding.primary?.model || "—";
    const chain = (binding.fallback ?? [])
      .map(
        (item) =>
          `${item.account || item.provider || "?"}/${item.model || "?"}`,
      )
      .join(" → ");
    return chain
      ? t("主模型 {0}/{1} · 回退 [{2}]", "Primary {0}/{1} · Fallback [{2}]", [
          primaryAccount,
          primaryModel,
          chain,
        ])
      : t("主模型 {0}/{1} · 无回退", "Primary {0}/{1} · No fallback", [
          primaryAccount,
          primaryModel,
        ]);
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return t("空列表（0 项）", "Empty list (0 items)");
    const scalars = value.every(
      (item) => typeof item === "string" || typeof item === "number",
    );
    if (scalars && value.length <= 4) return `[${value.join(", ")}]`;
    return t("列表（{0} 项）", "List ({0} items)", [value.length]);
  }
  const keys = Object.keys(value);
  return t("配置对象（{0} 项）", "Object ({0} entries)", [keys.length]);
}

export function describeBatchReason(
  code: string | undefined,
  store: SettingsStore,
  t: (zh: string, en: string) => string,
): string {
  if (!code) return "";
  if (code === "model_unavailable")
    return t(
      "所选模型的账号已停用或移除，请先切换为可用账号。",
      "The selected model's account is disabled or unavailable.",
    );
  if (code === "section_overlap")
    return t(
      "与同一分区的整表编辑重叠，已跳过以避免覆盖其他子项。",
      "Overlaps with a whole-section edit in the same group; skipped to prevent overwriting.",
    );
  const translated = store.message(code);
  if (translated !== code) return translated;
  if (code === "conflict")
    return t(
      "服务端值已被修改，请先核对服务端值后再提交。",
      "Changed on the server; review the server value before saving.",
    );
  if (code === "network")
    return t("网络请求失败，可稍后重试。", "Request failed; retry when ready.");
  return code;
}

export function ChangesDialog({
  open,
  onOpenChange,
  store,
  onLocate,
  onSaveAll,
  onRetryFailed,
  onReloadConflict,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  store: SettingsStore;
  onLocate: (field: { id: string; category: Category }) => void;
  onSaveAll: () => void;
  onRetryFailed: () => void;
  onReloadConflict: (field: ConfigField) => Promise<void>;
}) {
  const { t } = useLocale();
  const dirtyEntries = store.dirtyEntries;
  const groupsRecord = Object.fromEntries(store.groups.entries());
  const report = store.lastBatchReport;

  const categoryLabel = (category: Category) => {
    const item = CATEGORY_ITEMS.find((entry) => entry.id === category);
    return item ? t(item.label[0], item.label[1]) : category;
  };

  // Group dirty entries by Category -> Section
  const grouped = new Map<
    Category,
    Map<string, { sectionTitle: string; items: Entry[] }>
  >();
  for (const catMeta of CATEGORY_ITEMS) {
    if (catMeta.id === "about") continue;
    const catEntries = dirtyEntries.filter(
      (entry) => entry.field.category === catMeta.id,
    );
    if (!catEntries.length) continue;
    const sectionMap = new Map<
      string,
      { sectionTitle: string; items: Entry[] }
    >();
    for (const entry of catEntries) {
      const groupKey = entry.field.group;
      const existing = sectionMap.get(groupKey);
      if (existing) existing.items.push(entry);
      else
        sectionMap.set(groupKey, {
          sectionTitle: lookupSectionTitle(
            catMeta.id,
            groupKey,
            groupsRecord,
            t,
          ),
          items: [entry],
        });
    }
    grouped.set(catMeta.id, sectionMap);
  }

  const retryableCount = store.retryableIds.size;
  const onlyRetryable =
    retryableCount > 0 && retryableCount === dirtyEntries.length;

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={t("修改清单与保存结果", "Pending changes & save results")}
      description={t(
        "按分类核对修改内容。每项设置按各自版本独立校验并持久化，未成功项会保留在草稿中。",
        "Review pending edits by category. Each setting is validated and persisted with its own version; unsaved items stay in your draft.",
      )}
    >
      <div className="settings-changes-dialog">
        {report && (
          <BatchOutcomeSummary
            report={report}
            store={store}
            categoryLabel={categoryLabel}
            groupsRecord={groupsRecord}
            onLocate={(item) => {
              onOpenChange(false);
              onLocate(item);
            }}
          />
        )}

        {dirtyEntries.length === 0 ? (
          <p className="settings-changes-empty" role="status">
            <CheckCircle2 size={18} />
            <span>
              {t(
                "当前没有未保存的设置修改。",
                "There are no unsaved setting changes.",
              )}
            </span>
          </p>
        ) : (
          <div className="settings-changes-groups">
            {[...grouped.entries()].map(([catId, sections]) => (
              <section key={catId} className="settings-changes-category">
                <h3>{categoryLabel(catId)}</h3>
                {[...sections.entries()].map(
                  ([groupKey, { sectionTitle, items }]) => (
                    <div key={groupKey} className="settings-changes-section">
                      <h4>{sectionTitle}</h4>
                      {items.map((entry) => {
                        const unavailable = store.isFieldUnavailable(
                          entry.field.id,
                        );
                        return (
                          <div
                            key={entry.field.id}
                            className={`settings-change-item${entry.error || entry.conflict ? " has-issue" : ""}`}
                          >
                            <div className="settings-change-main">
                              <div className="settings-change-header">
                                <strong>{entry.field.label}</strong>
                                {entry.field.requiresRestart && (
                                  <span className="settings-badge settings-badge-warn">
                                    <Power size={11} />
                                    {t("需重启", "Restart required")}
                                  </span>
                                )}
                                {entry.conflict && (
                                  <span className="settings-badge settings-badge-danger">
                                    {t("版本冲突", "Conflict")}
                                  </span>
                                )}
                                {!entry.conflict && entry.error && (
                                  <span className="settings-badge settings-badge-danger">
                                    {t("校验或保存未通过", "Needs attention")}
                                  </span>
                                )}
                                {unavailable && (
                                  <span className="settings-badge settings-badge-warn">
                                    {t("账号不可用", "Account unavailable")}
                                  </span>
                                )}
                              </div>

                              {entry.field.sensitive ? (
                                <p className="settings-change-diff settings-change-secret">
                                  <KeyRound size={13} />
                                  <span>
                                    {entry.field.hasValue
                                      ? t(
                                          "已配置凭据 → 将更新（不显示内容）",
                                          "Configured credential → Will update (hidden)",
                                        )
                                      : t(
                                          "未配置凭据 → 将写入新凭据（不显示内容）",
                                          "No credential → Will set new credential (hidden)",
                                        )}
                                  </span>
                                </p>
                              ) : (
                                <p className="settings-change-diff">
                                  <span className="settings-diff-old">
                                    {formatPreviewValue(
                                      entry.field,
                                      entry.field.value,
                                      t,
                                    )}
                                  </span>
                                  <ArrowRight size={13} aria-hidden="true" />
                                  <span className="settings-diff-new">
                                    {formatPreviewValue(
                                      entry.field,
                                      entry.draft,
                                      t,
                                    )}
                                  </span>
                                </p>
                              )}

                              {entry.error && (
                                <p className="settings-error">
                                  {describeBatchReason(entry.error, store, t)}
                                </p>
                              )}
                            </div>

                            <div className="settings-change-actions">
                              <button
                                type="button"
                                className="button button-secondary"
                                onClick={() => {
                                  onOpenChange(false);
                                  onLocate(entry.field);
                                }}
                              >
                                <LocateFixed size={14} />
                                {t("定位", "Locate")}
                              </button>
                              {entry.conflict && (
                                <button
                                  type="button"
                                  className="button button-secondary"
                                  disabled={store.savingAny}
                                  onClick={() =>
                                    void onReloadConflict(entry.field)
                                  }
                                >
                                  <RotateCcw size={14} />
                                  {t("采用服务端值", "Use server value")}
                                </button>
                              )}
                              <button
                                type="button"
                                className="button button-secondary"
                                disabled={entry.saving || store.batchSaving}
                                aria-label={`${t("撤销", "Revert")}: ${entry.field.label}`}
                                onClick={() => store.revert(entry.field.id)}
                              >
                                <Undo2 size={14} />
                                {t("撤销", "Revert")}
                              </button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  ),
                )}
              </section>
            ))}
          </div>
        )}

        <div className="settings-dialog-actions">
          <button
            type="button"
            className="button"
            onClick={() => onOpenChange(false)}
          >
            {t("关闭", "Close")}
          </button>
          {report &&
            (report.counts.failed > 0 || report.counts.skipped > 0) &&
            retryableCount > 0 && (
              <button
                type="button"
                className={
                  onlyRetryable ? "button primary" : "button button-secondary"
                }
                disabled={store.savingAny}
                onClick={onRetryFailed}
              >
                <RotateCcw size={15} />
                {t("重试未成功项 ({0})", "Retry unsaved items ({0})", [
                  retryableCount,
                ])}
              </button>
            )}
          {dirtyEntries.length > 0 && !onlyRetryable && (
            <button
              type="button"
              className="button primary"
              disabled={
                store.savingAny ||
                !dirtyEntries.some(
                  ({ field, conflict }) =>
                    field.writable &&
                    !conflict &&
                    !store.isFieldUnavailable(field.id),
                )
              }
              onClick={onSaveAll}
            >
              {store.batchSaving ? (
                <LoaderCircle size={15} className="spin" />
              ) : (
                <CheckCircle2 size={15} />
              )}
              {store.batchSaving
                ? t("正在保存…", "Saving…")
                : t("保存修改 ({0})", "Save changes ({0})", [
                    dirtyEntries.length,
                  ])}
            </button>
          )}
        </div>
      </div>
    </Dialog>
  );
}

function BatchOutcomeSummary({
  report,
  store,
  categoryLabel,
  groupsRecord,
  onLocate,
}: {
  report: BatchSaveReport;
  store: SettingsStore;
  categoryLabel: (category: Category) => string;
  groupsRecord: Record<string, string>;
  onLocate: (item: BatchItemOutcome) => void;
}) {
  const { t } = useLocale();
  const { counts } = report;
  const hasProblems =
    counts.failed > 0 || counts.conflict > 0 || counts.skipped > 0;
  const dirtyIds = new Set(store.dirtyEntries.map(({ field }) => field.id));
  const attentionItems = report.outcomes.filter((item) => {
    if (item.status === "saved") return false;
    const entry = store.entries.get(item.id);
    return (
      !dirtyIds.has(item.id) || !entry?.error || entry.error !== item.reason
    );
  });

  return (
    <section
      className={`settings-batch-summary${hasProblems ? " has-problems" : ""}`}
      role={hasProblems ? "alert" : "status"}
    >
      <div className="settings-batch-counts">
        {counts.saved > 0 && (
          <span className="settings-badge settings-badge-success">
            <CheckCircle2 size={12} />
            {t("已保存 {0}", "Saved {0}", [counts.saved])}
          </span>
        )}
        {counts.pending > 0 && (
          <span className="settings-badge settings-badge-info">
            <Clock size={12} />
            {t("等待生效 {0}", "Pending sync {0}", [counts.pending])}
          </span>
        )}
        {counts.restartRequired > 0 && (
          <span className="settings-badge settings-badge-warn">
            <Power size={12} />
            {t("需要重启 {0}", "Restart required {0}", [
              counts.restartRequired,
            ])}
          </span>
        )}
        {counts.failed > 0 && (
          <span className="settings-badge settings-badge-danger">
            <AlertTriangle size={12} />
            {t("失败 {0}", "Failed {0}", [counts.failed])}
          </span>
        )}
        {counts.conflict > 0 && (
          <span className="settings-badge settings-badge-danger">
            <AlertTriangle size={12} />
            {t("冲突 {0}", "Conflict {0}", [counts.conflict])}
          </span>
        )}
        {counts.skipped > 0 && (
          <span className="settings-badge settings-badge-muted">
            {t("跳过 {0}", "Skipped {0}", [counts.skipped])}
          </span>
        )}
      </div>

      {attentionItems.length > 0 && (
        <ul className="settings-batch-outcomes">
          {attentionItems.map((item) => (
            <li key={item.id}>
              <div>
                <span className="settings-hint">
                  {categoryLabel(item.category)} ›{" "}
                  {lookupSectionTitle(
                    item.category,
                    item.group,
                    groupsRecord,
                    t,
                  )}
                </span>
                <strong>{item.label}</strong>
                <p>
                  {item.status === "pending"
                    ? t(
                        "已持久化，后台正在同步运行时状态。",
                        "Persisted; runtime synchronization is pending.",
                      )
                    : item.status === "restart_required"
                      ? t(
                          "已持久化，重启应用后生效。",
                          "Persisted; takes effect after restarting the application.",
                        )
                      : describeBatchReason(item.reason, store, t)}
                </p>
              </div>
              <button
                type="button"
                className="button button-secondary"
                onClick={() => onLocate(item)}
              >
                <LocateFixed size={13} />
                {t("定位", "Locate")}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
