import { expect, test } from "@playwright/test";

const setupToken = "isolated-setup-browser-token";
const databaseUrl = "mysql+asyncmy://runtime:fixture@db.invalid/sakura";
const firstRepairPassword = "wrong-one-time-password";
const retryRepairPassword = "second-one-time-password";

const databaseFailure = {
  success: false,
  connected: true,
  schema_ready: false,
  initialized: false,
  stage: "preflight",
  retryable: true,
  dialect: "mysql",
  version: "8.4.13",
  identity: "runtime@%",
  log_bin: 1,
  log_bin_trust_function_creators: 0,
  privileges: ["CREATE", "ALTER", "TRIGGER"],
  code: "billing_schema_privilege_required",
  message: "billing_schema_privilege_required",
  database_error_code: 1419,
  failed_object: "billing_transactions_no_update",
  warnings: [],
  repair_sql: "-- isolated fixture only\nSELECT 1;\n",
  guards: [{ name: "billing_transactions_no_update", status: "missing" }],
};

const localizedCopy = {
  en: {
    setupHeading: "Set up Sakura AI",
    workspaceHeading: "Configure your workspace",
    credential: "Setup credential",
    continue: "Continue",
    databaseUrl: "Database URL",
    preflight: "Check database connection",
    repairDisclosure: "Repair initialization with a temporary administrator",
    repairUsername: "Database administrator username",
    repairPassword: "Database administrator password",
    repair: "Repair initialization",
    confirm: "Confirm",
    finish: "Finish setup",
    complete: "Setup complete",
  },
  "zh-CN": {
    setupHeading: "初始化 Sakura AI",
    workspaceHeading: "配置工作空间",
    credential: "初始化凭据",
    continue: "继续",
    databaseUrl: "数据库连接地址",
    preflight: "检查数据库连接",
    repairDisclosure: "使用临时管理员修复初始化",
    repairUsername: "数据库管理员用户名",
    repairPassword: "数据库管理员密码",
    repair: "修复初始化",
    confirm: "确认",
    finish: "完成配置",
    complete: "配置已完成",
  },
} as const;

for (const language of ["en", "zh-CN"] as const) {
  test(`setup browser protects temporary repair credentials (${language})`, async ({
    page,
  }) => {
    const copy = localizedCopy[language];
    const repairBodies: Array<Record<string, unknown>> = [];
    const completionBodies: Array<Record<string, unknown>> = [];
    const authorizedRequests: string[] = [];
    let repairCount = 0;

    await page.addInitScript((value) => {
      window.localStorage.setItem("sakura.language", value);
    }, language);

    await page.route("**/api/v1/setup/**", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname;
      const headers = request.headers();
      const setupCredentialHeader = headers["x-setup-token"] ?? "";
      expect(["", setupToken]).toContain(setupCredentialHeader);
      expect(url.search).not.toContain(setupToken);
      expect(request.postData() ?? "").not.toContain(setupToken);
      for (const [name, value] of Object.entries(headers)) {
        if (name !== "x-setup-token") expect(value).not.toContain(setupToken);
      }

      if (path === "/api/v1/setup/browser") {
        if (setupCredentialHeader !== setupToken) {
          return route.fulfill({
            status: 403,
            json: { success: false, error: "Setup authorization required" },
          });
        }
        authorizedRequests.push(path);
        return route.fulfill({
          json: {
            state: "in_progress",
            missing_fields: [],
            configured: { DATABASE_URL: false, REDIS_URL: false },
          },
        });
      }

      // These versioned API fixtures keep Setup isolated from any real database,
      // administrator account, restart, or external provider.
      expect(headers["x-setup-token"]).toBe(setupToken);
      authorizedRequests.push(path);

      if (path === "/api/v1/setup/browser/verify") {
        expect(request.method()).toBe("POST");
        expect(request.postData()).toBeNull();
        return route.fulfill({ json: { success: true } });
      }

      if (path === "/api/v1/setup/database-preflight") {
        expect(request.method()).toBe("POST");
        expect(request.postDataJSON()).toEqual({ database_url: databaseUrl });
        return route.fulfill({
          status: 409,
          json: {
            success: false,
            error: databaseFailure.message,
            code: databaseFailure.code,
            data: databaseFailure,
          },
        });
      }

      if (path === "/api/v1/setup/database-repair") {
        expect(request.method()).toBe("POST");
        const body = request.postDataJSON() as Record<string, unknown>;
        repairBodies.push(body);
        expect(body.database_url).toBe(databaseUrl);
        expect(body.confirmed).toBe(true);
        repairCount += 1;

        if (repairCount === 1) {
          expect(body).toEqual({
            database_url: databaseUrl,
            username: "persistent_dba",
            password: firstRepairPassword,
            confirmed: true,
          });
          return route.fulfill({
            status: 500,
            json: {
              success: false,
              error: "database_admin_authentication_failed",
              code: "database_admin_authentication_failed",
              data: {
                ...databaseFailure,
                stage: "repair",
                code: "database_admin_authentication_failed",
                message: "database_admin_authentication_failed",
                database_error_code: 1045,
              },
            },
          });
        }

        expect(body).toEqual({
          database_url: databaseUrl,
          username: "persistent_dba",
          password: retryRepairPassword,
          confirmed: true,
        });
        return route.fulfill({
          json: {
            success: true,
            message: "ok",
            data: {
              ...databaseFailure,
              success: true,
              schema_ready: true,
              stage: "repair",
              code: "database_preflight_ready",
              message: "database_preflight_ready",
              database_error_code: null,
              failed_object: null,
              repaired: true,
            },
          },
        });
      }

      if (path === "/api/v1/setup/browser/complete") {
        expect(request.method()).toBe("POST");
        const body = request.postDataJSON() as Record<string, unknown>;
        completionBodies.push(body);
        expect(body).toEqual({ DATABASE_URL: databaseUrl });
        expect(JSON.stringify(body)).not.toContain(firstRepairPassword);
        expect(JSON.stringify(body)).not.toContain(retryRepairPassword);
        expect(body).not.toHaveProperty("username");
        expect(body).not.toHaveProperty("password");
        // The browser Setup endpoint returns the service result directly.
        return route.fulfill({
          json: { success: true, message: "Fixture setup complete" },
        });
      }

      throw new Error(`Unexpected Setup API request: ${request.method()} ${path}`);
    });

    await page.goto("/app/setup");
    await expect(page.getByRole("heading", { name: copy.setupHeading })).toBeVisible();
    await page.getByLabel(copy.credential).fill(setupToken);
    await page.getByRole("button", { name: copy.continue, exact: true }).click();
    await expect(page.getByRole("heading", { name: copy.workspaceHeading })).toBeVisible();
    await expect(page.getByLabel(copy.credential)).toHaveCount(0);

    await page.getByLabel(copy.databaseUrl).fill(databaseUrl);
    await page.getByRole("button", { name: copy.preflight, exact: true }).click();
    await expect(page.getByRole("status")).toHaveText(
      "billing_schema_privilege_required",
    );
    await expect(
      page.getByText(copy.repairDisclosure, { exact: true }),
    ).toBeVisible();
    await page.getByText(copy.repairDisclosure, { exact: true }).click();

    await page.getByLabel(copy.repairUsername).fill("persistent_dba");
    await page.getByLabel(copy.repairPassword).fill(firstRepairPassword);
    await page.getByRole("button", { name: copy.repair, exact: true }).click();
    await page.getByRole("dialog").getByRole("button", { name: copy.confirm, exact: true }).click();
    await expect(page.getByRole("alert")).toHaveText(
      "database_admin_authentication_failed",
    );
    await expect(page.getByLabel(copy.repairUsername)).toHaveValue("");
    await expect(page.getByLabel(copy.repairPassword)).toHaveValue("");
    await expect(page.getByText(copy.repairDisclosure, { exact: true })).toBeVisible();

    // A failed attempt remains recoverable; retry with fresh temporary credentials.
    await page.getByLabel(copy.repairUsername).fill("persistent_dba");
    await page.getByLabel(copy.repairPassword).fill(retryRepairPassword);
    await page.getByRole("button", { name: copy.repair, exact: true }).click();
    await page.getByRole("dialog").getByRole("button", { name: copy.confirm, exact: true }).click();
    await expect(page.getByRole("status")).toHaveText("database_preflight_ready");
    await expect(page.getByLabel(copy.repairUsername)).toHaveCount(0);
    await expect(page.getByLabel(copy.repairPassword)).toHaveCount(0);

    await page.getByRole("button", { name: copy.finish, exact: true }).click();
    await page.getByRole("dialog").getByRole("button", { name: copy.confirm, exact: true }).click();
    await expect(page.getByRole("heading", { name: copy.complete, exact: true })).toBeVisible();

    expect(repairBodies).toHaveLength(2);
    expect(completionBodies).toHaveLength(1);
    expect(authorizedRequests).toContain("/api/v1/setup/browser/verify");
    expect(authorizedRequests).toContain("/api/v1/setup/browser/complete");
  });
}
