import { expect, test } from "@playwright/test";

const setupToken = "isolated-setup-browser-token";
const databaseUrl = "mysql+asyncmy://runtime:fixture@db.invalid/sakura";

// Mirrors check_database's real connection-failure report: the outer except
// builds a fresh report with dialect "unknown" and connected=false, which
// setup_database_status maps to HTTP 400.
const databaseFailure = {
  success: false,
  connected: false,
  schema_ready: false,
  initialized: false,
  stage: "preflight",
  retryable: true,
  dialect: "unknown",
  code: "database_preflight_failed",
  message: "database_preflight_failed",
  database_error_code: 1045,
  failed_object: null,
  checks: [],
  warnings: [],
};

const localizedCopy = {
  en: {
    setupHeading: "Set up Sakura AI",
    workspaceHeading: "Configure your workspace",
    credential: "Setup credential",
    continue: "Continue",
    databaseUrl: "Database URL",
    preflight: "Check database connection",
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
    confirm: "确认",
    finish: "完成配置",
    complete: "配置已完成",
  },
} as const;

for (const language of ["en", "zh-CN"] as const) {
  test(`setup browser reports preflight results and protects the token (${language})`, async ({
    page,
  }) => {
    const copy = localizedCopy[language];
    const completionBodies: Array<Record<string, unknown>> = [];
    const authorizedRequests: string[] = [];
    let preflightCount = 0;

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
      // restart, or external provider.
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
        preflightCount += 1;
        if (preflightCount === 1) {
          // A plain connection failure reports its real code; the removed
          // Billing Guard repair flow must never reappear.
          return route.fulfill({
            status: 400,
            json: {
              success: false,
              error: databaseFailure.message,
              code: databaseFailure.code,
              data: databaseFailure,
            },
          });
        }
        return route.fulfill({
          json: {
            success: true,
            message: "ok",
            data: {
              ...databaseFailure,
              success: true,
              connected: true,
              schema_ready: true,
              dialect: "mysql",
              database: "sakura",
              version: "8.4.13-fixture",
              code: "database_preflight_ready",
              message: "database_preflight_ready",
              database_error_code: null,
              failed_object: null,
            },
          },
        });
      }

      if (path === "/api/v1/setup/browser/complete") {
        expect(request.method()).toBe("POST");
        const body = request.postDataJSON() as Record<string, unknown>;
        completionBodies.push(body);
        expect(body).toEqual({ DATABASE_URL: databaseUrl });
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
      "database_preflight_failed",
    );

    // A failed preflight stays recoverable: retrying reports success without
    // any administrator credential form.
    await page.getByRole("button", { name: copy.preflight, exact: true }).click();
    await expect(page.getByRole("status")).toHaveText("database_preflight_ready");

    await page.getByRole("button", { name: copy.finish, exact: true }).click();
    await page.getByRole("dialog").getByRole("button", { name: copy.confirm, exact: true }).click();
    await expect(page.getByRole("heading", { name: copy.complete, exact: true })).toBeVisible();

    expect(preflightCount).toBe(2);
    expect(completionBodies).toHaveLength(1);
    expect(authorizedRequests).toContain("/api/v1/setup/browser/verify");
    expect(authorizedRequests).toContain("/api/v1/setup/browser/complete");
    expect(authorizedRequests).not.toContain("/api/v1/setup/database-repair");
  });
}
