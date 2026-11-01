import { expect, test } from "@playwright/test";

// This browser scenario uses the actual SPA and authenticated app shell, but a
// stateful route fixture for notification APIs. It validates API contracts and
// UI persistence; it does not exercise provider delivery or SMTP/Telegram.
test("mocked notification API supports endpoint rename, preferences, and inbox read state", async ({
  page,
}) => {
  const endpoint = {
    id: 1,
    provider: "email",
    name: "Personal email",
    address: "alice@example.test",
    enabled: true,
    verified: true,
    credential_configured: false,
  };
  const endpointState = { ...endpoint };
  const subscriptions: Array<{
    id: number;
    event_type: string;
    endpoint_id: number | null;
    repo_name: string | null;
    resource_type: string | null;
    resource_id: string | null;
    min_severity: string;
    enabled: boolean;
  }> = [];
  let notificationRead = false;
  let settingsReads = 0;
  const subscriptionBodies: Array<Record<string, unknown>> = [];
  const endpointUpdates: Array<Record<string, unknown>> = [];
  const readRequests: string[] = [];

  await page.addInitScript(() => {
    window.localStorage.setItem("sakura.language", "en");
  });

  await page.route("**/api/v1/notifications/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;

    if (path === "/api/v1/notifications/settings" && request.method() === "GET") {
      settingsReads += 1;
      return route.fulfill({
        json: {
          providers: [
            { channel: "email", capabilities: ["text", "html", "verification_code"] },
            { channel: "webhook", capabilities: ["text", "html"] },
            { channel: "telegram", capabilities: ["text", "html"] },
          ],
          event_types: ["review.completed", "agent.action_required"],
          endpoints: [endpointState],
          subscriptions,
        },
      });
    }

    if (path === "/api/v1/notifications/endpoints/1" && request.method() === "PATCH") {
      expect(request.headers()["x-csrf-token"]).toBeTruthy();
      const body = request.postDataJSON() as Record<string, unknown>;
      endpointUpdates.push(body);
      expect(body).toEqual({ name: "My verified email", enabled: true });
      Object.assign(endpointState, body);
      return route.fulfill({
        json: {
          ...endpointState,
          config_version: 2,
        },
      });
    }

    if (path === "/api/v1/notifications/subscriptions" && request.method() === "POST") {
      expect(request.headers()["x-csrf-token"]).toBeTruthy();
      const body = request.postDataJSON() as Record<string, unknown>;
      subscriptionBodies.push(body);
      expect(body).toEqual({
        event_type: "review.completed",
        endpoint_id: 1,
        min_severity: "info",
        repo_name: "alice/Sakura-AI",
        resource_type: null,
        resource_id: null,
        enabled: true,
      });
      subscriptions.push({ id: 21, ...body } as (typeof subscriptions)[number]);
      return route.fulfill({ json: { id: 21 } });
    }

    if (path === "/api/v1/notifications/history" && request.method() === "GET") {
      expect(url.searchParams.get("page")).toBe("1");
      return route.fulfill({
        json: {
          items: [
            {
              id: 41,
              type: "review.completed",
              title: "Sakura-AI PR #646 review completed",
              content: "The isolated browser fixture has a completed review.",
              severity: "info",
              detail_url: null,
              read: notificationRead,
              occurred_at: "2026-10-29T10:00:00Z",
            },
          ],
          has_next: false,
          page: 1,
          unread: notificationRead ? 0 : 1,
        },
      });
    }

    if (path === "/api/v1/notifications/41/read" && request.method() === "POST") {
      expect(request.headers()["x-csrf-token"]).toBeTruthy();
      expect(request.postData()).toBeNull();
      notificationRead = true;
      readRequests.push(path);
      return route.fulfill({ json: { ok: true } });
    }

    throw new Error(`Unexpected notification API request: ${request.method()} ${path}`);
  });

  await page.goto("/app/notifications/preferences");
  await expect(page.getByRole("heading", { name: "Notification center" })).toBeVisible();
  const endpointRow = page.locator(".notification-endpoint").filter({
    hasText: "Personal email",
  });
  await expect(endpointRow).toContainText("alice@example.test");
  await endpointRow.getByRole("button", { name: "Edit", exact: true }).click();

  const endpointDialog = page.getByRole("dialog", { name: "Edit delivery channel" });
  await endpointDialog.getByLabel("Name", { exact: true }).fill("My verified email");
  await endpointDialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator(".notification-endpoint")).toContainText("My verified email");
  expect(endpointUpdates).toEqual([{ name: "My verified email", enabled: true }]);

  // The same in-memory fixture state is returned on a fresh document load.
  await page.reload();
  await expect(page.locator(".notification-endpoint")).toContainText("My verified email");
  expect(settingsReads).toBeGreaterThanOrEqual(2);

  await page.getByRole("button", { name: "Add preference", exact: true }).click();
  const preferenceDialog = page.getByRole("dialog", {
    name: "Notification preference",
  });
  await expect(preferenceDialog.getByRole("combobox").nth(0)).toHaveValue("");
  await preferenceDialog.getByRole("combobox").nth(0).selectOption("review.completed");
  await preferenceDialog.getByRole("combobox").nth(1).selectOption("1");
  await preferenceDialog.getByLabel("Repository (optional)").fill("alice/Sakura-AI");
  await preferenceDialog.getByRole("button", { name: "Save preference", exact: true }).click();
  await expect(page.locator(".security-row")).toContainText("alice/Sakura-AI");
  await expect(page.locator(".security-row")).toContainText("My verified email");
  expect(subscriptionBodies).toEqual([
    {
      event_type: "review.completed",
      endpoint_id: 1,
      min_severity: "info",
      repo_name: "alice/Sakura-AI",
      resource_type: null,
      resource_id: null,
      enabled: true,
    },
  ]);

  await page.goto("/app/notifications");
  await expect(
    page.getByRole("heading", { name: "Sakura-AI PR #646 review completed", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Mark read", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Mark read", exact: true }).click();
  await expect(page.getByRole("button", { name: "Mark read", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Mark all read", exact: true })).toBeDisabled();
  expect(readRequests).toEqual(["/api/v1/notifications/41/read"]);
});
