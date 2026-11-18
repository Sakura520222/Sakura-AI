import { defineConfig } from '@playwright/test';

const port = Number(process.env.SAKURA_E2E_PORT ?? 8768);
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './e2e',
  workers: 1,
  timeout: 30_000,
  use: { baseURL, headless: true, screenshot: 'only-on-failure', trace: 'retain-on-failure' },
  webServer: {
    command: process.env.SAKURA_E2E_PYTHON
      ? `"${process.env.SAKURA_E2E_PYTHON}" -m uvicorn tests.webui_v2_app:app --host 127.0.0.1 --port ${port}`
      : `uv run --no-sync uvicorn tests.webui_v2_app:app --host 127.0.0.1 --port ${port}`,
    cwd: '..',
    url: `${baseURL}/api/v1/webui/bootstrap`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
