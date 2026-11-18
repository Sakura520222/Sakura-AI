import type { Category, ConfigField, Json, Schema } from "./types";

export function same(a: Json, b: Json): boolean {
  const canonical = (value: Json): string =>
    JSON.stringify(value, (_key, item: unknown) =>
      item && typeof item === "object" && !Array.isArray(item)
        ? Object.fromEntries(
            Object.entries(item).sort(([a], [b]) => a.localeCompare(b)),
          )
        : item,
    );
  return canonical(a) === canonical(b);
}

export function initialValue(schema: Schema): Json {
  if ("default" in schema) return structuredClone(schema.default ?? null);
  if ("const" in schema) return schema.const ?? null;
  if (schema.enum?.length) return schema.enum[0];
  if (schema.anyOf)
    return initialValue(schema.anyOf.find((s) => s.type !== "null") ?? {});
  if (schema.type === "object")
    return Object.fromEntries(
      Object.entries(schema.properties ?? {})
        .filter(([key]) => schema.required?.includes(key))
        .map(([key, value]) => [key, initialValue(value)]),
    );
  if (schema.type === "array") return [];
  if (schema.type === "boolean") return false;
  if (schema.type === "integer" || schema.type === "number")
    return schema.minimum ?? 0;
  if (schema.type === "null") return null;
  return "";
}

/** Resolve local presentation-schema references without evaluating remote URLs. */
export function expandSchema(
  schema: Schema,
  definitions = schema.$defs ?? {},
  depth = 0,
): Schema {
  if (depth > 32) return {};
  if (schema.$ref) {
    const key = schema.$ref.startsWith("#/$defs/") ? schema.$ref.slice(8) : "";
    const { $ref: _reference, ...rest } = schema;
    return expandSchema(
      { ...(definitions[key] ?? {}), ...rest },
      definitions,
      depth + 1,
    );
  }
  const result: Schema = { ...schema };
  if (schema.properties)
    result.properties = Object.fromEntries(
      Object.entries(schema.properties).map(([key, child]) => [
        key,
        expandSchema(child, definitions, depth + 1),
      ]),
    );
  if (schema.items)
    result.items = expandSchema(schema.items, definitions, depth + 1);
  if (schema.anyOf)
    result.anyOf = schema.anyOf.map((child) =>
      expandSchema(child, definitions, depth + 1),
    );
  if (typeof schema.additionalProperties === "object")
    result.additionalProperties = expandSchema(
      schema.additionalProperties,
      definitions,
      depth + 1,
    );
  return result;
}

export function schemaError(value: Json, schema: Schema): string | undefined {
  if (schema.anyOf)
    return schema.anyOf.some((s) => !schemaError(value, s))
      ? undefined
      : "invalid_type";
  const type =
    value === null ? "null" : Array.isArray(value) ? "array" : typeof value;
  if (
    schema.type &&
    (schema.type === "integer"
      ? !Number.isInteger(value)
      : type !== schema.type)
  )
    return "invalid_type";
  if (schema.enum && !schema.enum.some((item) => same(value, item)))
    return "invalid_choice";
  if ("const" in schema && !same(value, schema.const ?? null))
    return "invalid_choice";
  if (
    typeof value === "number" &&
    (!Number.isFinite(value) ||
      (schema.minimum != null && value < schema.minimum) ||
      (schema.maximum != null && value > schema.maximum))
  )
    return "out_of_range";
  if (
    typeof value === "number" &&
    ((schema.exclusiveMinimum != null && value <= schema.exclusiveMinimum) ||
      (schema.exclusiveMaximum != null && value >= schema.exclusiveMaximum))
  )
    return "out_of_range";
  if (typeof value === "string") {
    if (
      (schema.minLength != null && value.length < schema.minLength) ||
      (schema.maxLength != null && value.length > schema.maxLength)
    )
      return "out_of_range";
    if (schema.pattern && !new RegExp(schema.pattern).test(value))
      return "invalid_value";
  }
  if (Array.isArray(value)) {
    if (schema.minItems != null && value.length < schema.minItems)
      return "editor_incomplete";
    for (const item of value) {
      const error = schemaError(item, schema.items ?? {});
      if (error) return error;
    }
  } else if (value && typeof value === "object") {
    if (schema.required?.some((key) => !(key in value)))
      return "editor_incomplete";
    for (const [key, item] of Object.entries(value)) {
      const child =
        schema.properties?.[key] ??
        (typeof schema.additionalProperties === "object"
          ? schema.additionalProperties
          : {});
      const error = schemaError(item, child);
      if (error) return error;
    }
  }
  return undefined;
}

export function validateValue(
  field: ConfigField,
  value: Json,
): string | undefined {
  if (
    field.sensitive &&
    (typeof value !== "string" ||
      !value.trim() ||
      value.includes("****") ||
      value === "[REDACTED]")
  )
    return "credential_required";
  if (field.id.startsWith("binding/")) {
    const binding = value as { primary?: Json; fallback?: Json[] } | null;
    for (const raw of [binding?.primary, ...(binding?.fallback ?? [])]) {
      const row = raw as { account?: string; model?: string } | undefined;
      if (!row?.account || !row.model?.trim()) return "binding_incomplete";
    }
  }
  const constraints = field.constraints;
  const error = schemaError(value, {
    ...((field.editorSchema as Schema) ?? { type: field.type }),
    enum: constraints.enum,
    minimum: constraints.ge,
    maximum: constraints.le,
    minLength: constraints.min_length,
    maxLength: constraints.max_length,
  });
  if (error) return error;
  if (
    typeof value === "number" &&
    ((constraints.gt != null && value <= constraints.gt) ||
      (constraints.lt != null && value >= constraints.lt))
  )
    return "out_of_range";
  return undefined;
}

export interface Entry {
  field: ConfigField;
  draft: Json;
  generation: number;
  saving: boolean;
  conflict: boolean;
  error?: string;
  state:
    | "unchanged"
    | "unsaved"
    | "saving"
    | "saved"
    | "failed"
    | "pending"
    | "restart_required";
}

export const isDirty = (entry: Entry): boolean =>
  !same(entry.draft, entry.field.value);

function changedBaseline(previous: ConfigField, current: ConfigField): boolean {
  return (
    previous.source !== current.source ||
    !same(previous.value, current.value) ||
    (previous.sensitive && previous.version !== current.version)
  );
}

export type BatchOutcomeStatus =
  "saved" | "pending" | "restart_required" | "failed" | "conflict" | "skipped";

export interface BatchItemOutcome {
  id: string;
  category: Category;
  group: string;
  label: string;
  sensitive: boolean;
  status: BatchOutcomeStatus;
  reason?: string;
}

export interface BatchSaveReport {
  completedAt: number;
  outcomes: BatchItemOutcome[];
  counts: {
    saved: number;
    pending: number;
    restartRequired: number;
    failed: number;
    conflict: number;
    skipped: number;
  };
}

export interface BatchSnapshotItem {
  id: string;
  field: ConfigField;
  draft: Json;
  generation: number;
}

const CROSS_FIELD_PAIRS: readonly [string, string][] = [
  ["star_aid_min_interval_minutes", "star_aid_max_interval_minutes"],
  ["code_chunk_overlap", "code_chunk_size"],
  [
    "pr_issue_related_confidence_threshold",
    "pr_issue_closing_confidence_threshold",
  ],
];

export function fieldRefreshScope(field: ConfigField): string {
  if (field.id.startsWith("model/")) {
    const parts = field.id.split("/");
    if (parts.length >= 3)
      return `model:${parts[1]}:${decodeURIComponent(parts[2])}`;
  }
  return `category:${field.category}`;
}

function writePriority(item: BatchSnapshotItem): number {
  const key = item.field.key;
  if (
    item.field.type === "boolean" &&
    (key.endsWith("_enabled") || key.startsWith("enable_")) &&
    item.draft === false
  )
    return 0;
  if (item.field.sensitive) return 1;
  if (
    item.field.type === "boolean" &&
    (key.endsWith("_enabled") || key.startsWith("enable_")) &&
    item.draft === true
  )
    return 3;
  return 2;
}

export function planBatchExecution(
  snapshots: BatchSnapshotItem[],
  entries: ReadonlyMap<string, Entry>,
  unavailablePrefixes: ReadonlySet<string>,
): {
  executable: BatchSnapshotItem[];
  immediate: BatchItemOutcome[];
} {
  const immediate: BatchItemOutcome[] = [];
  const candidates: BatchSnapshotItem[] = [];

  const wholeSectionKeys = new Set(
    snapshots
      .filter(
        (item) =>
          item.field.id.startsWith("section/") && item.field.path.length === 0,
      )
      .map((item) => item.field.key),
  );

  for (const item of snapshots) {
    const entry = entries.get(item.id);
    if (!entry) continue;
    if (!entry.field.writable) {
      immediate.push({
        id: item.id,
        category: item.field.category,
        group: item.field.group,
        label: item.field.label,
        sensitive: item.field.sensitive,
        status: "skipped",
        reason: "deployment_owned",
      });
      continue;
    }
    if ([...unavailablePrefixes].some((prefix) => item.id.startsWith(prefix))) {
      immediate.push({
        id: item.id,
        category: item.field.category,
        group: item.field.group,
        label: item.field.label,
        sensitive: item.field.sensitive,
        status: "skipped",
        reason: "model_unavailable",
      });
      continue;
    }
    if (entry.conflict) {
      immediate.push({
        id: item.id,
        category: item.field.category,
        group: item.field.group,
        label: item.field.label,
        sensitive: item.field.sensitive,
        status: "conflict",
        reason: "conflict",
      });
      continue;
    }
    const invalid = validateValue(entry.field, item.draft);
    if (invalid) {
      entry.error = invalid;
      entry.state = "failed";
      immediate.push({
        id: item.id,
        category: item.field.category,
        group: item.field.group,
        label: item.field.label,
        sensitive: item.field.sensitive,
        status: "failed",
        reason: invalid,
      });
      continue;
    }
    if (
      item.field.id.startsWith("section/") &&
      item.field.path.length > 0 &&
      wholeSectionKeys.has(item.field.key)
    ) {
      immediate.push({
        id: item.id,
        category: item.field.category,
        group: item.field.group,
        label: item.field.label,
        sensitive: item.field.sensitive,
        status: "skipped",
        reason: "section_overlap",
      });
      continue;
    }
    candidates.push(item);
  }

  const ordered = [...candidates].sort((a, b) => {
    const scopeCompare = fieldRefreshScope(a.field).localeCompare(
      fieldRefreshScope(b.field),
    );
    if (scopeCompare !== 0) return scopeCompare;
    const priorityDiff = writePriority(a) - writePriority(b);
    if (priorityDiff !== 0) return priorityDiff;
    return a.field.key.localeCompare(b.field.key);
  });

  // Enforce safe ordering for bounded (lower <= upper) cross-field pairs.
  for (const [lowerKey, upperKey] of CROSS_FIELD_PAIRS) {
    const lowerIdx = ordered.findIndex((item) => item.field.key === lowerKey);
    const upperIdx = ordered.findIndex((item) => item.field.key === upperKey);
    if (lowerIdx === -1 || upperIdx === -1) continue;
    const lowerItem = ordered[lowerIdx];
    const upperEntry = entries.get(ordered[upperIdx].id);
    const currentUpper =
      typeof upperEntry?.field.value === "number"
        ? upperEntry.field.value
        : Infinity;
    const desiredLower =
      typeof lowerItem.draft === "number" ? lowerItem.draft : 0;
    const lowerShouldGoFirst = desiredLower <= currentUpper;
    if (lowerShouldGoFirst && lowerIdx > upperIdx) {
      const [moved] = ordered.splice(lowerIdx, 1);
      ordered.splice(upperIdx, 0, moved);
    } else if (!lowerShouldGoFirst && upperIdx > lowerIdx) {
      const [moved] = ordered.splice(upperIdx, 1);
      ordered.splice(lowerIdx, 0, moved);
    }
  }

  return { executable: ordered, immediate };
}

function buildReport(outcomes: BatchItemOutcome[]): BatchSaveReport {
  return {
    completedAt: Date.now(),
    outcomes,
    counts: {
      saved: outcomes.filter((item) => item.status === "saved").length,
      pending: outcomes.filter((item) => item.status === "pending").length,
      restartRequired: outcomes.filter(
        (item) => item.status === "restart_required",
      ).length,
      failed: outcomes.filter((item) => item.status === "failed").length,
      conflict: outcomes.filter((item) => item.status === "conflict").length,
      skipped: outcomes.filter((item) => item.status === "skipped").length,
    },
  };
}

export class SettingsStore {
  entries = new Map<string, Entry>();
  groups = new Map<string, string>();
  messages = new Map<string, string>();
  batchSaving = false;
  batchProgress: { completed: number; total: number } | null = null;
  lastBatchReport: BatchSaveReport | null = null;
  private listeners = new Set<() => void>();
  private revision = 0;
  private sessionEpoch = 0;
  private queue: Promise<unknown> = Promise.resolve();
  private externalDirty = new Set<string>();
  private unavailablePrefixes = new Set<string>();

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  snapshot = () => this.revision;
  private emit() {
    this.revision++;
    this.listeners.forEach((listener) => listener());
  }
  get dirty() {
    return (
      this.batchSaving ||
      this.hasExternalDrafts ||
      [...this.entries.values()].some((entry) => isDirty(entry) || entry.saving)
    );
  }
  get hasExternalDrafts() {
    return this.externalDirty.size > 0;
  }
  get dirtyCount() {
    return [...this.entries.values()].filter(isDirty).length;
  }
  get dirtyEntries(): Entry[] {
    return [...this.entries.values()].filter(isDirty);
  }
  get retryableIds(): Set<string> {
    const failed = new Set(
      this.lastBatchReport?.outcomes
        .filter(({ status }) => status === "failed" || status === "skipped")
        .map(({ id }) => id),
    );
    return new Set(
      this.dirtyEntries
        .filter(
          ({ field, conflict }) =>
            failed.has(field.id) &&
            field.writable &&
            !conflict &&
            !this.isFieldUnavailable(field.id),
        )
        .map(({ field }) => field.id),
    );
  }
  get savingAny() {
    return (
      this.batchSaving ||
      [...this.entries.values()].some((entry) => entry.saving)
    );
  }
  recordCatalogMetadata(
    groups?: Record<string, string>,
    messages?: Record<string, string>,
  ) {
    if (groups) {
      for (const [key, value] of Object.entries(groups))
        this.groups.set(key, value);
    }
    if (messages) {
      for (const [key, value] of Object.entries(messages))
        this.messages.set(key, value);
    }
  }
  message(code: string): string {
    return this.messages.get(code) ?? code;
  }
  setExternalDirty(key: string, dirty: boolean) {
    if (this.externalDirty.has(key) === dirty) return;
    if (dirty) this.externalDirty.add(key);
    else this.externalDirty.delete(key);
    this.emit();
  }
  setPrefixUnavailable(prefix: string, unavailable: boolean) {
    if (this.unavailablePrefixes.has(prefix) === unavailable) return;
    if (unavailable) this.unavailablePrefixes.add(prefix);
    else this.unavailablePrefixes.delete(prefix);
    this.emit();
  }
  isFieldUnavailable(id: string): boolean {
    for (const prefix of this.unavailablePrefixes) {
      if (id.startsWith(prefix)) return true;
    }
    return false;
  }
  clearBatchReport() {
    if (!this.lastBatchReport) return;
    this.lastBatchReport = null;
    this.emit();
  }
  forget(prefix: string) {
    for (const key of this.entries.keys())
      if (key.startsWith(prefix)) this.entries.delete(key);
    this.unavailablePrefixes.delete(prefix);
    this.emit();
  }
  clearAll() {
    this.sessionEpoch++;
    this.entries.clear();
    this.groups.clear();
    this.messages.clear();
    this.externalDirty.clear();
    this.unavailablePrefixes.clear();
    this.batchSaving = false;
    this.batchProgress = null;
    this.lastBatchReport = null;
    this.emit();
  }

  reconcile(fields: ConfigField[]) {
    for (const field of fields) {
      const entry = this.entries.get(field.id);
      if (!entry) {
        this.entries.set(field.id, {
          field,
          draft: field.value,
          generation: 0,
          saving: false,
          conflict: false,
          state: field.effect === "live" ? "unchanged" : field.effect,
        });
      } else if (!entry.saving) {
        if (isDirty(entry) && changedBaseline(entry.field, field)) {
          entry.conflict = true;
          entry.error = "conflict";
          entry.state = "failed";
          // Keep the original baseline and draft until the user explicitly reloads.
        } else {
          const dirty = isDirty(entry);
          entry.field = field;
          if (!dirty) {
            entry.draft = field.value;
            if (field.effect !== "live") entry.state = field.effect;
            else if (entry.state === "pending") entry.state = "saved";
          }
        }
      } else if (!changedBaseline(entry.field, field)) {
        entry.field = { ...entry.field, version: field.version };
      }
    }
    this.emit();
  }

  edit(id: string, draft: Json) {
    const entry = this.entries.get(id);
    if (!entry) return;
    entry.draft = draft;
    entry.generation++;
    if (!entry.conflict) entry.error = undefined;
    entry.state = entry.conflict
      ? "failed"
      : isDirty(entry)
        ? "unsaved"
        : entry.field.effect === "live"
          ? "unchanged"
          : entry.field.effect;
    this.emit();
  }

  revert(id: string) {
    const entry = this.entries.get(id);
    if (!entry || entry.saving) return;
    entry.draft = entry.field.value;
    entry.generation++;
    entry.error = undefined;
    entry.conflict = false;
    entry.state =
      entry.field.effect === "live" ? "unchanged" : entry.field.effect;
    this.emit();
  }

  accept(field: ConfigField) {
    const entry = this.entries.get(field.id);
    this.entries.set(field.id, {
      field,
      draft: field.value,
      generation: (entry?.generation ?? 0) + 1,
      saving: false,
      conflict: false,
      state: field.effect === "live" ? "unchanged" : field.effect,
    });
    this.emit();
  }

  discardAll() {
    for (const entry of this.entries.values()) {
      if (entry.saving) continue;
      entry.draft = entry.field.value;
      entry.generation++;
      entry.error = undefined;
      entry.conflict = false;
      entry.state =
        entry.field.effect === "live" ? "unchanged" : entry.field.effect;
    }
    this.lastBatchReport = null;
    this.emit();
  }

  async save(
    id: string,
    mutation: (
      field: ConfigField,
      value: Json,
      reset: boolean,
    ) => Promise<ConfigField>,
    refresh: () => Promise<ConfigField[]>,
    reset = false,
  ) {
    const entry = this.entries.get(id);
    if (
      !entry ||
      entry.saving ||
      this.batchSaving ||
      entry.conflict ||
      !entry.field.writable ||
      (!reset && !isDirty(entry))
    )
      return;
    const invalid = !reset && validateValue(entry.field, entry.draft);
    if (invalid) {
      entry.error = invalid;
      entry.state = "failed";
      this.emit();
      return;
    }
    const epoch = this.sessionEpoch;
    const generation = entry.generation;
    const draft = structuredClone(entry.draft);
    entry.saving = true;
    entry.state = "saving";
    entry.error = undefined;
    this.emit();
    const run = async () => {
      if (this.sessionEpoch !== epoch) return;
      try {
        // An earlier queued mutation may have changed this field, e.g. a whole-map reset.
        if (entry.conflict)
          throw { status: 409, body: { error: { code: "conflict" } } };
        const response = await mutation(entry.field, draft, reset);
        if (this.sessionEpoch !== epoch) return;
        entry.field = response;
        if (entry.generation === generation) entry.draft = response.value;
        entry.state = isDirty(entry)
          ? "unsaved"
          : response.effect === "live"
            ? "saved"
            : response.effect;
        entry.saving = false;
        this.emit();
        // The committed write is successful even if this refresh fails. Never replay it.
        try {
          const fresh = await refresh();
          if (this.sessionEpoch === epoch) this.reconcile(fresh);
        } catch {
          /* Next focus/retry refreshes versions. */
        }
      } catch (error) {
        if (this.sessionEpoch !== epoch) return;
        const failure = error as {
          status?: number;
          body?: { error?: { code?: string } };
        };
        entry.conflict = failure.status === 409;
        entry.error =
          failure.body?.error?.code ??
          (entry.conflict ? "conflict" : "network");
        entry.state = "failed";
      } finally {
        if (this.sessionEpoch === epoch) {
          entry.saving = false;
          this.emit();
        }
      }
    };
    const result = this.queue.then(run, run);
    this.queue = result.catch(() => undefined);
    await result;
  }

  captureBatch(onlyIds?: ReadonlySet<string>): BatchSnapshotItem[] {
    return this.dirtyEntries
      .filter(
        (entry) => !entry.saving && (!onlyIds || onlyIds.has(entry.field.id)),
      )
      .map((entry) => ({
        id: entry.field.id,
        field: entry.field,
        draft: structuredClone(entry.draft),
        generation: entry.generation,
      }));
  }

  async saveBatch({
    snapshots = this.captureBatch(),
    mutateField,
    refreshScope,
  }: {
    snapshots?: BatchSnapshotItem[];
    mutateField: (field: ConfigField, value: Json) => Promise<ConfigField>;
    refreshScope: (scope: string) => Promise<ConfigField[]>;
  }): Promise<BatchSaveReport | null> {
    if (this.savingAny || !snapshots.length) return null;

    const epoch = this.sessionEpoch;
    const { executable, immediate } = planBatchExecution(
      snapshots,
      this.entries,
      this.unavailablePrefixes,
    );
    const outcomes: BatchItemOutcome[] = [...immediate];

    if (!executable.length) {
      const report = buildReport(outcomes);
      this.lastBatchReport = report;
      this.emit();
      return report;
    }

    this.batchSaving = true;
    this.batchProgress = { completed: 0, total: executable.length };
    this.lastBatchReport = null;
    this.emit();

    const run = async (): Promise<BatchSaveReport | null> => {
      if (this.sessionEpoch !== epoch) return null;
      const mutatedScopes = new Set<string>();
      const needsRebaseBeforeNext = new Set<string>();

      for (let index = 0; index < executable.length; index++) {
        if (this.sessionEpoch !== epoch) return null;
        const snap = executable[index];
        const entry = this.entries.get(snap.id);
        if (!entry) continue;
        const scope = fieldRefreshScope(entry.field);

        if (needsRebaseBeforeNext.has(scope)) {
          try {
            const rebased = await refreshScope(scope);
            if (this.sessionEpoch !== epoch) return null;
            this.reconcile(rebased);
            needsRebaseBeforeNext.delete(scope);
          } catch {
            /* Proceed with best known version; server 409 protects against stale overwrite. */
          }
        }

        const unavailable = this.isFieldUnavailable(snap.id);
        const conflict =
          entry.conflict || changedBaseline(snap.field, entry.field);
        if (conflict || !entry.field.writable || unavailable) {
          outcomes.push({
            id: snap.id,
            category: entry.field.category,
            group: entry.field.group,
            label: entry.field.label,
            sensitive: entry.field.sensitive,
            status: conflict ? "conflict" : "skipped",
            reason: conflict
              ? "conflict"
              : unavailable
                ? "model_unavailable"
                : "deployment_owned",
          });
          this.batchProgress = {
            completed: index + 1,
            total: executable.length,
          };
          this.emit();
          continue;
        }

        entry.saving = true;
        entry.state = "saving";
        entry.error = undefined;
        this.emit();

        try {
          const response = await mutateField(entry.field, snap.draft);
          if (this.sessionEpoch !== epoch) return null;
          entry.field = response;
          if (entry.generation === snap.generation)
            entry.draft = response.value;
          entry.saving = false;
          entry.state = isDirty(entry)
            ? "unsaved"
            : response.effect === "live"
              ? "saved"
              : response.effect;
          mutatedScopes.add(scope);
          needsRebaseBeforeNext.add(scope);
          outcomes.push({
            id: snap.id,
            category: response.category,
            group: response.group,
            label: response.label,
            sensitive: response.sensitive,
            status:
              response.effect === "pending"
                ? "pending"
                : response.effect === "restart_required"
                  ? "restart_required"
                  : "saved",
          });
        } catch (error) {
          if (this.sessionEpoch !== epoch) return null;
          const failure = error as {
            status?: number;
            body?: { error?: { code?: string } };
          };
          entry.saving = false;
          entry.conflict = failure.status === 409;
          const code =
            failure.body?.error?.code ??
            (entry.conflict ? "conflict" : "network");
          entry.error = code;
          entry.state = "failed";
          outcomes.push({
            id: snap.id,
            category: entry.field.category,
            group: entry.field.group,
            label: entry.field.label,
            sensitive: entry.field.sensitive,
            status: entry.conflict ? "conflict" : "failed",
            reason: code,
          });
        }

        this.batchProgress = { completed: index + 1, total: executable.length };
        this.emit();
      }

      for (const scope of mutatedScopes) {
        if (this.sessionEpoch !== epoch) return null;
        try {
          const fresh = await refreshScope(scope);
          if (this.sessionEpoch === epoch) this.reconcile(fresh);
        } catch {
          /* Individual commits already succeeded; next focus/retry refreshes versions. */
        }
      }

      if (this.sessionEpoch !== epoch) return null;
      const report = buildReport(outcomes);
      this.batchSaving = false;
      this.batchProgress = null;
      this.lastBatchReport = report;
      this.emit();
      return report;
    };

    const pending = this.queue.then(run, run);
    this.queue = pending.catch(() => null);
    return pending;
  }
}
