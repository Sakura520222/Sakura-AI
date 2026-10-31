import { expect, test } from '@playwright/test';

test('settings persist through the real API, guard dirty navigation, and reset', async ({ page }) => {
  await page.goto('/app/settings/review');
  const row = page.locator('[id="setting-global/max_concurrent_reviews"]');
  const input = row.getByRole('spinbutton');
  await expect(input).toBeVisible();
  await input.fill('7');
  await page.getByRole('link', { name: '概览', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: '取消', exact: true }).click();
  await expect(input).toHaveValue('7');
  const saved = page.waitForResponse(response => response.url().endsWith('/api/v1/config-center') && response.request().method() === 'PATCH');
  await row.getByRole('button', { name: '保存', exact: true }).click();
  expect((await saved).ok()).toBeTruthy();
  await page.reload();
  await expect(input).toHaveValue('7');
  await row.getByText('详情与默认值', { exact: true }).click();
  await row.getByRole('button', { name: /恢复默认|重置/ }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button').last().click();
  await expect(input).toHaveValue('5');
});

test('conflict keeps the local draft and offers an explicit reload', async ({ page }) => {
  await page.goto('/app/settings/review');
  const row = page.locator('[id="setting-global/max_concurrent_reviews"]');
  await row.getByRole('spinbutton').fill('8');
  await page.route('**/api/v1/config-center', async route => {
    if (route.request().method() === 'PATCH') await route.fulfill({ status: 409, json: { error: { code: 'conflict', field: 'global/max_concurrent_reviews', detail: null } } });
    else await route.continue();
  });
  await row.getByRole('button', { name: '保存', exact: true }).click();
  await expect(row.getByRole('alert')).toBeVisible();
  await expect(row.getByRole('spinbutton')).toHaveValue('8');
  await expect(row.getByRole('button', { name: '采用服务端值' })).toBeVisible();
});

test('cross-category search and deep links locate a setting', async ({ page }) => {
  await page.goto('/app/settings/personal');
  await page.getByRole('textbox', { name: '搜索所有设置' }).fill('max_concurrent_reviews');
  await page.locator('.settings-search-result').first().click();
  await expect(page).toHaveURL(/settings\/review#global%2Fmax_concurrent_reviews/);
  await expect(page.locator('[id="setting-global/max_concurrent_reviews"]')).toBeVisible();
});

test('ordinary users cannot see administrator navigation or settings', async ({ context, page }) => {
  await context.addCookies([{ name: 'test-role', value: 'user', url: 'http://127.0.0.1:8768' }]);
  await page.goto('/app/settings/personal');
  await expect(page.getByRole('heading', { name: '设置', exact: true })).toBeVisible();
  await expect(page.locator('.primary-nav').getByRole('link', { name: '仓库', exact: true })).toHaveCount(0);
  await expect(page.locator('.settings-sidebar').getByText('AI 与模型', { exact: true })).toHaveCount(0);
  await page.goto('/app/settings/ai');
  await expect(page.getByRole('heading', { name: '此设置不可用', exact: true })).toBeVisible();
});

test('dark mode, command search, and narrow navigation remain usable', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/app/settings/review');
  await expect(page.getByRole('heading', { name: '设置', exact: true })).toBeVisible();
  await expect(page.locator('[id="setting-global/max_concurrent_reviews"]').getByRole('spinbutton')).toBeVisible();
  await page.screenshot({ path: 'artifacts/settings-desktop.png' });
  await page.locator('.user-trigger').click();
  await page.getByRole('menuitem', { name: '深色', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.screenshot({ path: 'artifacts/settings-dark.png' });
  await page.keyboard.press('Control+k');
  await expect(page.getByRole('dialog', { name: '快速查找' })).toBeVisible();
  await page.keyboard.press('Escape');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: '打开导航' }).click();
  await expect(page.locator('.sidebar')).toHaveClass(/open/);
  // Click the visible part of the backdrop, outside the open navigation drawer.
  await page.getByRole('button', { name: '关闭导航', exact: true }).click({ position: { x: 380, y: 20 } });
  await expect.poll(async () => page.locator('.sidebar').evaluate(element => element.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await page.screenshot({ path: 'artifacts/settings-mobile.png' });
});

test('expired session exposes login without retaining private workspace data', async ({ page }) => {
  await page.route('**/api/v1/webui/bootstrap', route => route.fulfill({ status: 401, json: { detail: 'Not authenticated' } }));
  await page.goto('/app/settings/review');
  await expect(page.getByRole('link', { name: '登录 / Sign in' })).toBeVisible();
  await expect(page.locator('.app-shell')).toHaveCount(0);
});
