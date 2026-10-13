import { test } from "node:test";
import assert from "node:assert/strict";
import { FieldController } from "../../backend/webui/assets/settings/field-controller.js";
import { SettingsClient } from "../../backend/webui/assets/settings/client.js";

const field = {
  id: "global/test",
  value: "old",
  version: "v1",
  resettable: true,
  overridden: true,
  effect: "live",
};
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((a, b) => {
    resolve = a;
    reject = b;
  });
  return { promise, resolve, reject };
};
test("debounce coalesces edits and IME never saves intermediate composition", async () => {
  let scheduled,
    calls = 0;
  const c = new FieldController(
    { ...field },
    {
      schedule: (fn) => {
        scheduled = fn;
        return 1;
      },
      cancel: () => {
        scheduled = null;
      },
      save: async (payload) => {
        calls++;
        return { ...field, value: payload().value, version: "v2" };
      },
    },
  );
  c.composition(true);
  c.edit("中");
  assert.equal(scheduled, null);
  assert.equal(calls, 0);
  c.edit("中文");
  c.composition(false);
  assert.ok(scheduled);
  await scheduled();
  assert.equal(calls, 1);
  assert.equal(c.saved, "中文");
});
test("rapid edits serialize requests and an old reply cannot overwrite the latest draft", async () => {
  const first = deferred(),
    second = deferred();
  const payloads = [];
  const c = new FieldController(
    { ...field },
    {
      save: (payload) => {
        payloads.push(payload());
        return payloads.length === 1 ? first.promise : second.promise;
      },
    },
  );
  c.edit("first", true);
  c.edit("second", true);
  assert.equal(payloads.length, 1);
  first.resolve({ ...field, value: "first", version: "v2" });
  await new Promise(setImmediate);
  assert.equal(c.draft, "second");
  assert.equal(payloads.length, 2);
  assert.equal(payloads[1].version, "v2");
  second.resolve({ ...field, value: "second", version: "v3" });
  await new Promise(setImmediate);
  assert.equal(c.saved, "second");
  assert.equal(c.state, "saved");
});
test("failed requests retain drafts and explicit retry uses the same version", async () => {
  let calls = 0;
  const c = new FieldController(
    { ...field },
    {
      save: async (payload) => {
        if (!calls++) throw { code: "network" };
        return { ...field, value: payload().value, version: "v2" };
      },
    },
  );
  c.edit("draft");
  await c.flush();
  assert.equal(c.state, "failed");
  assert.equal(c.draft, "draft");
  await c.retry();
  assert.equal(c.state, "saved");
});
test("conflict does not silently rebase a changed server field", async () => {
  let calls = 0;
  const c = new FieldController(
    { ...field },
    {
      save: async () => {
        calls++;
        throw { status: 409, code: "conflict" };
      },
    },
  );
  c.edit("draft");
  await c.flush();
  c.reconcile({ ...field, value: "remote", version: "v2" });
  await c.retry();
  assert.equal(calls, 1);
  assert.equal(c.draft, "draft");
  assert.equal(c.conflict, true);
  c.acceptServer({ ...field, value: "remote", version: "v2" });
  assert.equal(c.dirty, false);
});
test("reset persists even if effective value equals its default", async () => {
  let reset = false;
  const c = new FieldController(
    { ...field },
    {
      save: async (payload, mode) => {
        reset = mode;
        return {
          ...field,
          value: "inherited",
          source: "environment",
          overridden: false,
        };
      },
    },
  );
  await c.reset();
  assert.equal(reset, true);
  assert.equal(c.saved, "inherited");
  assert.equal(c.dirty, false);
});
test("credentials require confirmation and never autosave while typing", async () => {
  let calls = 0;
  const c = new FieldController(
    { ...field, value: null, sensitive: true },
    {
      save: async () => {
        calls++;
        return { ...field, value: null, sensitive: true };
      },
    },
  );
  c.edit("credential", true);
  await c.flush();
  assert.equal(calls, 0);
  await c.flush({ confirmed: true });
  assert.equal(calls, 1);
  assert.equal(c.draft, null);
});
test("shared client serializes sibling writes without losing errors", async () => {
  const c = new SettingsClient("csrf", () => {}),
    d = deferred(),
    events = [];
  const a = c.serialize(async () => {
    events.push(1);
    await d.promise;
    throw new Error("failure");
  });
  const b = c.serialize(() => {
    events.push(2);
    return 2;
  });
  await new Promise(setImmediate);
  assert.deepEqual(events, [1]);
  d.resolve();
  await assert.rejects(a);
  assert.equal(await b, 2);
});

test("typing while reset is in flight saves the new draft after reset", async () => {
  const pending = deferred(),
    calls = [];
  const c = new FieldController(
    { ...field },
    {
      save: async (payload, reset) => {
        calls.push({ payload: payload(), reset });
        if (calls.length === 1) return pending.promise;
        return { ...field, value: payload().value, version: "v3" };
      },
    },
  );
  const resetting = c.reset();
  c.edit("new draft", true);
  pending.resolve({ ...field, value: "default", version: "v2" });
  await resetting;
  await new Promise(setImmediate);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].reset, true);
  assert.equal(calls[1].reset, false);
  assert.equal(c.saved, "new draft");
});

test("object formatting alone does not create an unsaved change", () => {
  const c = new FieldController(
    { ...field, value: { a: 1, b: 2 } },
    {
      save: () => {
        throw new Error("not called");
      },
    },
  );
  c.edit({ b: 2, a: 1 });
  assert.equal(c.dirty, false);
  c.cancel(c.timer);
});
