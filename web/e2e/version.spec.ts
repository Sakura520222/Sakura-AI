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
