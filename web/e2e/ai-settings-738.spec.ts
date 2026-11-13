import { expect, test } from "@playwright/test";

test("fallback drag order persists and model capabilities use configured account models", async ({
  page,
}) => {
  const configuration = await (
    await page.request.get("/api/v1/config-center?category=ai")
  ).json();
  const headers = { "X-CSRF-Token": configuration.csrf };
  const created = await page.request.post("/api/v1/config/ai/accounts", {
    headers,
    data: {
      name: "Issue 738 browser account",
      provider_id: "custom",
      protocol: "openai-compatible",
      api_base: "https://fixture.invalid/v1/",
      api_key: "isolated-browser-fixture",
      default_model: "primary-model",
      models: ["primary-model", "first-fallback", "second-fallback"],
    },
  });
  expect(created.ok()).toBeTruthy();
  const createdBody = await created.json();
  expect(createdBody.success).toBeTruthy();
  const account = createdBody.data.account;
  const binding = configuration.fields.find(
    (field: { id: string }) => field.id === "binding/main",
  );
  const saved = await page.request.patch("/api/v1/config-center", {
    headers,
    data: {
      field: binding.id,
      version: binding.version,
      value: {
        primary: { account: account.id, model: "primary-model" },
        fallback: [
          { account: account.id, model: "first-fallback" },
          { account: account.id, model: "second-fallback" },
        ],
      },
    },
  });
  expect(saved.ok()).toBeTruthy();
  await page.goto("/app/settings/ai");
  const row = page.locator('[id="setting-binding/main"]');
  await expect(row.getByRole("combobox", { name: /模型 ID: 1/ })).toHaveValue(
    "first-fallback",
  );
  await row
    .getByRole("button", { name: /拖拽排序: 2/ })
    .dragTo(row.getByRole("group", { name: /1$/, exact: false }).first());
  await expect(row.getByRole("combobox", { name: /模型 ID: 1/ })).toHaveValue(
    "second-fallback",
  );
  await expect(page.locator(".settings-dirty-bar")).toContainText(
    "项未保存的修改",
  );
  await row.getByRole("button", { name: "保存", exact: true }).click();
  await expect
    .poll(async () => {
      const response = await page.request.get(
        "/api/v1/config-center?category=ai",
      );
      const body = await response.json();
      return body.fields.find(
        (field: { id: string }) => field.id === "binding/main",
      ).value.fallback[0].model;
    })
    .toBe("second-fallback");
  await page.reload();
  await expect(row.getByRole("combobox", { name: /模型 ID: 1/ })).toHaveValue(
    "second-fallback",
  );
  await expect(
    row.getByRole("combobox", { name: /模型 ID: 主模型/ }),
  ).toHaveValue("primary-model");
  const modelPanel = page.locator(".settings-model-panel");
  const modelResponse = page.waitForResponse(
    (response) =>
      response.url().includes("/api/v1/config-center?category=ai") &&
      response.url().includes("provider=custom&model=primary-model"),
  );
  await modelPanel
    .getByRole("combobox", { name: "AI 账号", exact: true })
    .selectOption(account.id);
  expect((await modelResponse).ok()).toBeTruthy();
  await expect(
    modelPanel.getByRole("combobox", { name: "模型", exact: true }),
  ).toHaveValue("primary-model");
  await expect(
    modelPanel.getByRole("option", { name: "second-fallback", exact: true }),
  ).toBeAttached();
  await expect(page.locator(".settings-group-picker")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "显示高级设置" })).toHaveCount(
    0,
  );
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
  await page.screenshot({
    animations: "disabled",
    path: "artifacts/ai-settings-desktop.png",
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
  await page.screenshot({
    animations: "disabled",
    path: "artifacts/ai-settings-mobile.png",
  });
});
