import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  workers: 1,
  timeout: 30_000,
  use: { baseURL: 'http://127.0.0.1:8768', headless: true, screenshot: 'only-on-failure', trace: 'retain-on-failure' },
  webServer: {
    command: process.env.SAKURA_E2E_PYTHON
      ? `${process.env.SAKURA_E2E_PYTHON} -m uvicorn tests.webui_v2_app:app --host 127.0.0.1 --port 8768`
      : 'uv run --no-sync uvicorn tests.webui_v2_app:app --host 127.0.0.1 --port 8768',
    cwd: '..',
    url: 'http://127.0.0.1:8768/api/v1/webui/bootstrap',
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
