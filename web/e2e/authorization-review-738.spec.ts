import { expect, test } from "@playwright/test";

const bootstrap = (username: string) => ({
  user: {
    id: username === "alice" ? 1 : 2,
    username,
    display_name: username,
    role: "user",
    avatar_url: null,
  },
  preferences: { language: "en", theme: "light", items_per_page: 20 },
  permissions: { admin: false, super_admin: false, manage_settings: false },
  features: {
    payment_enabled: false,
    agent_enabled: false,
    github_app_configured: true,
  },
  csrf: "fixture-csrf",
  version: "3.2.6",
  timezone: "UTC",
  links: {
    login: "/app/login",
    logout: "/api/v1/webui/logout",
    mfa: "/app/security",
  },
});

test("authorization cache follows the authenticated account when bootstrap refreshes", async ({
  page,
}) => {
  let username = "alice";
  await page.clock.install();
  await page.route("**/api/v1/webui/bootstrap", (route) =>
    route.fulfill({ json: bootstrap(username) }),
  );
  await page.route("**/api/v1/github-app", (route) =>
    route.fulfill({
      json: {
        authorization: {
          status: "connected",
          github_username: username,
          installations: [],
          error_code: null,
        },
        authorization_url: null,
        install_url: null,
        repository_count: 0,
      },
    }),
  );
  await page.goto("/app/github-app");
  await expect(page.locator(".github-connection")).toContainText("@alice");
  username = "bob";
  await page.clock.fastForward(61_000);
  await page.evaluate(() =>
    window.dispatchEvent(new Event("visibilitychange")),
  );
  await expect(page.locator(".github-connection")).toContainText("@bob");
  await expect(page.locator(".github-connection")).not.toContainText("@alice");
  await expect(page.getByRole("link", { name: "Version update" })).toHaveCount(
    0,
  );
});

test("review links use repository number and missing numbers disable the GitHub action", async ({
  page,
}) => {
  let prNumber: number | null = 42;
  await page.route("**/api/v1/webui/bootstrap", (route) =>
    route.fulfill({ json: bootstrap("alice") }),
  );
  await page.route("**/api/v1/reviews/5", (route) =>
    route.fulfill({
      json: {
        success: true,
        message: "ok",
        data: {
          id: 5,
          pr_id: 987654321,
          pr_number: prNumber,
          repo_owner: "alice",
          repo_name: "app",
          title: "PR link regression",
          comments: [],
        },
      },
    }),
  );
  await page.goto("/app/reviews/5");
  await expect(
    page.getByRole("link", { name: "GitHub", exact: true }),
  ).toHaveAttribute("href", "https://github.com/alice/app/pull/42");
  await expect(page.locator(".wf-page-header")).toContainText("#42");
  prNumber = null;
  await page.reload();
  await expect(
    page.getByRole("button", { name: "GitHub", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("link", { name: "GitHub", exact: true }),
  ).toHaveCount(0);
});
