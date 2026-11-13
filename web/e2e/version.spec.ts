import { expect, test } from "@playwright/test";

test("updates recheck canonical targets and require host plus running-version evidence", async ({
  page,
}) => {
  const target = {
    channel: "development",
    version: "3.2.6",
    tag: "dev-20261030-fixture",
    digest: `sha256:${"b".repeat(64)}`,
    revision: "a".repeat(40),
  };
  let allowed = true,
    verified = false,
    starts = 0,
    healthChecks = 0;
  const preflightBodies: unknown[] = [];
  await page.route("**/api/v1/version/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/info"))
      return route.fulfill({
        json: {
          current_version: "3.2.5",
          build: { channel: "stable" },
          deployment_type: "image",
          update_supported: true,
          updater_connected: true,
          updater_version: "1.0",
          has_active_job: false,
          active_job_id: null,
          last_checked: null,
        },
      });
    if (path.endsWith("/images"))
      return route.fulfill({
        json: {
          stale: false,
          heads: { development: target },
          images: [
            {
              ...target,
              canonical_tag: target.tag,
              tag: undefined,
              tags: ["edge", target.tag],
              selectable: true,
            },
          ],
        },
      });
    if (path.endsWith("/releases"))
      return route.fulfill({ json: { releases: [] } });
    if (path.endsWith("/preflight")) {
      preflightBodies.push(route.request().postDataJSON());
      return route.fulfill({
        json: {
          data: {
            can_update: allowed,
            checks: [{ name: "target_identity_valid", passed: allowed }],
          },
        },
      });
    }
    if (path.endsWith("/update")) {
      starts++;
      expect(route.request().postDataJSON()).toEqual({
        target,
        confirm_channel_switch: true,
      });
      return route.fulfill({
        status: 202,
        json: { data: { job_id: "fixture-job" } },
      });
    }
    if (path.endsWith("/jobs/fixture-job"))
      return route.fulfill({
        json: {
          data: {
            job_id: "fixture-job",
            state: "success",
            deployment_verified: verified,
            target_version: target.version,
            target_channel: target.channel,
            target_digest: target.digest,
            target_revision: target.revision,
          },
        },
      });
    return route.fulfill({ status: 404, json: {} });
  });
  await page.route("**/health", (route) => {
    healthChecks++;
    return route.fulfill({ json: { version: target.version, build: target } });
  });
  await page.goto("/app/admin/version");
  await page
    .getByRole("combobox", { name: "更新通道" })
    .selectOption("development");
  await page.getByRole("button", { name: "选择", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText(
    "开发版可能包含尚未稳定的变更",
  );
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "确认", exact: true })
    .click();
  await page.getByRole("button", { name: "运行预检", exact: true }).click();
  await expect(page.getByText("预检通过", { exact: true })).toBeVisible();
  allowed = false;
  await page.getByRole("button", { name: "开始更新", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "开始更新", exact: true })
    .click();
  await expect(page.getByRole("alert")).toBeVisible();
  expect(starts).toBe(0);
  expect(preflightBodies).toEqual([
    { target, confirm_channel_switch: true },
    { target, confirm_channel_switch: true },
  ]);
  allowed = true;
  await page.getByRole("button", { name: "开始更新", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "开始更新", exact: true })
    .click();
  await expect(
    page.getByText("尚未验证完整部署结果，请检查 Host Updater 与运行版本。"),
  ).toBeVisible();
  expect(healthChecks).toBe(0);
  await expect(
    page.getByText("部署与运行版本均已验证", { exact: true }),
  ).toHaveCount(0);
  verified = true;
  await page.getByRole("button", { name: "继续查询此任务" }).click();
  await expect(
    page.getByText("部署与运行版本均已验证", { exact: true }),
  ).toBeVisible();
  expect(starts).toBe(1);
  expect(healthChecks).toBeGreaterThan(0);
});

test("running build channel, provenance and release notes stay visible on desktop and mobile", async ({
  page,
}) => {
  const revision = "abcdef0123456789abcdef0123456789abcdef01";
  const digest = `sha256:${"c".repeat(64)}`;
  await page.route("**/api/v1/version/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/info"))
      return route.fulfill({
        json: {
          current_version: "3.2.6",
          build: {
            channel: "development",
            revision,
            digest,
            created_at: "2026-11-13T00:00:00Z",
          },
          deployment_type: "image",
          update_supported: false,
          updater_connected: false,
          has_active_job: false,
          active_job_id: null,
          update_available: true,
          last_checked: "2026-11-13T01:00:00Z",
          next_check_at: "2026-11-13T02:00:00Z",
          scheduler_running: true,
          check_error: "timeout",
        },
      });
    if (path.endsWith("/images"))
      return route.fulfill({ json: { stale: false, heads: {}, images: [] } });
    if (path.endsWith("/releases"))
      return route.fulfill({
        json: {
          releases: [
            {
              tag_name: "v3.2.6",
              name: "Fixture release",
              body: "## Fixture changes\n\nRelease **notes** inline.",
              published_at: "2026-11-13T00:00:00Z",
            },
          ],
        },
      });
    return route.fulfill({ status: 404, json: {} });
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/app/admin/version");
  await expect(page.getByRole("combobox", { name: "更新通道" })).toHaveValue(
    "development",
  );
  await expect(page.locator(".version-build-details")).toContainText(revision);
  await expect(page.locator(".version-build-details")).toContainText(digest);
  await expect(page.locator(".topbar-current-version")).toHaveText("3.2.6");
  await expect(page.locator(".topbar-update-label")).toHaveText("有可用更新");
  await expect(page.getByText("检查失败，保留上次成功结果")).toBeVisible();
  await page.getByRole("button", { name: "发布记录", exact: true }).click();
  await page.getByRole("button", { name: "查看说明", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(
    page
      .locator(".version-release")
      .getByRole("heading", { name: "Fixture changes" }),
  ).toBeVisible();
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
  await page.screenshot({
    animations: "disabled",
    path: "artifacts/version-738-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator(".topbar-current-version")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
  await page.screenshot({
    animations: "disabled",
    path: "artifacts/version-738-mobile.png",
    fullPage: true,
  });
});
