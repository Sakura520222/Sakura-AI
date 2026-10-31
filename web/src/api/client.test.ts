import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError, setCsrfToken } from './client';

afterEach(() => { vi.unstubAllGlobals(); setCsrfToken(''); });
describe('same-origin API client', () => {
  it('attaches the CSRF token to a mutation and retains caller cancellation', async () => {
    const request = vi.fn().mockResolvedValue(new Response('{"persisted":true}', { status: 200 }));
    vi.stubGlobal('fetch', request);
    setCsrfToken('test-csrf');
    const controller = new AbortController();
    expect(await api('/api/v1/config-center', { method: 'PATCH', body: '{}', signal: controller.signal })).toEqual({ persisted: true });
    const init = request.mock.calls[0][1] as RequestInit;
    expect(new Headers(init.headers).get('X-CSRF-Token')).toBe('test-csrf');
    expect(init.signal).toBe(controller.signal);
    expect(init.credentials).toBe('same-origin');
  });
  it('rejects foreign destinations before attaching cookies or CSRF', async () => {
    const request = vi.fn(); vi.stubGlobal('fetch', request);
    await expect(api('//foreign.example/api')).rejects.toThrow('same-origin');
    await expect(api('https://foreign.example/api')).rejects.toThrow('same-origin');
    expect(request).not.toHaveBeenCalled();
  });
  it('keeps structured conflict details without echoing them into error text', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"error":{"code":"version_conflict"}}', { status: 409 })));
    await expect(api('/api/v1/config-center')).rejects.toMatchObject({ status: 409, body: { error: { code: 'version_conflict' } } });
  });
  it('notifies the application when a session expires', async () => {
    const expired = vi.fn(); window.addEventListener('sakura:session-expired', expired);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 401 })));
    await expect(api('/api/v1/webui/bootstrap')).rejects.toBeInstanceOf(ApiError);
    expect(expired).toHaveBeenCalledOnce();
    window.removeEventListener('sakura:session-expired', expired);
  });
  it('does not replace multipart boundaries or require a body for logout', async () => {
    const request = vi.fn().mockResolvedValue(new Response(null, { status: 204 })); vi.stubGlobal('fetch', request);
    const body = new FormData(); body.set('file', 'fixture');
    await api('/api/v1/upload', { method: 'POST', body });
    expect(new Headers(request.mock.calls[0][1].headers).has('Content-Type')).toBe(false);
    expect(await api('/api/v1/webui/logout', { method: 'POST' })).toBeNull();
  });
});
