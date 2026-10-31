import { describe, expect, it, vi } from "vitest";
import type { ConfigField, Json } from "./types";
import { initialValue, isDirty, SettingsStore, validateValue } from "./state";

function field(id = "global/retries", value: Json = 2): ConfigField {
  return {
    id,
    key: "retries",
    path: [],
    category: "ai",
    group: "ai_strategy",
    type: "integer",
    label: "Retries",
    description: "",
    editorSchema: null,
    optionLabels: {},
    scope: "global",
    permission: "super_admin",
    sensitive: false,
    writable: true,
    resettable: true,
    requiresRestart: false,
    advanced: false,
    constraints: { ge: 0, le: 20 },
    value,
    default: 2,
    inheritedValue: 2,
    source: "database",
    version: "a".repeat(64),
    hasValue: true,
    overridden: true,
    effect: "live",
    submission: "automatic",
  };
}

describe("typed settings values", () => {
  it("keeps zero, false and explicitly empty strings while rejecting incomplete numeric input", () => {
    expect(validateValue(field(), 0)).toBeUndefined();
    expect(validateValue(field(), "")).toBe("invalid_type");
    expect(validateValue(field(), 1.5)).toBe("invalid_type");
    expect(validateValue(field(), Infinity)).toBe("invalid_type");
    expect(
      validateValue({ ...field(), type: "boolean", constraints: {} }, false),
    ).toBeUndefined();
    expect(
      validateValue({ ...field(), type: "string", constraints: {} }, ""),
    ).toBeUndefined();
  });
  it("rejects secret masks and blank values instead of saving them", () => {
    const secret = {
      ...field(),
      type: "string" as const,
      sensitive: true,
      constraints: {},
    };
    for (const value of ["", "  ", "****", "[REDACTED]"])
      expect(validateValue(secret, value)).toBe("credential_required");
    expect(validateValue(secret, "new-secret-value")).toBeUndefined();
  });
  it("uses typed defaults and validates nested required schema fields", () => {
    const schema = {
      type: "object",
      properties: {
        enabled: { type: "boolean" },
        count: { type: "integer", minimum: 1 },
      },
      required: ["enabled", "count"],
    };
    expect(initialValue(schema)).toEqual({ enabled: false, count: 1 });
    const item = {
      ...field(),
      type: "object" as const,
      constraints: {},
      editorSchema: schema,
    };
    expect(validateValue(item, { enabled: false, count: 0 })).toBe(
      "out_of_range",
    );
    expect(
      validateValue(item, { enabled: false, count: 1, historical: "keep me" }),
    ).toBeUndefined();
    expect(validateValue(item, { enabled: false })).toBe("editor_incomplete");
  });
  it("refuses incomplete primary and fallback assignments", () => {
    const binding = {
      ...field("binding/main", {}),
      type: "object" as const,
      constraints: {},
    };
    expect(
      validateValue(binding, {
        primary: { account: "", model: "" },
        fallback: [],
      }),
    ).toBe("binding_incomplete");
    expect(
      validateValue(binding, {
        primary: { account: "one", model: "m" },
        fallback: [{ account: "two", model: "" }],
      }),
    ).toBe("binding_incomplete");
  });
});

describe("SettingsStore concurrency and saved feedback", () => {
  it("does not overwrite a dirty draft on a background refresh", () => {
    const store = new SettingsStore();
    store.reconcile([field()]);
    store.edit("global/retries", 0);
    store.reconcile([{ ...field(), value: 3, version: "b".repeat(64) }]);
    const entry = store.entries.get("global/retries")!;
    expect(entry.draft).toBe(0);
    expect(entry.conflict).toBe(true);
    expect(entry.field.value).toBe(2);
  });
  it("rebases sibling versions and serializes queued mutations", async () => {
    const store = new SettingsStore();
    const first = field("section/shared/a");
    const second = field("section/shared/b");
    store.reconcile([first, second]);
    store.edit(first.id, 4);
    store.edit(second.id, 5);
    const calls: string[] = [];
    const mutation = vi.fn(async (current: ConfigField, value: Json) => {
      calls.push(current.version);
      return { ...current, value, version: "b".repeat(64) };
    });
    await Promise.all([
      store.save(first.id, mutation, async () => [
        { ...first, value: 4, version: "b".repeat(64) },
        { ...second, version: "b".repeat(64) },
      ]),
      store.save(second.id, mutation, async () => [
        { ...first, value: 4, version: "b".repeat(64) },
        { ...second, value: 5, version: "b".repeat(64) },
      ]),
    ]);
    expect(calls).toEqual(["a".repeat(64), "b".repeat(64)]);
    expect(store.dirty).toBe(false);
  });
  it("keeps a newer draft typed while an earlier save is in flight", async () => {
    const store = new SettingsStore();
    const item = field();
    store.reconcile([item]);
    store.edit(item.id, 4);
    let resolve!: (field: ConfigField) => void;
    const pending = store.save(
      item.id,
      () =>
        new Promise((done) => {
          resolve = done;
        }),
      async () => [{ ...item, value: 4 }],
    );
    await Promise.resolve();
    store.edit(item.id, 5);
    resolve({ ...item, value: 4 });
    await pending;
    expect(store.entries.get(item.id)?.draft).toBe(5);
    expect(store.entries.get(item.id)?.state).toBe("unsaved");
  });
  it("does not retry a committed mutation when its refresh fails", async () => {
    const store = new SettingsStore();
    const item = field();
    store.reconcile([item]);
    store.edit(item.id, 0);
    const save = vi.fn(async () => ({ ...item, value: 0 }));
    await store.save(item.id, save, async () => {
      throw new Error("network");
    });
    expect(save).toHaveBeenCalledTimes(1);
    expect(store.entries.get(item.id)?.state).toBe("saved");
    expect(store.dirty).toBe(false);
  });
  it("preserves drafts on failure and requires explicit acceptance after conflict", async () => {
    const store = new SettingsStore();
    const item = field();
    store.reconcile([item]);
    store.edit(item.id, 4);
    const save = vi.fn(async () => {
      throw { status: 409, body: { error: { code: "conflict" } } };
    });
    await store.save(item.id, save, async () => []);
    await store.save(item.id, save, async () => []);
    expect(save).toHaveBeenCalledTimes(1);
    expect(store.entries.get(item.id)?.draft).toBe(4);
    store.accept({ ...item, value: 7 });
    expect(isDirty(store.entries.get(item.id)!)).toBe(false);
  });
  it("keeps persisted pending and restart states distinct from completed runtime application", async () => {
    const store = new SettingsStore();
    const item = field();
    store.reconcile([item]);
    store.edit(item.id, 4);
    await store.save(
      item.id,
      async () => ({ ...item, value: 4, effect: "pending" }),
      async () => [{ ...item, value: 4, effect: "pending" }],
    );
    expect(store.entries.get(item.id)?.state).toBe("pending");
    expect(store.dirty).toBe(false);
    store.reconcile([{ ...item, value: 4, effect: "live" }]);
    expect(store.entries.get(item.id)?.state).toBe("saved");
    store.reconcile([{ ...item, value: 4, effect: "restart_required" }]);
    expect(store.entries.get(item.id)?.state).toBe("restart_required");
  });
});
