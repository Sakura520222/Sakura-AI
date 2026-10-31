import { expect, test } from '@playwright/test';

test('public sign-in keeps the original brand and reports a rejected MFA code', async ({ page }) => {
  await page.route('**/api/v1/webui/auth/state', route => route.fulfill({ json: { csrf: 'fixture-csrf', timezone: 'Asia/Shanghai', github_available: true, passkeys_available: true, authenticated: false, enrollment_required: false, mfa_pending: true, totp_enabled: true, username: 'alice' } }));
  await page.route('**/api/v1/webui/auth/mfa/totp', async route => {
    expect(route.request().postDataJSON()).toEqual({ code: '123456' });
    expect(route.request().headers()['x-csrf-token']).toBe('fixture-csrf');
    await route.fulfill({ status: 400, json: { success: false, data: null } });
  });
  await page.goto('/app/login');
  await expect(page.locator('.public-auth-brand .sakura-brand-mark')).toHaveText('🌸');
  await expect(page.locator('link[rel="icon"]')).toHaveAttribute('href', 'data:,');
  await page.getByLabel('验证码或恢复码').fill('123456');
  await page.getByRole('button', { name: '验证并登录' }).click();
  await expect(page.getByRole('alert')).toHaveText(/登录验证失败/);
  await expect(page.getByLabel('验证码或恢复码')).toHaveValue('');
  await expect(page.locator('.app-shell')).toHaveCount(0);
});

test('skills upload, disable and delete persist through the real local service', async ({ page }) => {
  await page.goto('/app/agent/skills');
  await page.getByRole('button', { name: '安装技能', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('安装方式').selectOption('upload');
  await dialog.getByLabel('SKILL.md 或 ZIP 文件').setInputFiles({ name: 'SKILL.md', mimeType: 'text/markdown', buffer: Buffer.from('---\nname: Browser fixture\ndescription: For isolated browser testing\n---\n# Browser fixture\nCheck fixture data.\n') });
  await dialog.getByRole('button', { name: '安装', exact: true }).click();
  const row = page.locator('article').filter({ has: page.getByRole('heading', { name: 'Browser fixture', exact: true }) });
  await expect(row).toBeVisible();
  await row.getByRole('button', { name: '停用 Browser fixture', exact: true }).click();
  await expect(row.getByText('已停用', { exact: true })).toBeVisible();
  await page.reload();
  await expect(row.getByText('已停用', { exact: true })).toBeVisible();
  await row.getByRole('button', { name: '删除 Browser fixture', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '确认', exact: true }).click();
  await expect(row).toHaveCount(0);
});

test('scan reports preserve a zero score and queue retry changes the real record', async ({ page }) => {
  await page.goto('/app/scans');
  await expect(page.locator('.scan-stats')).toContainText('平均健康评分 0');
  await page.getByRole('link', { name: 'owner/browser-fixture', exact: true }).click();
  await expect(page.getByText('Fixture finding', { exact: true })).toBeVisible();
  await page.getByRole('combobox', { name: '严重程度' }).selectOption('critical');
  await expect(page.getByRole('heading', { name: '暂无符合筛选条件的发现' })).toBeVisible();
  await page.goto('/app/queue');
  const row = page.getByRole('row').filter({ hasText: 'owner/browser-fixture #42' });
  await row.getByRole('button', { name: '重试', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '确认', exact: true }).click();
  await expect(row.getByText('等待中', { exact: true })).toBeVisible();
  await page.reload();
  await expect(row.getByText('等待中', { exact: true })).toBeVisible();
});

test('virtual passkey registration and sign-in use real browser assertions', async ({ page, context }) => {
  const cdp = await context.newCDPSession(page);
  await cdp.send('WebAuthn.enable');
  const { authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', { options: { protocol: 'ctap2', transport: 'internal', hasResidentKey: true, hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true } });
  try {
    await page.goto('http://localhost:8768/app/settings/personal?tab=security');
    await page.getByLabel('设备名称', { exact: true }).fill('Browser test passkey');
    await page.getByRole('button', { name: '添加通行密钥', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Browser test passkey', exact: true })).toBeVisible();
    await context.clearCookies();
    await page.goto('http://localhost:8768/app/login');
    await expect(page.getByRole('heading', { name: '登录 Sakura AI' })).toBeVisible();
    const response = page.waitForResponse(value => value.url().endsWith('/api/v1/webui/auth/passkeys/verify'));
    await page.getByRole('button', { name: '使用通行密钥', exact: true }).click();
    expect((await response).ok()).toBeTruthy();
    await expect(page).toHaveURL('http://localhost:8768/app/');
    const state = await page.evaluate(async () => (await fetch('/api/v1/webui/auth/state')).json());
    expect(state.authenticated).toBe(true);
  } finally {
    await cdp.send('WebAuthn.removeVirtualAuthenticator', { authenticatorId });
    await cdp.detach();
  }
});
