import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { KeyRound, ShieldCheck, Smartphone, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { api, ApiError, setCsrfToken } from '../../api/client';
import { useLocale } from '../../app/locale';
import { Dialog } from '../../components/ui/Dialog';
import { useConfirm } from '../../components/ui/ConfirmProvider';
import { ErrorState, LoadingState, Section, Timestamp } from '../../components/workflow/Workflow';
import type { SettingsStore } from '../settings/state';
import { registrationOptions, registrationPayload, type CreationOptions } from './webauthn';
import './security.css';

interface Envelope<T> { success: boolean; data: T; message?: string }
interface SecurityStatus { totp_enabled: boolean; recovery_code_count: number; mfa_required: boolean; two_factor_available: boolean; passkeys_available: boolean; csrf: string; passkeys: Array<{ id: number; device_name: string; backed_up: boolean; created_at: string; last_used_at: string | null }> }
interface TotpSetup { secret: string; provisioning_uri: string; qr_code_data_uri: string }
const base = '/api/v1/webui/security';

export default function SecurityPanel({ store, enrollmentOnly = false, onEnrolled }: { store?: SettingsStore; enrollmentOnly?: boolean; onEnrolled?: () => void }) {
  const { t } = useLocale();
  const confirm = useConfirm();
  const client = useQueryClient();
  const [setup, setSetup] = useState<TotpSetup | null>(null);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [codeAction, setCodeAction] = useState<'disable' | 'recover' | null>(null);
  const [code, setCode] = useState('');
  const [deviceName, setDeviceName] = useState('');
  const controller = useRef<AbortController | null>(null);
  const status = useQuery({ queryKey: ['personal-security'], queryFn: async () => { const result = await api<Envelope<SecurityStatus>>(base); setCsrfToken(result.data.csrf); return result.data; } });
  const mutation = useMutation({ mutationFn: (operation: () => Promise<void>) => operation(), gcTime: 0 });
  useEffect(() => { store?.setExternalDirty('security', !!setup || !!codes || !!code || !!deviceName); return () => store?.setExternalDirty('security', false); }, [store, setup, codes, code, deviceName]);
  useEffect(() => {
    const clear = () => { setSetup(null); setCodes(null); setCode(''); controller.current?.abort(); };
    window.addEventListener('sakura:session-expired', clear);
    return () => { controller.current?.abort(); window.removeEventListener('sakura:session-expired', clear); };
  }, []);
  async function run(operation: () => Promise<void>) {
    try { await mutation.mutateAsync(operation); await client.invalidateQueries({ queryKey: ['personal-security'] }); }
    catch (error) {
      const detail = error instanceof ApiError && typeof error.body === 'object' && error.body !== null && 'detail' in error.body ? String(error.body.detail) : '';
      toast.error(detail === 'invalid_code' ? t('验证码无效或已经使用，请输入新验证码。', 'The code is invalid or already used. Enter a new code.') : detail === 'setup_expired' ? t('设置已过期，请重新开始。', 'Setup expired. Please start again.') : error instanceof DOMException && error.name === 'NotAllowedError' ? t('通行密钥操作已取消或超时。', 'The passkey request was cancelled or timed out.') : t('操作失败，请重试。', 'Unable to complete this action. Please try again.'));
    } finally { mutation.reset(); }
  }
  async function send<T>(path: string, body?: unknown): Promise<T> {
    const result = await api<Envelope<T>>(base + path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) });
    if (!result.success) throw new Error('Security operation failed');
    return result.data;
  }
  function submitCode(event: FormEvent) {
    event.preventDefault();
    const value = code; setCode('');
    void run(async () => {
      if (setup) { const result = await send<{ recovery_codes: string[] }>('/totp/enable', { code: value }); setSetup(null); setCodes(result.recovery_codes); }
      else if (codeAction === 'recover') { const result = await send<{ recovery_codes: string[] }>('/recovery-codes', { code: value }); setCodes(result.recovery_codes); setCodeAction(null); }
      else { await send('/totp/disable', { code: value }); setCodeAction(null); toast.success(t('身份验证器已关闭', 'Authenticator disabled')); }
    });
  }
  async function registerPasskey(event: FormEvent) {
    event.preventDefault();
    await run(async () => {
      controller.current?.abort(); controller.current = new AbortController();
      const data = await send<{ challenge_id: string; public_key: CreationOptions }>('/passkeys/options');
      const credential = await navigator.credentials.create({ publicKey: registrationOptions(data.public_key), signal: controller.current.signal });
      if (!(credential instanceof PublicKeyCredential)) throw new Error('No credential returned');
      await send('/passkeys/verify', { challenge_id: data.challenge_id, credential: registrationPayload(credential), device_name: deviceName.trim() || t('我的通行密钥', 'My passkey') });
      setDeviceName(''); toast.success(t('通行密钥已添加', 'Passkey added')); onEnrolled?.();
    });
  }
  if (status.isPending) return <LoadingState />;
  if (status.isError) return <ErrorState retry={() => status.refetch()} />;
  const data = status.data;
  const passkeySupported = typeof PublicKeyCredential !== 'undefined' && !!navigator.credentials && window.isSecureContext;
  return <div className="security-panel">
    {data.mfa_required && <div className="security-notice" role="status"><ShieldCheck size={20} />{t('请先添加身份验证器或通行密钥，再继续使用工作空间。', 'Add an authenticator or passkey to continue to your workspace.')}</div>}
    <Section title={t('身份验证器', 'Authenticator app')} aside={<ShieldCheck size={20} />}>
      <div className="security-row"><div><h3>{data.totp_enabled ? t('两步验证已开启', 'Two-step verification is on') : t('为登录增加一道保护', 'Add protection to your sign-in')}</h3><p>{t('使用 Google Authenticator 等应用生成一次性验证码。', 'Use an app such as Google Authenticator to generate one-time codes.')}</p></div>
        {!data.totp_enabled && <button className="button primary" disabled={mutation.isPending || !data.two_factor_available} onClick={() => void run(async () => setSetup(await send<TotpSetup>('/totp/setup')))}><Smartphone size={16} />{t('设置身份验证器', 'Set up authenticator')}</button>}
        {data.totp_enabled && !enrollmentOnly && <button className="button" disabled={mutation.isPending} onClick={() => setCodeAction('disable')}>{t('关闭', 'Turn off')}</button>}</div>
      {data.totp_enabled && !enrollmentOnly && <div className="security-row"><div><h3>{t('恢复码', 'Recovery codes')}</h3><p>{t("剩余 {0} 个。重新生成会使旧恢复码失效。", "{0} remaining. Generating new codes invalidates the previous codes.", [data.recovery_code_count])}</p></div><button className="button" disabled={mutation.isPending} onClick={() => setCodeAction('recover')}>{t('重新生成', 'Generate new codes')}</button></div>}
    </Section>
    <Section title={t('通行密钥', 'Passkeys')} aside={<KeyRound size={20} />}>
      <p className="muted">{t('使用设备上的指纹、面容或安全密钥登录。', 'Sign in with your device fingerprint, face recognition, or security key.')}</p>
      {data.passkeys.map(key => <div className="security-row" key={key.id}><div><h3>{key.device_name}</h3><p>{t('上次使用：', 'Last used: ')}<Timestamp value={key.last_used_at} /></p></div>{!enrollmentOnly && <button className="button" disabled={mutation.isPending} aria-label={t("删除 {0}", "Delete {0}", [key.device_name])} onClick={async () => { if (await confirm({ title: t('删除通行密钥？', 'Delete passkey?'), description: t('删除后将无法再用这把密钥登录。', 'You will no longer be able to sign in with this key.'), destructive: true })) void run(async () => { await api(base + `/passkeys/${key.id}`, { method: 'DELETE' }); }); }}><Trash2 size={16} /></button>}</div>)}
      {data.passkeys.length === 0 && <p className="muted">{t('尚未添加通行密钥。', 'No passkeys added yet.')}</p>}
      {!passkeySupported && <p className="security-notice">{t('请在支持通行密钥的浏览器中，通过 HTTPS 访问此页面。', 'Open this page over HTTPS in a browser that supports passkeys.')}</p>}
      <form className="security-inline" onSubmit={registerPasskey}><label>{t('设备名称', 'Device name')}<input value={deviceName} onChange={e => setDeviceName(e.target.value)} maxLength={100} placeholder={t('例如：我的笔记本', 'For example: My laptop')} /></label><button className="button primary" disabled={mutation.isPending || !passkeySupported || !data.passkeys_available} type="submit">{t('添加通行密钥', 'Add passkey')}</button></form>
    </Section>
    <Dialog open={!!setup || !!codeAction} onOpenChange={open => { if (!open && !mutation.isPending) { setSetup(null); setCodeAction(null); setCode(''); } }} title={setup ? t('连接身份验证器', 'Connect your authenticator') : codeAction === 'disable' ? t('关闭身份验证器', 'Turn off authenticator') : t('生成新的恢复码', 'Generate new recovery codes')}>
      {setup && <div className="security-setup"><p>{t('用身份验证器扫描二维码，然后输入应用中显示的验证码。', 'Scan the QR code with your authenticator, then enter the code shown in the app.')}</p><img src={setup.qr_code_data_uri} alt={t('身份验证器设置二维码', 'Authenticator setup QR code')} width={200} height={200} /><details><summary>{t('无法扫描？手动输入设置密钥', 'Cannot scan? Enter the setup key manually')}</summary><code>{setup.secret}</code></details></div>}
      <form onSubmit={submitCode} className="security-form"><label>{codeAction === 'disable' ? t('验证码或恢复码', 'Authenticator or recovery code') : t('验证码', 'Verification code')}<input autoFocus autoComplete="one-time-code" inputMode={codeAction === 'disable' ? 'text' : 'numeric'} required value={code} onChange={event => setCode(event.target.value)} /></label><button className="button primary" disabled={mutation.isPending || !code.trim()}>{t('确认', 'Confirm')}</button></form>
    </Dialog>
    <Dialog open={!!codes} onOpenChange={open => { if (!open) { setCodes(null); onEnrolled?.(); } }} title={t('保存恢复码', 'Save your recovery codes')} description={t('恢复码只显示这一次。请将它们保存到安全的地方，每个恢复码只能使用一次。', 'These codes are shown only once. Save them somewhere safe. Each code can be used once.')}><div className="recovery-codes">{codes?.map(value => <code key={value}>{value}</code>)}</div><button className="button primary" onClick={() => { setCodes(null); onEnrolled?.(); }}>{t('我已保存', 'I have saved these codes')}</button></Dialog>
  </div>;
}
