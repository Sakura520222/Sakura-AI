import { test, expect } from "@playwright/test";

const row = (page, id) => page.locator(`[data-field="${id}"]`);
async function ready(page, category = "review") {
  await page.goto(`/settings-center/${category}`);
  await expect(page.locator("#settings-fields")).toHaveAttribute(
    "aria-busy",
    "false",
  );
}
async function expand(item) {
  await item
    .locator("xpath=ancestor::details")
    .evaluateAll((nodes) => nodes.forEach((node) => (node.open = true)));
}
async function configuration(page, category) {
  return await (
    await page.request.get(`/api/v1/config-center?category=${category}`)
  ).json();
}
const saved = async (item) =>
  expect(item.locator(".settings-status")).toContainText(/已保存|Saved/);

test("fallback buttons add, order, remove and reload account/model rows without JSON", async ({
  page,
}) => {
  await ready(page, "ai");
  const state = await configuration(page, "ai");
  const accountIds = [];
  for (const suffix of ["one", "two"]) {
    const response = await page.request.post("/api/v1/config/ai/accounts", {
      headers: { "X-CSRF-Token": state.csrf },
      data: {
        name: `Fallback ${suffix}`,
        provider_id: "openai",
        api_key: "isolated-editor-test-key",
        default_model: `model-${suffix}`,
        models: [`model-${suffix}`],
        enabled: true,
      },
    });
    const result = await response.json();
    expect(result.success).toBe(true);
    accountIds.push(result.data.account.id);
  }
  await ready(page, "ai");
  const binding = row(page, "binding/main");
  await expand(binding);
  await binding
    .locator('[data-assignment="primary"] select')
    .selectOption(accountIds[0]);
  await saved(binding);
  const patches = [];
  page.on("request", (request) => {
    if (request.method() === "PATCH") patches.push(request.postDataJSON());
  });
  await binding.getByRole("button", { name: /添加回退|Add fallback/ }).click();
  await page.waitForTimeout(800);
  expect(patches).toHaveLength(0);
  await expect(binding.locator("textarea")).toHaveCount(0);
  let fallback = binding.locator('[data-assignment="fallback"]');
  await page.route(
    "**/api/v1/config-center",
    (route) =>
      route.request().method() === "PATCH" ? route.abort() : route.continue(),
    { times: 1 },
  );
  await fallback
    .first()
    .getByRole("combobox", { name: /AI 账号|AI account/, exact: true })
    .selectOption(accountIds[1]);
  await expect(binding.locator(".settings-status")).toContainText(
    /保存失败|Save failed/,
  );
  await expect(fallback.first().locator("select")).toHaveValue(accountIds[1]);
  await binding.getByRole("button", { name: /^重试$|^Retry$/ }).click();
  await saved(binding);
  await binding.getByRole("button", { name: /添加回退|Add fallback/ }).click();
  await fallback.last().locator("select").selectOption(accountIds[0]);
  await saved(binding);
  await fallback
    .last()
    .getByRole("button", { name: /上移|Move up/ })
    .click();
  await expect(fallback.first().locator("select")).toHaveValue(accountIds[0]);
  await saved(binding);
  const current = (await configuration(page, "ai")).fields.find(
    (field) => field.id === "binding/main",
  );
  expect(current.value).toEqual({
    primary: { account: accountIds[0], model: "model-one" },
    fallback: [
      { account: accountIds[0], model: "model-one" },
      { account: accountIds[1], model: "model-two" },
    ],
  });
  await fallback
    .first()
    .getByRole("button", { name: /删除回退|Remove fallback/ })
    .click();
  await expect(fallback).toHaveCount(1);
  await saved(binding);
  await page.reload();
  await expect(
    binding.locator('[data-assignment="fallback"] select'),
  ).toHaveValue(accountIds[1]);
  await expect(
    binding.locator('[data-assignment="fallback"] input'),
  ).toHaveValue("model-two");
  const summary = row(page, "binding/summary");
  await expand(summary);
  const summaryAccount = summary.locator('[data-assignment="primary"] select');
  await summaryAccount.selectOption("main");
  await saved(summary);
  await expect(
    summary.locator('[data-assignment="primary"] input'),
  ).toBeDisabled();
  await expect(
    summary.locator('[data-assignment="primary"] input'),
  ).toHaveValue(/跟随主角色|Follow main/);
  await summaryAccount.selectOption("");
  await expect(summaryAccount).toHaveValue("");
  await summaryAccount.selectOption("main");
  await expect(summary.locator(".settings-status")).not.toContainText(
    /失败|failed/,
  );
  await page.screenshot({
    path: "test-results/settings-fallback-form.png",
    fullPage: true,
  });
  await binding.screenshot({
    path: "test-results/settings-fallback-desktop.png",
  });
  await page.setViewportSize({ width: 390, height: 1000 });
  await page.reload();
  await expect(
    binding.locator('[data-assignment="fallback"] input'),
  ).toHaveValue("model-two");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await binding.screenshot({
    path: "test-results/settings-fallback-mobile.png",
  });
  await binding
    .getByRole("button", { name: /删除回退|Remove fallback/ })
    .click();
  await expect(binding.locator('[data-assignment="fallback"]')).toHaveCount(0);
  await saved(binding);
});

test("list buttons save typed values and reset only that list", async ({
  page,
}) => {
  await ready(page);
  const item = row(page, "section/strategy.file_filters/skip_extensions");
  await expand(item);
  const original = (await configuration(page, "review")).fields.find(
    (field) => field.id === "section/strategy.file_filters/skip_extensions",
  );
  const inputs = item.locator("input");
  await item.getByRole("button", { name: /添加一项|Add item/ }).click();
  await expect(inputs).toHaveCount(original.value.length + 1);
  await inputs.last().fill(".editor-test");
  await saved(item);
  const changed = (await configuration(page, "review")).fields.find(
    (field) => field.id === original.id,
  );
  expect(changed.value).toEqual([...original.value, ".editor-test"]);
  await item
    .locator(".settings-editor-row")
    .last()
    .getByRole("button", { name: /删除|Remove/ })
    .click();
  await saved(item);
  expect(
    (await configuration(page, "review")).fields.find(
      (field) => field.id === original.id,
    ).value,
  ).toEqual(original.value);
  await expect(item.locator("textarea")).toHaveCount(0);
});

test("category forms retain historical custom fields, zero and false", async ({
  page,
}) => {
  await ready(page, "issue");
  const state = await configuration(page, "issue");
  const current = state.fields.find(
    (field) => field.id === "section/strategy.issue_analysis/categories",
  );
  const categories = structuredClone(current.value);
  categories[0].custom = { counter: 0, enabled: false, text: "keep me" };
  const response = await page.request.patch("/api/v1/config-center", {
    headers: { "X-CSRF-Token": state.csrf },
    data: { field: current.id, version: current.version, value: categories },
  });
  expect(response.status()).toBe(200);
  await ready(page, "issue");
  const item = row(page, current.id);
  await expand(item);
  const first = item.locator(".settings-editor-list > details").first();
  await first.locator(":scope > summary").click();
  await first
    .getByRole("textbox", { name: /说明|Description/, exact: true })
    .fill("Edited through the category form");
  await saved(item);
  const result = (await configuration(page, "issue")).fields.find(
    (field) => field.id === current.id,
  ).value;
  expect(result[0].custom).toEqual(categories[0].custom);
  expect(result[0].keywords).toEqual(categories[0].keywords);
  expect(result[0].description).toBe("Edited through the category form");
  await page.screenshot({
    path: "test-results/settings-category-form.png",
    fullPage: true,
  });
});

test("label map and thinking controls create typed values without JSON", async ({
  page,
}) => {
  await ready(page, "labels");
  const labels = row(page, "section/label.definitions");
  await expand(labels);
  await labels
    .getByRole("button", { name: /添加条目: 标签定义|Add entry: Definitions/ })
    .first()
    .click();
  const entry = labels.locator(".settings-editor-object > details").last();
  await entry
    .getByRole("textbox", { name: /标签名称|Label name/ })
    .fill("editor-test-label");
  await entry
    .getByRole("textbox", { name: /颜色|Color/, exact: true })
    .fill("aabbcc");
  await saved(labels);
  await entry
    .getByRole("textbox", { name: /说明|Description/, exact: true })
    .fill("Form-created label");
  await saved(labels);
  expect(
    (await configuration(page, "labels")).fields.find(
      (field) => field.id === "section/label.definitions",
    ).value["editor-test-label"],
  ).toEqual({ color: "aabbcc", description: "Form-created label" });
  await ready(page, "ai");
  await page
    .getByText(/模型能力覆盖|Model capability overrides/, { exact: true })
    .click();
  await page
    .locator('input[aria-label="模型 ID"], input[aria-label="Model ID"]')
    .last()
    .fill("editor-model");
  await page
    .getByRole("button", { name: /打开模型设置|Open model settings/ })
    .click();
  const thinking = row(
    page,
    "model/openai/editor-model/reasoning_params/thinking",
  );
  await thinking.locator("select").first().selectOption("1");
  await saved(thinking);
  await thinking.locator("select").last().selectOption("disabled");
  await saved(thinking);
  await thinking.locator("select").first().selectOption("0");
  await saved(thinking);
  const data = await (
    await page.request.get(
      "/api/v1/config-center?category=ai&provider=openai&model=editor-model",
    )
  ).json();
  expect(
    data.fields.find((field) => field.id.endsWith("/reasoning_params/thinking"))
      .value,
  ).toBeNull();
});

test("plugin page defaults to forms and saves MCP headers and hooks with explicit submit", async ({
  page,
}) => {
  await page.goto("/agent-plugins/");
  await expect(
    page.locator("#plugin-form-editor .settings-value-editor"),
  ).toBeVisible();
  await expect(page.locator("#plugin-json")).not.toBeVisible();
  const servers = page.locator('[data-editor-path="/mcp/servers"]');
  await servers.getByRole("button", { name: /添加一项|Add item/ }).click();
  const server = servers.locator(".settings-editor-row").first();
  await server
    .getByRole("textbox", { name: /标识|ID/, exact: true })
    .fill("form-server");
  await server
    .getByRole("textbox", { name: /服务地址|Service URL/, exact: true })
    .fill("https://mcp.example.com/mcp");
  await server
    .locator(
      'select[aria-label$="添加配置项"], select[aria-label$="Add setting"]',
    )
    .selectOption("headers");
  await server.getByRole("button", { name: /添加配置项|Add setting/ }).click();
  const headers = server.locator('[data-editor-path="/mcp/servers/0/headers"]');
  await headers.getByRole("button", { name: /添加条目|Add entry/ }).click();
  await headers
    .getByRole("textbox", { name: /名称|Name/ })
    .fill("Authorization");
  await headers
    .locator('input[type="password"]')
    .fill("isolated-plugin-editor-secret");
  const hooks = page.locator('[data-editor-path="/hooks"]');
  await hooks.getByRole("button", { name: /添加一项|Add item/ }).click();
  await hooks
    .getByRole("textbox", { name: /标识|ID/, exact: true })
    .fill("form-audit");
  await hooks
    .getByRole("combobox", { name: /执行方式|Action kind/, exact: true })
    .selectOption("audit");
  const submits = [];
  page.on("request", (request) => {
    if (request.method() === "POST") submits.push(request);
  });
  await page.waitForTimeout(800);
  expect(submits).toHaveLength(0);
  await page.getByRole("button", { name: /保存插件|Save plugins/ }).click();
  await expect(
    page.locator("#plugin-form-editor .settings-value-editor"),
  ).toBeVisible();
  const stored = await (await page.request.get("/agent-plugins/config")).json();
  expect(stored.plugins.mcp.servers[0].id).toBe("form-server");
  expect(stored.plugins.mcp.servers[0].headers.Authorization).toBe(
    "[UNCHANGED SECRET]",
  );
  expect(stored.plugins.hooks[0].kind).toBe("audit");
  expect(await page.content()).not.toContain("isolated-plugin-editor-secret");
  await page
    .getByRole("button", { name: /高级：原始 JSON|Advanced raw JSON/ })
    .click();
  await expect(page.locator("#plugin-json")).toBeVisible();
  const imported = JSON.parse(await page.locator("#plugin-json").inputValue());
  imported.mcp.servers[0].timeout_seconds = 47;
  await page.locator("#plugin-json").fill(JSON.stringify(imported));
  await page.getByRole("button", { name: /表单编辑|Form editor/ }).click();
  await page.getByRole("button", { name: /保存插件|Save plugins/ }).click();
  const updated = await (
    await page.request.get("/agent-plugins/config")
  ).json();
  expect(updated.plugins.mcp.servers[0].timeout_seconds).toBe(47);
  expect(updated.plugins.mcp.servers[0].headers.Authorization).toBe(
    "[UNCHANGED SECRET]",
  );
  await page.screenshot({
    path: "test-results/settings-plugin-form.png",
    fullPage: true,
  });
});

test("autosave toggle and debounced number persist without navigation, then reset one field", async ({
  page,
}) => {
  await ready(page);
  const requests = [];
  page.on("request", (r) => {
    if (r.method() === "PATCH") requests.push(r.postDataJSON());
  });
  const before = await page.evaluate(() => performance.timeOrigin);
  const timeout = row(page, "global/review_timeout_seconds");
  await timeout.locator("input").fill("123");
  await expect(timeout.locator("[role=status]")).toHaveText(/已保存|Saved/);
  expect(requests).toHaveLength(1);
  expect(requests[0].field).toBe("global/review_timeout_seconds");
  expect(await page.evaluate(() => performance.timeOrigin)).toBe(before);
  const toggle = row(page, "global/enable_auto_review");
  const initial = await toggle.locator("input").isChecked();
  await toggle.locator("input").setChecked(!initial);
  await expect(toggle.locator("[role=status]")).toHaveText(/已保存|Saved/);
  await timeout
    .getByRole("button", { name: /恢复默认|Restore default/ })
    .click();
  await expect(timeout.locator("[role=status]")).toHaveText(/已保存|Saved/);
  await expect(toggle.locator("input")).toBeChecked({ checked: !initial });
});

test("network failure retains input, retry succeeds, cross-tab conflict preserves the draft", async ({
  page,
  context,
}) => {
  await ready(page);
  const item = row(page, "global/review_timeout_seconds");
  await page.route(
    "**/api/v1/config-center",
    (route) =>
      route.request().method() === "PATCH" ? route.abort() : route.continue(),
    { times: 1 },
  );
  await item.locator("input").fill("157");
  await expect(item.locator("[role=status]")).toContainText(
    /保存失败|Save failed/,
  );
  await expect(item.locator("input")).toHaveValue("157");
  await item.getByRole("button", { name: /^重试$|^Retry$/ }).click();
  await expect(item.locator("[role=status]")).toContainText(/已保存|Saved/);
  const other = await context.newPage();
  await ready(other);
  // Keep the first tab's original version; the actual HTTP endpoint rejects it.
  const state = await page.request.get("/api/v1/config-center?category=review");
  const old = (await state.json()).fields.find(
    (f) => f.id === "global/review_timeout_seconds",
  );
  await row(other, old.id).locator("input").fill("158");
  await expect(row(other, old.id).locator("[role=status]")).toContainText(
    /已保存|Saved/,
  );
  const csrf = await page
    .locator("#settings-bootstrap")
    .evaluate((n) => JSON.parse(n.textContent).csrf);
  const conflict = await page.request.patch("/api/v1/config-center", {
    headers: { "X-CSRF-Token": csrf },
    data: { field: old.id, version: old.version, value: 159 },
  });
  expect(conflict.status()).toBe(409);
  await other.close();
});

test("Chinese composition waits for compositionend and search navigates and expands a group", async ({
  page,
}) => {
  await ready(page, "agent");
  const item = row(page, "global/agent_team_feasibility_keywords");
  await item
    .locator("xpath=ancestor::details")
    .evaluateAll((nodes) => nodes.forEach((n) => (n.open = true)));
  const input = item.locator("input");
  const patches = [];
  page.on("request", (r) => {
    if (r.method() === "PATCH") patches.push(r);
  });
  await input.dispatchEvent("compositionstart");
  await input.fill("中文输入");
  await page.waitForTimeout(850);
  expect(patches).toHaveLength(0);
  await input.dispatchEvent("compositionend");
  await expect(item.locator("[role=status]")).toContainText(/已保存|Saved/);
  expect(patches).toHaveLength(1);
  await page.locator("#settings-search").fill("smtp_port");
  await page.locator("#settings-results a").first().click();
  await expect(page).toHaveURL(/settings-center\/system/);
  await expect(row(page, "global/smtp_port").locator("input")).toBeFocused();
  expect(await page.locator('[data-field^="section/strategy."]').count()).toBe(
    0,
  );
});

test("personal permissions, language autosave and dark mode", async ({
  browser,
}) => {
  const context = await browser.newContext();
  await context.addCookies([
    { name: "test-role", value: "user", domain: "127.0.0.1", path: "/" },
  ]);
  const page = await context.newPage();
  await ready(page, "personal");
  expect(await page.locator("[data-category-link]").count()).toBe(1);
  expect((await page.request.get("/notifications/settings")).status()).toBe(
    200,
  );
  await row(page, "personal/language").locator("select").selectOption("en");
  await expect(page.locator("h1")).toHaveText("Configuration centre");
  await row(page, "personal/theme").locator("select").selectOption("dark");
  await expect(page.locator("html")).toHaveClass(/dark/);
  await page.locator("[data-theme-toggle]").click();
  await expect(page.locator("html")).not.toHaveClass(/dark/);
  await page.emulateMedia({ colorScheme: "dark" });
  await row(page, "personal/theme").locator("select").selectOption("system");
  await expect(
    row(page, "personal/theme").locator("[role=status]"),
  ).toContainText(/Saved/);
  await page.reload();
  await expect(page.locator("html")).toHaveClass(/dark/);
  const forbidden = await page.request.get(
    "/api/v1/config-center?category=system",
  );
  expect(forbidden.status()).toBe(404);
  expect(
    JSON.stringify(
      await (
        await page.request.get("/api/v1/config-center?search=true")
      ).json(),
    ),
  ).not.toContain("smtp_password");
  await context.close();
});

for (const theme of ["dark", "light", "system"]) {
  test(`initial load restores persisted ${theme} theme in a fresh browser`, async ({
    browser,
  }) => {
    const context = await browser.newContext({ colorScheme: "dark" });
    await context.addCookies([
      { name: "test-role", value: "user", domain: "127.0.0.1", path: "/" },
    ]);
    const state = await (
      await context.request.get("/api/v1/config-center?category=personal")
    ).json();
    const field = state.fields.find((item) => item.id === "personal/theme");
    const saved = await context.request.patch("/api/v1/config-center", {
      headers: { "X-CSRF-Token": state.csrf },
      data: { field: field.id, version: field.version, value: theme },
    });
    expect(saved.status()).toBe(200);
    const page = await context.newPage();
    const writes = [];
    page.on("request", (request) => {
      if (request.method() === "PATCH") writes.push(request);
    });
    await ready(page, "personal");
    await expect(row(page, field.id).locator("select")).toHaveValue(theme);
    if (theme === "light")
      await expect(page.locator("html")).not.toHaveClass(/dark/);
    else await expect(page.locator("html")).toHaveClass(/dark/);
    expect(await page.evaluate(() => localStorage.getItem("theme"))).toBe(
      theme,
    );
    expect(writes).toHaveLength(0);
    await context.close();
  });
}

test("empty personal language follows the request and reset inherits the global value", async ({
  page,
  browser,
}) => {
  const globalState = await (
    await page.request.get("/api/v1/config-center")
  ).json();
  const globalField = globalState.fields.find(
    (field) => field.id === "global/output_language",
  );
  const forced = await page.request.patch("/api/v1/config-center", {
    headers: { "X-CSRF-Token": globalState.csrf },
    data: { field: globalField.id, version: globalField.version, value: "en" },
  });
  expect(forced.status()).toBe(200);
  const context = await browser.newContext();
  await context.addCookies([
    { name: "test-role", value: "user", domain: "127.0.0.1", path: "/" },
  ]);
  const personal = await context.newPage();
  try {
    await ready(personal, "personal");
    const language = row(personal, "personal/output_language");
    await expect(language.locator("select")).toHaveValue("en");
    await expect(language.locator('option[value=""]')).toHaveText(
      /跟随请求\/界面语言|Follow request\/UI language/,
    );
    await language.locator("select").selectOption("");
    await expect(language.locator("[role=status]")).toContainText(
      /已保存|Saved/,
    );
    const saved = (
      await (
        await personal.request.get("/api/v1/config-center?category=personal")
      ).json()
    ).fields.find((field) => field.id === "personal/output_language");
    expect(saved).toMatchObject({
      value: "",
      source: "user",
      overridden: true,
      inheritedValue: "en",
    });
    await ready(personal, "personal");
    await expect(language.locator("select")).toHaveValue("");
    await language
      .getByRole("button", { name: /恢复默认|Restore default/ })
      .click();
    await expect(language.locator("[role=status]")).toContainText(
      /已保存|Saved/,
    );
    await expect(language.locator("select")).toHaveValue("en");
    const reset = (
      await (
        await personal.request.get("/api/v1/config-center?category=personal")
      ).json()
    ).fields.find((field) => field.id === "personal/output_language");
    expect(reset).toMatchObject({
      value: "en",
      source: "database",
      overridden: false,
    });
  } finally {
    await context.close();
    const committed = (await forced.json()).field;
    await page.request.post("/api/v1/config-center/reset", {
      headers: { "X-CSRF-Token": globalState.csrf },
      data: { field: committed.id, version: committed.version },
    });
  }
});

test("nullable system scalars expose a working independent reset", async ({
  page,
}) => {
  await ready(page, "system");
  const host = row(page, "global/smtp_host");
  await host
    .locator("xpath=ancestor::details")
    .evaluateAll((nodes) => nodes.forEach((node) => (node.open = true)));
  const initial = await (
    await page.request.get("/api/v1/config-center?category=system")
  ).json();
  const original = initial.fields.find(
    (field) => field.id === "global/smtp_host",
  );
  const port = initial.fields.find((field) => field.id === "global/smtp_port");
  expect(original.resettable).toBe(true);
  await host.locator("input").fill("smtp.browser.example");
  await expect(host.locator("[role=status]")).toContainText(/已保存|Saved/);
  await host.getByRole("button", { name: /恢复默认|Restore default/ }).click();
  await expect(host.locator("input")).toHaveValue(
    original.inheritedValue ?? "",
  );
  const reset = await (
    await page.request.get("/api/v1/config-center?category=system")
  ).json();
  expect(reset.fields.find((field) => field.id === original.id).value).toBe(
    original.inheritedValue,
  );
  expect(reset.fields.find((field) => field.id === port.id).value).toBe(
    port.value,
  );
});

for (const viewport of [
  { width: 1440, height: 1000 },
  { width: 820, height: 1100 },
  { width: 390, height: 844 },
]) {
  test(`responsive shell and secret confirmation at ${viewport.width}px`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    await ready(page, "system");
    const secret = row(page, "global/smtp_password");
    await secret
      .locator("xpath=ancestor::details")
      .evaluateAll((nodes) => nodes.forEach((n) => (n.open = true)));
    const writes = [];
    page.on("request", (r) => {
      if (r.method() === "PATCH") writes.push(r);
    });
    await secret.locator("input").fill("new-private-value");
    await page.waitForTimeout(800);
    expect(writes).toHaveLength(0);
    await expect(
      secret.getByRole("button", { name: /确认并保存|Confirm and save/ }),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await secret.locator("input").fill("");
    await page.evaluate(() => window.scrollTo(0, 0));
    if (process.env.UPDATE_SETTINGS_SCREENSHOTS && viewport.width === 1440)
      await page.screenshot({
        path: "../../docs/images/settings-center-desktop.png",
        fullPage: true,
      });
    if (process.env.UPDATE_SETTINGS_SCREENSHOTS && viewport.width === 390)
      await page.screenshot({
        path: "../../docs/images/settings-center-mobile.png",
        fullPage: true,
      });
  });
}

test("legacy routes redirect and backup retains explicit import confirmation", async ({
  page,
}) => {
  for (const [url, category] of [
    ["/config", "review"],
    ["/config/ai", "ai"],
    ["/config/backup", "backup"],
    ["/settings/", "personal"],
    ["/system-config/", "system"],
  ]) {
    await page.goto(url);
    await expect(page).toHaveURL(new RegExp(`/settings-center/${category}`));
    await expect(page.locator("h1")).toHaveCount(1);
  }
  await ready(page, "backup");
  await expect(
    page.locator('form[action="/config/backup/import"]'),
  ).toHaveAttribute("data-confirm", /.+/);
  expect(
    await page.locator('#sidebar a[href*="settings-center"]').count(),
  ).toBe(1);
});

test("AI account creation stays explicit and bindings autosave without exposing the credential", async ({
  page,
}) => {
  await ready(page, "ai");
  const name = page.locator('[x-model="form.name"]');
  await name.fill("Browser test account");
  await page
    .locator('[x-model="form.api_key"]')
    .fill("opaque-browser-test-credential");
  await page.locator('[x-model="form.default_model"]').fill("test-model");
  const saved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/config/ai/accounts") &&
      response.request().method() === "POST",
  );
  await page
    .locator("form")
    .filter({ has: name })
    .locator('button[type="submit"]')
    .click();
  const accountResponse = await saved;
  const data = await accountResponse.json();
  expect(data.success).toBe(true);
  expect(JSON.stringify(data)).not.toContain("opaque-browser-test-credential");
  const binding = row(page, "binding/main");
  await expect(binding.locator("select")).toContainText("Browser test account");
  await binding.locator("select").selectOption(data.data.account.id);
  await expect(binding.locator("[role=status]")).toContainText(/已保存|Saved/);
  const state = await page.request.get("/api/v1/config/ai/bindings");
  expect((await state.json()).data.bindings.main.primary.account).toBe(
    data.data.account.id,
  );
  await page
    .getByText(/模型能力覆盖|Model capability overrides/, { exact: true })
    .click();
  const editor = page
    .locator('input[aria-label="模型 ID"],input[aria-label="Model ID"]')
    .last();
  await editor.fill("test-model");
  await page
    .getByRole("button", { name: /打开模型设置|Open model settings/ })
    .click();
  const vision = row(page, "model/openai/test-model/capabilities/vision");
  await vision.locator("input").check();
  await expect(vision.locator("[role=status]")).toContainText(/已保存|Saved/);
  await vision
    .getByRole("button", { name: /恢复默认|Restore default/ })
    .click();
  await expect(vision.locator("input")).not.toBeChecked();
  const streamUsage = row(
    page,
    "model/openai/test-model/capabilities/stream_usage",
  );
  await expect(streamUsage.locator("input")).toBeChecked();
  await streamUsage.locator("input").uncheck();
  await expect(streamUsage.locator("[role=status]")).toContainText(
    /已保存|Saved/,
  );
  const savedModel = await page.request.get(
    "/api/v1/config-center?category=ai&provider=openai&model=test-model",
  );
  const fields = (await savedModel.json()).fields;
  expect(
    fields.find((field) => field.id.endsWith("/capabilities/stream_usage"))
      .value,
  ).toBe(false);
  await page.screenshot({
    path: "test-results/billing-stream-usage.png",
    fullPage: true,
  });
  await streamUsage.screenshot({
    path: "test-results/billing-stream-usage-row.png",
  });
  await streamUsage
    .getByRole("button", { name: /恢复默认|Restore default/ })
    .click();
  await expect(streamUsage.locator("input")).toBeChecked();
});

test("legacy section bookmarks select and expand their new category", async ({
  page,
}) => {
  await page.goto("/config#section-strategy-issue-analysis");
  await expect(page).toHaveURL(/settings-center\/issue/);
  await expect(
    page.locator('[data-group="strategy.issue_analysis"]'),
  ).toHaveAttribute("open", "");
});
