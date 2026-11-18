import { expect, test } from "@playwright/test";

test("flow 1: locate AI fallback via TOC, reorder priority, batch save with policy, and verify persistence", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const initial = await (
    await page.request.get("/api/v1/config-center?category=ai")
  ).json();
  const headers = { "X-CSRF-Token": initial.csrf };

  const created = await page.request.post("/api/v1/config/ai/accounts", {
    headers,
    data: {
      name: "Flow1 Fallback Account",
      provider_id: "custom",
      protocol: "openai-compatible",
      api_base: "https://fixture.invalid/v1/",
      api_key: "isolated-flow1-fixture",
      default_model: "model-alpha",
      models: ["model-alpha", "model-beta", "model-gamma"],
    },
  });
  expect(created.ok()).toBeTruthy();
  const account = (await created.json()).data.account;

  const binding = initial.fields.find(
    (field: { id: string }) => field.id === "binding/main",
  );
  const seeded = await page.request.patch("/api/v1/config-center", {
    headers,
    data: {
      field: binding.id,
      version: binding.version,
      value: {
        primary: { account: account.id, model: "model-alpha" },
        fallback: [
          { account: account.id, model: "model-beta" },
          { account: account.id, model: "model-gamma" },
        ],
      },
    },
  });
  expect(seeded.ok()).toBeTruthy();

  await page.goto("/app/settings/ai");
  const toc = page.locator(".settings-toc-desktop");
  await expect(toc).toBeVisible();
  await expect(
    toc.getByRole("link", { name: /1\. 连接 AI 账号/ }),
  ).toBeVisible();
  await expect(
    toc.getByRole("link", { name: /2\. 分配任务模型与回退链/ }),
  ).toBeVisible();
  await expect(
    toc.getByRole("link", { name: /3\. 单模型能力与参数覆盖/ }),
  ).toBeVisible();
  const policyLink = toc.getByRole("link", {
    name: /4\. 调用超时、回退策略与活动采集/,
  });
  await expect(policyLink).toBeVisible();

  // Click TOC section link and confirm target section is visible and unobstructed
  await policyLink.click();
  await expect(page).toHaveURL(/#section-ai_strategy$/);
  await expect(page.locator("#section-ai_strategy h3")).toBeFocused();
  const maxCandidatesRow = page.locator(
    '[id="setting-global/ai_fallback_max_candidates"]',
  );
  await expect(maxCandidatesRow).toBeVisible();
  await expect(maxCandidatesRow).toContainText("0 = 完整回退链");

  // Reorder fallback chain via keyboard-accessible Move up button and adjust fallback max candidates
  await toc.getByRole("link", { name: /2\. 分配任务模型与回退链/ }).click();
  const mainBindingRow = page.locator('[id="setting-binding/main"]');
  await expect(
    mainBindingRow.getByRole("combobox", { name: /模型 ID: 1/ }),
  ).toHaveValue("model-beta");
  await mainBindingRow.getByRole("button", { name: "上移: 2" }).click();
  await expect(
    mainBindingRow.getByRole("combobox", { name: /模型 ID: 1/ }),
  ).toHaveValue("model-gamma");

  const maxCandidatesInput = maxCandidatesRow.getByRole("spinbutton");
  await maxCandidatesInput.fill("2");
  const bar = page.locator(".settings-dirty-bar");
  await expect(bar).toContainText("2 项未保存");
  await expect
    .poll(async () => {
      const [fieldBox, barBox] = await Promise.all([
        maxCandidatesInput.boundingBox(),
        bar.boundingBox(),
      ]);
      return !!fieldBox && !!barBox && fieldBox.y + fieldBox.height <= barBox.y;
    })
    .toBeTruthy();

  await mainBindingRow.scrollIntoViewIfNeeded();
  await page.screenshot({
    animations: "disabled",
    path: "artifacts/settings-ai-desktop-zh-light-738.png",
  });

  await page
    .locator(".settings-dirty-bar")
    .getByRole("button", { name: "保存修改" })
    .click();
  await expect
    .poll(async () => {
      const res = await page.request.get("/api/v1/config-center?category=ai");
      const body = await res.json();
      const updatedBinding = body.fields.find(
        (field: { id: string }) => field.id === "binding/main",
      );
      const updatedMax = body.fields.find(
        (field: { id: string }) =>
          field.id === "global/ai_fallback_max_candidates",
      );
      return {
        firstFallback: updatedBinding.value.fallback[0].model,
        maxCandidates: updatedMax.value,
      };
    })
    .toEqual({ firstFallback: "model-gamma", maxCandidates: 2 });

  await page.reload();
  await expect(
    mainBindingRow.getByRole("combobox", { name: /模型 ID: 1/ }),
  ).toHaveValue("model-gamma");
  await expect(maxCandidatesRow.getByRole("spinbutton")).toHaveValue("2");

  const modelPanel = page.locator(".settings-model-panel");
  const accountSelect = modelPanel.getByRole("combobox", {
    name: "AI 账号",
    exact: true,
  });
  await accountSelect.selectOption(account.id);
  const modelRow = modelPanel.locator(
    '[id="setting-model/custom/model-alpha/context_window_tokens"]',
  );
  const modelInput = modelRow.getByRole("spinbutton");
  await modelInput.fill("65536");
  await page
    .locator(".settings-navigation")
    .getByRole("link", { name: /代码审查/ })
    .click();
  await bar.getByRole("button", { name: "查看修改" }).click();
  await page
    .getByRole("dialog", { name: "修改清单与保存结果" })
    .getByRole("button", { name: "定位" })
    .click();
  await expect(modelRow).toBeFocused();
  await expect(modelInput).toHaveValue("65536");
  await expect(accountSelect).toHaveValue(account.id);
});

test("flow 2: search, cross-category drafts, and partial review settings retry", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/app/settings/ai");

  // Search globally for review concurrency
  const searchInput = page.getByRole("textbox", { name: "搜索所有设置" });
  await searchInput.fill("max_concurrent_reviews");
  const firstHit = page.locator(".settings-search-result").first();
  await expect(firstHit).toContainText("代码审查 ›");
  await expect(firstHit).toContainText("max_concurrent_reviews");
  await searchInput.press("Enter");

  await expect(page).toHaveURL(
    /settings\/review#global%2Fmax_concurrent_reviews/,
  );
  await expect(
    page.getByRole("button", {
      name: /返回“max_concurrent_reviews”的搜索结果/,
    }),
  ).toBeVisible();

  // Edit multiple related review settings (including cross-field chunk size & overlap)
  const concurrentRow = page.locator(
    '[id="setting-global/max_concurrent_reviews"]',
  );
  const chunkSizeRow = page.locator('[id="setting-global/code_chunk_size"]');
  const chunkOverlapRow = page.locator(
    '[id="setting-global/code_chunk_overlap"]',
  );

  await expect(concurrentRow).toBeFocused();
  await concurrentRow.getByRole("spinbutton").fill("6");
  await chunkSizeRow.getByRole("spinbutton").fill("1800");
  await chunkOverlapRow.getByRole("spinbutton").fill("300");
  await page.goBack();
  await expect(searchInput).toHaveValue("max_concurrent_reviews");
  await page.goForward();
  await expect(chunkSizeRow.getByRole("spinbutton")).toHaveValue("1800");

  // Switch category to Labels and back via search without losing drafts
  await page
    .locator(".settings-navigation")
    .getByRole("link", { name: /标签与扫描/ })
    .click();
  await expect(page.locator(".settings-dirty-bar")).toContainText("3 项未保存");

  // Open Review Changes dialog and inspect grouped pending changes
  await page
    .locator(".settings-dirty-bar")
    .getByRole("button", { name: "查看修改" })
    .click();
  const dialog = page.getByRole("dialog", { name: "修改清单与保存结果" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("代码审查");
  await expect(dialog).toContainText("1800");
  await expect(dialog).toContainText("300");
  await page.screenshot({
    animations: "disabled",
    path: "artifacts/settings-changes-dialog-738.png",
  });

  // Simulate partial failure on max_concurrent_reviews once while allowing chunk settings to succeed
  let failOnce = true;
  await page.route("**/api/v1/config-center", async (route) => {
    if (route.request().method() === "PATCH") {
      const body = route.request().postDataJSON() as { field?: string };
      if (body.field === "global/max_concurrent_reviews" && failOnce) {
        failOnce = false;
        await route.fulfill({
          status: 503,
          json: {
            error: { code: "network", detail: "Simulated transient failure" },
          },
        });
        return;
      }
    }
    await route.continue();
  });

  await dialog.getByRole("button", { name: "保存修改 (3)" }).click();

  // Dialog remains open showing partial failure summary: 2 saved, 1 failed
  await expect(dialog.getByRole("alert")).toContainText("已保存 2");
  await expect(dialog.getByRole("alert")).toContainText("失败 1");
  await page.screenshot({
    path: "artifacts/settings-partial-save-738.png",
    animations: "disabled",
  });
  await expect(page.locator(".settings-dirty-bar")).toContainText("1 项未保存");

  // Retry only the 1 unsaved item and confirm all 3 are persisted
  await dialog.getByRole("button", { name: "重试未成功项 (1)" }).click();
  await expect
    .poll(async () => {
      const res = await page.request.get(
        "/api/v1/config-center?category=review",
      );
      const body = await res.json();
      const map = Object.fromEntries(
        body.fields.map((f: { id: string; value: unknown }) => [f.id, f.value]),
      );
      return {
        concurrent: map["global/max_concurrent_reviews"],
        size: map["global/code_chunk_size"],
        overlap: map["global/code_chunk_overlap"],
      };
    })
    .toEqual({ concurrent: 6, size: 1800, overlap: 300 });
  await page.unroute("**/api/v1/config-center");
});

test("flow 3: system settings, restart effect, secret masking, responsive mobile TOC, dark/en modes, and role isolation", async ({
  context,
  page,
  baseURL,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/app/settings/system");

  const portRow = page.locator('[id="setting-global/app_port"]');
  await expect(portRow).toBeVisible();
  await expect(portRow).toContainText("范围：1 – 65535");
  await expect(portRow).toContainText("修改需重启");

  await portRow.getByRole("spinbutton").fill("8088");

  const databaseRow = page.locator('[id="setting-global/database_url"]');
  await expect(databaseRow).toContainText("部署只读");
  await expect(databaseRow.locator("input")).toBeDisabled();

  // Exercise the password editor with a value longer than ordinary text fields.
  const fixtureSecret = "fixture-only-webhook-secret-738".repeat(8);
  const webhookSecretRow = page.locator(
    '[id="setting-global/github_webhook_secret"]',
  );
  await webhookSecretRow.locator("input").fill(fixtureSecret);
  await expect(webhookSecretRow.locator("input")).toHaveAttribute(
    "type",
    "password",
  );

  // Verify secret never enters browser storage or URL
  const storageDump = await page.evaluate(() => ({
    local: JSON.stringify(localStorage),
    session: JSON.stringify(sessionStorage),
    href: location.href,
  }));
  expect(storageDump.local).not.toContain(fixtureSecret);
  expect(storageDump.session).not.toContain(fixtureSecret);
  expect(storageDump.href).not.toContain(fixtureSecret);

  await page
    .locator(".settings-dirty-bar")
    .getByRole("button", { name: "查看修改" })
    .click();
  const changesDialog = page.getByRole("dialog", {
    name: "修改清单与保存结果",
  });
  await expect(changesDialog).toContainText(
    /将(?:更新|写入新凭据)（不显示内容）/,
  );
  await expect(changesDialog).not.toContainText(fixtureSecret);
  await changesDialog.getByRole("button", { name: "关闭" }).last().click();

  // Batch save prompts for sensitive confirmation, then reports restart_required on app_port
  await page
    .locator(".settings-dirty-bar")
    .getByRole("button", { name: "保存修改" })
    .click();
  const confirmDialog = page.getByRole("dialog", {
    name: "确认更新敏感配置？",
  });
  await expect(confirmDialog).toBeVisible();
  await confirmDialog.getByRole("button", { name: "确认并保存" }).click();

  await expect(portRow).toContainText("已保存，重启后生效");

  // Dark mode screenshot on Review settings
  await page.locator(".user-trigger").click();
  await page.getByRole("menuitem", { name: "深色", exact: true }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page
    .locator(".settings-navigation")
    .getByRole("link", { name: /代码审查/ })
    .click();
  await expect(
    page.locator('[id="setting-global/max_concurrent_reviews"]'),
  ).toBeVisible();
  await page.screenshot({
    animations: "disabled",
    path: "artifacts/settings-review-desktop-zh-dark-738.png",
  });

  // Switch back to Light + English and capture System settings
  await page.locator(".user-trigger").click();
  await page.getByRole("menuitem", { name: "浅色", exact: true }).click();
  await page.locator(".user-trigger").click();
  await page.getByRole("menuitem", { name: "English", exact: true }).click();
  await page
    .locator(".settings-navigation")
    .getByRole("link", { name: /^System/ })
    .click();
  await expect(
    page.getByRole("heading", { name: "Settings", exact: true }),
  ).toBeVisible();
  await expect(portRow.getByRole("spinbutton")).toHaveValue("8088");
  await page.screenshot({
    animations: "disabled",
    path: "artifacts/settings-system-desktop-en-light-738.png",
  });

  // Mobile viewport (390x844): verify collapsible "On this page" TOC without hiding fields or horizontal overflow
  await page.setViewportSize({ width: 390, height: 844 });
  const mobileToc = page.locator("details.settings-toc-mobile");
  await expect(mobileToc).toBeVisible();
  await expect(
    page.locator('.settings-navigation a[aria-current="page"]'),
  ).toBeInViewport();
  await expect(portRow).toBeVisible();
  await mobileToc.locator("summary").click();
  await expect(mobileToc).toHaveAttribute("open", "");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    animations: "disabled",
    path: "artifacts/settings-mobile-toc-738.png",
  });
  await mobileToc.locator("summary").click();
  await expect(portRow).toBeVisible();
  await page.setViewportSize({ width: 320, height: 640 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();

  // Switch identity to ordinary user: admin drafts and categories are isolated and inaccessible
  await portRow.getByRole("spinbutton").fill("8099");
  await context.addCookies([
    { name: "test-role", value: "user", url: baseURL! },
  ]);
  page.once("dialog", (dialog) => {
    void dialog.accept();
  });
  await page.goto("/app/settings/personal");
  await expect(page.locator(".settings-dirty-bar")).toHaveCount(0);
  await expect(
    page.locator(".settings-sidebar").getByText(/System|系统/),
  ).toHaveCount(0);
});

test("a delayed save preserves newer input and suppresses duplicate submissions", async ({
  page,
}) => {
  const field = "global/code_chunk_size";
  await page.goto(`/app/settings/review#${encodeURIComponent(field)}`);
  const row = page.locator(`[id="setting-${field}"]`);
  const input = row.getByRole("spinbutton");
  let finishRequest!: () => void;
  const delayed = new Promise<void>((resolve) => {
    finishRequest = resolve;
  });
  const sent: number[] = [];
  await page.route("**/api/v1/config-center", async (route) => {
    const request = route.request();
    if (
      request.method() === "PATCH" &&
      request.postDataJSON().field === field
    ) {
      sent.push(request.postDataJSON().value);
      await delayed;
    }
    await route.continue();
  });
  await input.fill("1801");
  const bar = page.locator(".settings-dirty-bar");
  await bar.getByRole("button", { name: "保存修改" }).click();
  try {
    await expect.poll(() => sent).toEqual([1801]);
    await expect(bar.getByRole("button", { name: "保存中…" })).toBeDisabled();
    await input.fill("1802");
  } finally {
    finishRequest();
  }
  await expect(bar.getByRole("button", { name: "保存修改" })).toBeEnabled();
  await expect(input).toHaveValue("1802");
  expect(sent).toEqual([1801]);
  const response = await page.request.get(
    "/api/v1/config-center?category=review",
  );
  expect(
    (await response.json()).fields.find(
      (item: { id: string }) => item.id === field,
    ).value,
  ).toBe(1801);
});
