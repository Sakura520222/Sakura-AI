import { describe, expect, it, vi } from "vitest";
import type { ConfigField, Json } from "./types";
import {
  initialValue,
  isDirty,
  planBatchExecution,
  SettingsStore,
  validateValue,
} from "./state";

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
  it("keeps a credential draft in conflict when another window rotates the masked credential", async () => {
    const store = new SettingsStore();
    const item = {
      ...field("global/credential", null),
      type: "string" as const,
      sensitive: true,
      constraints: {},
    };
    store.reconcile([item]);
    store.edit(item.id, "fixture-only-draft");
    store.reconcile([{ ...item, version: "b".repeat(64) }]);
    const mutateField = vi.fn();
    const report = await store.saveBatch({
      mutateField,
      refreshScope: async () => [],
    });
    expect(mutateField).not.toHaveBeenCalled();
    expect(report?.counts.conflict).toBe(1);
    expect(store.entries.get(item.id)?.draft).toBe("fixture-only-draft");
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

describe("SettingsStore batch planning, snapshot isolation, and session fencing", () => {
  it("orders cross-field bounds safely when growing or shrinking and orders credentials before enable toggles", () => {
    const store = new SettingsStore();
    const overlap = {
      ...field("global/code_chunk_overlap", 100),
      key: "code_chunk_overlap",
      category: "review" as const,
      constraints: { ge: 0, le: 5000 },
    };
    const size = {
      ...field("global/code_chunk_size", 500),
      key: "code_chunk_size",
      category: "review" as const,
      constraints: { ge: 100, le: 10000 },
    };
    const secret = {
      ...field("global/stripe_secret_key", ""),
      key: "stripe_secret_key",
      category: "system" as const,
      type: "string" as const,
      sensitive: true,
      constraints: {},
    };
    const toggle = {
      ...field("global/stripe_enabled", false),
      key: "stripe_enabled",
      category: "system" as const,
      type: "boolean" as const,
      constraints: {},
    };
    store.reconcile([overlap, size, secret, toggle]);

    // Growing both: desired overlap 800 > current size 500 -> size must execute first
    store.edit(overlap.id, 800);
    store.edit(size.id, 2000);
    store.edit(secret.id, "sk_test_fixture_only");
    store.edit(toggle.id, true);
    const growingPlan = planBatchExecution(
      store.dirtyEntries.map((entry) => ({
        id: entry.field.id,
        field: entry.field,
        draft: entry.draft,
        generation: entry.generation,
      })),
      store.entries,
      new Set(),
    );
    const growingOrder = growingPlan.executable.map((item) => item.field.key);
    expect(growingOrder.indexOf("code_chunk_size")).toBeLessThan(
      growingOrder.indexOf("code_chunk_overlap"),
    );
    expect(growingOrder.indexOf("stripe_secret_key")).toBeLessThan(
      growingOrder.indexOf("stripe_enabled"),
    );

    // Shrinking both: desired overlap 50 <= current size 500 -> overlap must execute first
    store.edit(overlap.id, 50);
    store.edit(size.id, 200);
    const shrinkingPlan = planBatchExecution(
      store.dirtyEntries.map((entry) => ({
        id: entry.field.id,
        field: entry.field,
        draft: entry.draft,
        generation: entry.generation,
      })),
      store.entries,
      new Set(),
    );
    const shrinkingOrder = shrinkingPlan.executable.map(
      (item) => item.field.key,
    );
    expect(shrinkingOrder.indexOf("code_chunk_overlap")).toBeLessThan(
      shrinkingOrder.indexOf("code_chunk_size"),
    );
  });

  it("skips leaf edits that overlap a whole-section edit and skips unavailable model prefixes", async () => {
    const store = new SettingsStore();
    const whole = {
      ...field("section/label.definitions", { bug: { color: "ff0000" } }),
      key: "label.definitions",
      path: [],
      category: "labels" as const,
      type: "object" as const,
      constraints: {},
    };
    const leaf = {
      ...field("section/label.definitions/bug", { color: "ff0000" }),
      key: "label.definitions",
      path: ["bug"],
      category: "labels" as const,
      type: "object" as const,
      constraints: {},
    };
    const modelField = {
      ...field("model/openai/gpt-4.1/temperature", 0.2),
      key: "temperature",
      type: "number" as const,
      constraints: { ge: 0, le: 2 },
    };
    store.reconcile([whole, leaf, modelField]);
    store.edit(whole.id, { bug: { color: "00ff00" } });
    store.edit(leaf.id, { color: "0000ff" });
    store.edit(modelField.id, 0.5);
    store.setPrefixUnavailable("model/openai/gpt-4.1/", true);

    const mutateField = vi.fn(async (current: ConfigField, value: Json) => ({
      ...current,
      value,
      version: "b".repeat(64),
    }));
    const report = await store.saveBatch({
      mutateField,
      refreshScope: async () => [
        {
          ...whole,
          value: { bug: { color: "00ff00" } },
          version: "b".repeat(64),
        },
      ],
    });
    expect(mutateField).toHaveBeenCalledTimes(1);
    expect(mutateField.mock.calls[0]?.[0].id).toBe(whole.id);
    expect(report?.counts).toEqual({
      saved: 1,
      pending: 0,
      restartRequired: 0,
      failed: 0,
      conflict: 0,
      skipped: 2,
    });
    expect(isDirty(store.entries.get(leaf.id)!)).toBe(true);
    expect(isDirty(store.entries.get(modelField.id)!)).toBe(true);
  });

  it("rebases sibling versions in saveBatch, isolates concurrent edits, and retries only unsaved items", async () => {
    const store = new SettingsStore();
    const theme = {
      ...field("personal/theme", "light"),
      key: "theme",
      category: "personal" as const,
      type: "string" as const,
      constraints: {},
    };
    const perPage = {
      ...field("personal/items_per_page", 20),
      key: "items_per_page",
      category: "personal" as const,
      constraints: { ge: 10, le: 100 },
    };
    const failing = {
      ...field("global/max_concurrent_reviews", 2),
      key: "max_concurrent_reviews",
      category: "review" as const,
      constraints: { ge: 1, le: 20 },
    };
    const restart = {
      ...field("global/app_port", 8000),
      key: "app_port",
      category: "system" as const,
      requiresRestart: true,
      effect: "restart_required" as const,
      constraints: { ge: 1, le: 65535 },
    };
    store.reconcile([theme, perPage, failing, restart]);
    store.edit(theme.id, "dark");
    store.edit(perPage.id, 50);
    store.edit(failing.id, 6);
    store.edit(restart.id, 9000);

    let failReview = true;
    const serverPersonal: Record<string, Json> = {
      [theme.id]: "light",
      [perPage.id]: 20,
    };
    let personalVersion = "a".repeat(64);
    const versionsSeen: Record<string, string> = {};
    const mutateField = vi.fn(async (current: ConfigField, value: Json) => {
      versionsSeen[current.id] = current.version;
      if (current.id === perPage.id) {
        // User types a newer value on perPage while batch save is mid-flight!
        store.edit(perPage.id, 80);
      }
      if (current.id === failing.id && failReview) {
        throw { status: 503, body: { error: { code: "network" } } };
      }
      if (current.category === "personal") {
        serverPersonal[current.id] = value;
        personalVersion =
          personalVersion === "a".repeat(64) ? "b".repeat(64) : "c".repeat(64);
        return { ...current, value, version: personalVersion };
      }
      return {
        ...current,
        value,
        version: "b".repeat(64),
      };
    });
    const refreshScope = vi.fn(async (scope: string) => {
      if (scope === "category:personal") {
        return [
          {
            ...theme,
            value: serverPersonal[theme.id],
            version: personalVersion,
          },
          {
            ...perPage,
            value: serverPersonal[perPage.id],
            version: personalVersion,
          },
        ];
      }
      if (scope === "category:system") {
        return [{ ...restart, value: 9000, version: "b".repeat(64) }];
      }
      return [failing];
    });

    const firstReport = await store.saveBatch({ mutateField, refreshScope });
    // Second personal field saw rebased version "bbb..." after first personal field mutated!
    expect(versionsSeen[perPage.id]).toBe("a".repeat(64));
    expect(versionsSeen[theme.id]).toBe("b".repeat(64));
    expect(firstReport?.counts).toEqual({
      saved: 2,
      pending: 0,
      restartRequired: 1,
      failed: 1,
      conflict: 0,
      skipped: 0,
    });
    // Newer draft (80) typed during save was NOT overwritten by earlier snapshot response (50)
    expect(store.entries.get(perPage.id)?.draft).toBe(80);
    expect(store.entries.get(failing.id)?.draft).toBe(6);
    expect(store.dirtyCount).toBe(2);

    // Retry the rejected request while retaining the newer, unsent edit.
    failReview = false;
    mutateField.mockClear();
    expect(store.retryableIds).toEqual(new Set([failing.id]));
    const secondReport = await store.saveBatch({
      snapshots: store.captureBatch(store.retryableIds),
      mutateField,
      refreshScope: async () => [
        { ...theme, value: "dark", version: "c".repeat(64) },
        { ...perPage, value: 80, version: "c".repeat(64) },
        { ...failing, value: 6, version: "c".repeat(64) },
      ],
    });
    expect(mutateField).toHaveBeenCalledTimes(1);
    expect(secondReport?.counts.saved).toBe(1);
    expect(store.dirtyCount).toBe(1);
    expect(store.entries.get(perPage.id)?.draft).toBe(80);
  });

  it("submits the captured values once even when edits continue before the save starts", async () => {
    const store = new SettingsStore();
    const item = field();
    store.reconcile([item]);
    store.edit(item.id, 4);
    const snapshots = store.captureBatch();
    store.edit(item.id, 9);
    const mutateField = vi.fn(async (current: ConfigField, value: Json) => ({
      ...current,
      value,
    }));
    const options = {
      snapshots,
      mutateField,
      refreshScope: async () => [{ ...item, value: 4 }],
    };
    const [first, duplicate] = await Promise.all([
      store.saveBatch(options),
      store.saveBatch(options),
    ]);
    expect(first?.counts.saved).toBe(1);
    expect(duplicate).toBeNull();
    expect(mutateField).toHaveBeenCalledExactlyOnceWith(item, 4);
    expect(store.entries.get(item.id)?.draft).toBe(9);
  });

  it("fences in-flight batch saves when clearAll runs on logout or identity switch", async () => {
    const store = new SettingsStore();
    const item = field("global/retries", 2);
    store.reconcile([item]);
    store.edit(item.id, 9);

    let resolveMutation!: (value: ConfigField) => void;
    const pending = store.saveBatch({
      mutateField: () =>
        new Promise((done) => {
          resolveMutation = done;
        }),
      refreshScope: async () => [{ ...item, value: 9 }],
    });
    await Promise.resolve();
    store.clearAll();
    resolveMutation({ ...item, value: 9 });
    const report = await pending;
    expect(report).toBeNull();
    expect(store.entries.size).toBe(0);
    expect(store.lastBatchReport).toBeNull();
  });
});
