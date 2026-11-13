import { expect, test } from "@playwright/test";

test("tool results stay paired, filterable and bounded in the browser", async ({
  page,
}) => {
  const common = {
    created_at: "2026-11-13T00:00:00Z",
    name: "read_file",
    tool_execution_id: 1,
    arguments_artifact_id: 51,
    result_artifact_id: 52,
    artifacts: [
      {
        artifact_id: 51,
        artifact_kind: "request_projection",
        can_request: true,
      },
      {
        artifact_id: 52,
        artifact_kind: "response_projection",
        can_request: true,
      },
    ],
    tool_call_id: "browser-call",
    status: "failed",
    started_at: "2026-11-13T00:00:00Z",
    completed_at: "2026-11-13T00:00:01Z",
  };
  await page.route("**/api/v1/activity/sessions/738/artifacts/*", (route) =>
    route.fulfill({
      json: {
        payload: route.request().url().endsWith("/51")
          ? '{"path":"README.md"}'
          : '{"error":"File missing"}',
      },
    }),
  );
  await page.route("**/api/v1/activity/sessions/738/snapshot", (route) =>
    route.fulfill({
      json: {
        session: {
          session_id: 738,
          repo_full_name: "owner/fixture",
          status: "completed",
        },
      },
    }),
  );
  await page.route("**/api/v1/activity/sessions/738/conversation", (route) =>
    route.fulfill({
      json: {
        entries: [
          {
            ...common,
            id: "tool_call:1",
            type: "tool_call",
            order_key: "1:6:1",
          },
          {
            ...common,
            id: "tool_result:1",
            type: "tool_result",
            order_key: "1:7:1",
            content_visibility: "restricted",
          },
        ],
        before_cursor: null,
        events_cursor: "",
        projection_version: 4,
      },
    }),
  );
  await page.goto("/app/activity/738");
  await expect(
    page.locator(".activity-entry-tool_call .activity-entry-tool_result"),
  ).toHaveCount(1);
  await expect(
    page.locator(".activity-entry-tool_result .activity-json"),
  ).toContainText("File missing");
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
  await page.screenshot({
    animations: "disabled",
    path: "artifacts/738-activity-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
  await page.screenshot({
    animations: "disabled",
    path: "artifacts/738-activity-mobile.png",
    fullPage: true,
  });
  await page.getByLabel("内容类型").selectOption("results");
  await expect(page.locator(".activity-entry-tool_call")).toHaveCount(0);
  await expect(page.locator(".activity-entry-tool_result")).toHaveCount(1);
});

test("community discovery separates management and personal selection on mobile", async ({
  page,
}) => {
  const repo = {
    id: 1,
    full_name: "owner/project",
    html_url: "https://github.com/owner/project",
    description: "Fixture project",
    ai_summary: "Browser summary",
    stargazers_count: 12,
    primary_language: "Python",
    topics: ["ai"],
    is_displayed: true,
    disabled_by_admin: false,
  };
  await page.route(/\/api\/v1\/community(?:\?.*)?$/, (route) =>
    route.fulfill({
      json: {
        feature_enabled: true,
        is_admin: true,
        is_super_admin: true,
        credential_status: "authorized",
        member: { status: "active" },
        daily_star_used: 0,
        daily_star_limit: 20,
        available_repos: [repo],
        displayed_repos: [repo],
        public_repos: [repo],
        admin_members: [
          { user_id: 2, github_username: "fixture-member", status: "active" },
        ],
        admin_member_page: { page: 1, total: 1, total_pages: 1 },
        admin_repositories: [repo],
        admin_repository_page: { page: 1, total: 1, total_pages: 1 },
      },
    }),
  );
  await page.goto("/app/community");
  await expect(
    page.getByRole("heading", { name: "社区项目", exact: true }),
  ).toBeVisible();
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
  await page.screenshot({
    animations: "disabled",
    path: "artifacts/738-community-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
  await page.screenshot({
    animations: "disabled",
    path: "artifacts/738-community-mobile.png",
    fullPage: true,
  });
  await expect(page.getByRole("checkbox")).toHaveCount(0);
  await expect(page.getByText("fixture-member")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "关闭功能", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "我的展示仓库", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("checkbox")).toBeChecked();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "关闭", exact: true })
    .click();
  await page.getByRole("tab", { name: "管理", exact: true }).click();
  await expect(page.getByText("fixture-member")).toBeVisible();
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
  await page.screenshot({
    animations: "disabled",
    path: "artifacts/738-community-management-mobile.png",
    fullPage: true,
  });
  await expect(
    page.getByRole("heading", { name: "社区项目", exact: true }),
  ).toHaveCount(0);
});
