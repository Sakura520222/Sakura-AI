import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, expect, it, vi } from 'vitest';
import { api } from '../../api/client';
import { LocaleProvider } from '../../app/locale';
import { ConfirmProvider } from '../../components/ui/ConfirmProvider';
import NotificationsPanel from './NotificationsPanel';
import NotificationsPage, { safeDetailPath } from './NotificationsPage';

vi.mock('../../api/client', async importOriginal => ({ ...(await importOriginal<object>()), api: vi.fn() }));
function renderPanel(page = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const content = page
    ? <MemoryRouter initialEntries={['/notifications']}><NotificationsPage /></MemoryRouter>
    : <NotificationsPanel />;
  return render(<QueryClientProvider client={client}><LocaleProvider initial="en"><ConfirmProvider>{content}</ConfirmProvider></LocaleProvider></QueryClientProvider>);
}
beforeEach(() => { vi.mocked(api).mockReset(); localStorage.clear(); });

it('creates an exact email endpoint using the versioned client without an owner override', async () => {
  vi.mocked(api).mockImplementation(async path => path.endsWith('/settings') ? { providers: [{ channel: 'email', capabilities: [] }], event_types: ['agent.action_required'], endpoints: [], subscriptions: [] } : { id: 1, provider: 'email', verified: false });
  const user = userEvent.setup(); renderPanel();
  await user.click(await screen.findByRole('button', { name: 'Add channel' }));
  await user.type(screen.getByLabelText('Name', { exact: true }), 'Private email');
  await user.type(screen.getByLabelText('Email address'), 'alice@example.test');
  await user.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/v1/notifications/endpoints', expect.objectContaining({ method: 'POST', body: JSON.stringify({ name: 'Private email', provider: 'email', address: 'alice@example.test', signing_secret: '' }) })));
});

it('keeps notification reading in the unified inbox API', async () => {
  vi.mocked(api).mockImplementation(async path => path.includes('/history') ? { items: [{ id: 2, type: 'agent.action_required', title: 'Task needs input', content: 'Review the result', severity: 'warning', detail_url: '/agent-team/?task_id=2', read: false, occurred_at: '2026-10-29T10:00:00Z' }], page: 1, unread: 1, has_next: false } : { ok: true });
  const user = userEvent.setup(); renderPanel(true);
  await screen.findByText('Task needs input');
  await user.click(screen.getByRole('button', { name: 'Mark all read' }));
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/v1/notifications/read-all', { method: 'POST' }));
});

it('rejects external and browser-normalized cross-origin detail destinations', () => {
  expect(safeDetailPath('https://external.test')).toBeUndefined();
  expect(safeDetailPath('//external.test')).toBeUndefined();
  expect(safeDetailPath('/\\external.test')).toBeUndefined();
  expect(safeDetailPath('/\n/external.test')).toBeUndefined();
  expect(safeDetailPath('/agent-team/?task_id=2')).toBe('/agent-team/?task_id=2');
});
