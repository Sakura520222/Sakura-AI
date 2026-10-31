import { type ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api/client';
import { LocaleProvider } from '../../app/locale';
import { ConfirmProvider } from '../../components/ui/ConfirmProvider';
import SecurityPanel from './SecurityPanel';
import { decodeBase64Url, encodeBase64Url, registrationOptions, registrationPayload } from './webauthn';

vi.mock('../../api/client', async importOriginal => ({ ...(await importOriginal<object>()), api: vi.fn() }));
const status = { totp_enabled: false, recovery_code_count: 0, mfa_required: false, two_factor_available: true, passkeys_available: true, passkeys: [], csrf: 'csrf' };
function renderPanel(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><LocaleProvider initial="en"><ConfirmProvider>{children}</ConfirmProvider></LocaleProvider></QueryClientProvider>);
  return client;
}
beforeEach(() => { vi.mocked(api).mockReset(); localStorage.clear(); });

describe('authenticator enrollment', () => {
  it('shows one-time setup/recovery data without storing it in query or mutation results', async () => {
    vi.mocked(api).mockImplementation(async path => {
      if (path.endsWith('/totp/setup')) return { success: true, data: { secret: 'ONETIMESECRET', provisioning_uri: 'otpauth://totp/example', qr_code_data_uri: 'data:image/png;base64,aA==' } };
      if (path.endsWith('/totp/enable')) return { success: true, data: { recovery_codes: ['ONE-TIME-CODE'] } };
      return { success: true, data: status };
    });
    const user = userEvent.setup(); const client = renderPanel(<SecurityPanel />);
    await user.click(await screen.findByRole('button', { name: 'Set up authenticator' }));
    await screen.findByRole('img', { name: 'Authenticator setup QR code' });
    await user.type(screen.getByLabelText('Verification code'), '123456');
    await user.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(await screen.findByText('ONE-TIME-CODE')).toBeInTheDocument();
    expect(screen.queryByText('ONETIMESECRET')).not.toBeInTheDocument();
    expect(JSON.stringify(client.getQueryCache().getAll().map(query => query.state.data))).not.toMatch(/ONETIMESECRET|ONE-TIME-CODE|123456/);
    await user.click(screen.getByRole('button', { name: 'I have saved these codes' }));
    expect(screen.queryByText('ONE-TIME-CODE')).not.toBeInTheDocument();
    await waitFor(() => expect(client.getMutationCache().getAll()).toHaveLength(0));
  });

  it('clears setup secrets when the session expires', async () => {
    vi.mocked(api).mockImplementation(async path => path.endsWith('/totp/setup') ? { success: true, data: { secret: 'PRIVATESETUP', provisioning_uri: '', qr_code_data_uri: 'data:image/png;base64,aA==' } } : { success: true, data: status });
    const user = userEvent.setup(); renderPanel(<SecurityPanel />);
    await user.click(await screen.findByRole('button', { name: 'Set up authenticator' }));
    await screen.findByRole('img', { name: 'Authenticator setup QR code' });
    window.dispatchEvent(new Event('sakura:session-expired'));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });
});

it('preserves WebAuthn challenge bytes and attestation transport', () => {
  const encoded = 'AP8B';
  expect(Array.from(new Uint8Array(decodeBase64Url(encoded)))).toEqual([0, 255, 1]);
  expect(encodeBase64Url(decodeBase64Url(encoded))).toBe(encoded);
  const result = registrationOptions({ challenge: encoded, rp: { name: 'Sakura' }, user: { id: encoded, name: 'alice', displayName: 'Alice' }, pubKeyCredParams: [{ type: 'public-key', alg: -7 }], excludeCredentials: [{ id: encoded, type: 'public-key' }] });
  expect(result.challenge).toEqual(decodeBase64Url(encoded));
  expect(result.user.id).toEqual(decodeBase64Url(encoded));
  const credential = { id: 'credential', type: 'public-key', rawId: decodeBase64Url(encoded), response: { attestationObject: decodeBase64Url(encoded), clientDataJSON: decodeBase64Url(encoded), getTransports: () => ['internal'] } } as unknown as PublicKeyCredential;
  expect(registrationPayload(credential)).toEqual({ id: 'credential', type: 'public-key', rawId: encoded, response: { attestationObject: encoded, clientDataJSON: encoded, transports: ['internal'] } });
});
