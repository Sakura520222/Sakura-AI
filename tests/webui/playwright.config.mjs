import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: ".",
  testMatch: "settings.spec.mjs",
  workers: 1,
  timeout: 30000,
  use: {
    baseURL: "http://127.0.0.1:8767",
    headless: true,
    channel: process.env.PLAYWRIGHT_CHANNEL || "chromium",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: process.env.SAKURA_E2E_PYTHON
      ? `${process.env.SAKURA_E2E_PYTHON} -m uvicorn tests.config_center_app:app --host 127.0.0.1 --port 8767`
      : "uv run uvicorn tests.config_center_app:app --host 127.0.0.1 --port 8767",
    cwd: "../..",
    url: "http://127.0.0.1:8767/api/v1/config-center?search=true",
    reuseExistingServer: false,
    timeout: 60000,
  },
});
