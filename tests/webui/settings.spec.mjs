import { test, expect } from "@playwright/test";

const row = (page, id) => page.locator(`[data-field="${id}"]`);
async function ready(page, category = "review") {
  await page.goto(`/settings-center/${category}`);
  await expect(page.locator("#settings-fields")).toHaveAttribute(
    "aria-busy",
    "false",
  );
}

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
