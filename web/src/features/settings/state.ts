import type { ConfigField, Json, Schema } from "./types";

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
export class SettingsStore {
  entries = new Map<string, Entry>();
  private listeners = new Set<() => void>();
  private revision = 0;
  private queue: Promise<unknown> = Promise.resolve();
  private externalDirty = new Set<string>();
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
      this.externalDirty.size > 0 ||
      [...this.entries.values()].some((entry) => isDirty(entry) || entry.saving)
    );
  }
  get dirtyCount() {
    return [...this.entries.values()].filter(isDirty).length;
  }
  setExternalDirty(key: string, dirty: boolean) {
    if (this.externalDirty.has(key) === dirty) return;
    if (dirty) this.externalDirty.add(key);
    else this.externalDirty.delete(key);
    this.emit();
  }
  forget(prefix: string) {
    for (const key of this.entries.keys())
      if (key.startsWith(prefix)) this.entries.delete(key);
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
        if (isDirty(entry) && !same(entry.field.value, field.value)) {
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
      } else if (same(entry.field.value, field.value)) {
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
        : "unchanged";
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
      state: "unchanged",
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
      entry.state = "unchanged";
    }
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
    entry.saving = true;
    entry.state = "saving";
    entry.error = undefined;
    this.emit();
    const run = async () => {
      const generation = entry.generation;
      try {
        // An earlier queued mutation may have changed this field, e.g. a whole-map reset.
        if (entry.conflict)
          throw { status: 409, body: { error: { code: "conflict" } } };
        const response = await mutation(entry.field, entry.draft, reset);
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
          this.reconcile(await refresh());
        } catch {
          /* Next focus/retry refreshes versions. */
        }
      } catch (error) {
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
        entry.saving = false;
        this.emit();
      }
    };
    const result = this.queue.then(run, run);
    this.queue = result.catch(() => undefined);
    await result;
  }
}
