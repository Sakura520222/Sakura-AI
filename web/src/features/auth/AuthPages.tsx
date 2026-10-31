import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowUpRight, Fingerprint, ShieldCheck } from "lucide-react";
import { Navigate } from "react-router-dom";
import { api, ApiError, setCsrfToken } from "../../api/client";
import { LocaleProvider, useLocale } from "../../app/locale";
import { ThemeProvider } from "../../app/theme";
import { ConfirmProvider } from "../../components/ui/ConfirmProvider";
import { ErrorState, LoadingState } from "../../components/common/states";
import SecurityPanel from "../security/SecurityPanel";
import {
  authenticationOptions,
  authenticationPayload,
  type AuthenticationOptions,
} from "../security/webauthn";
import "./auth.css";
import { BrandMark } from "../../components/common/BrandMark";

interface AuthState {
  csrf: string;
  timezone: string;
  github_available: boolean;
  passkeys_available: boolean;
  authenticated: boolean;
  enrollment_required: boolean;
  mfa_pending: boolean;
  totp_enabled: boolean;
  username: string | null;
}
interface Envelope<T> {
  success: boolean;
  data: T;
  message?: string;
}
const base = "/api/v1/webui/auth";
function useAuthState() {
  return useQuery({
    queryKey: ["browser-auth-state"],
    queryFn: async () => {
      const state = await api<AuthState>(base + "/state");
      setCsrfToken(state.csrf);
      return state;
    },
    staleTime: 0,
    retry: false,
  });
}
function PublicFrame({
  children,
  login = false,
}: {
  children: ReactNode;
  login?: boolean;
}) {
  const state = useAuthState();
  return (
    <LocaleProvider initial="zh-CN" timezone={state.data?.timezone}>
      <ThemeProvider initial="system">
        <ConfirmProvider>
          {login ? (
            <div className="login-page">
              <div className="login-frame">
                {children}
                <LoginFooter />
              </div>
            </div>
          ) : (
            <div className="public-auth">
              <header>
                <a href="/app/login" className="public-auth-brand">
                  <BrandMark />
                  Sakura AI
                </a>
                <LanguageSwitch />
              </header>
              {children}
              <footer>
                Sakura AI ·{" "}
                <a
                  href="https://github.com/Sakura520222/Sakura-AI"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  GitHub
                </a>
              </footer>
            </div>
          )}
        </ConfirmProvider>
      </ThemeProvider>
    </LocaleProvider>
  );
}
function LoginFooter() {
  const { t, language, setLanguage } = useLocale();
  return (
    <footer className="login-footer">
      <select
        className="login-language"
        aria-label={t("语言", "Language")}
        value={language}
        onChange={(event) =>
          setLanguage(event.target.value === "en" ? "en" : "zh-CN")
        }
      >
        <option value="zh-CN">简体中文</option>
        <option value="en">English</option>
      </select>
      <nav aria-label={t("相关链接", "Useful links")}>
        <a
          href="https://github.com/Sakura520222/Sakura-AI"
          target="_blank"
          rel="noreferrer"
        >
          GitHub
          <ArrowUpRight size={12} />
        </a>
        <a href="/app/legal/privacy">{t("隐私", "Privacy")}</a>
        <a href="/app/legal/terms">{t("条款", "Terms")}</a>
      </nav>
    </footer>
  );
}

function GitHubMark() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="21"
      height="21"
      fill="currentColor"
      aria-hidden="true"
    >
      <path d="M12 .297C5.37.297 0 5.67 0 12.297c0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.043-1.61-4.043-1.61-.546-1.387-1.333-1.756-1.333-1.756-1.09-.745.083-.73.083-.73 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.807 1.305 3.492.998.108-.776.42-1.305.763-1.605-2.665-.304-5.467-1.334-5.467-5.931 0-1.31.469-2.381 1.236-3.221-.124-.303-.536-1.524.118-3.176 0 0 1.008-.322 3.301 1.23a11.52 11.52 0 0 1 3.003-.404c1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.656 1.652.244 2.873.12 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222 0 1.606-.015 2.898-.015 3.293 0 .322.216.694.825.576C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12" />
    </svg>
  );
}
function LanguageSwitch() {
  const { language, setLanguage } = useLocale();
  return (
    <button
      className="button"
      onClick={() => setLanguage(language === "en" ? "zh-CN" : "en")}
    >
      {language === "en" ? "中文" : "English"}
    </button>
  );
}
export function PublicLogin() {
  return (
    <PublicFrame login>
      <Login />
    </PublicFrame>
  );
}
export function PublicSecurity() {
  return (
    <PublicFrame>
      <Enrollment />
    </PublicFrame>
  );
}

function Login() {
  const { t } = useLocale();
  const query = useAuthState();
  const [code, setCode] = useState("");
  const [error, setError] = useState(() =>
    new URLSearchParams(window.location.search).get("error") === "oauth_failed"
      ? t(
          "GitHub 登录未完成，请重新授权。",
          "GitHub sign-in did not complete. Please authorize again.",
        )
      : "",
  );
  const controller = useRef<AbortController | null>(null);
  const mutation = useMutation({
    mutationFn: (operation: () => Promise<void>) => operation(),
    gcTime: 0,
  });
  useEffect(() => () => controller.current?.abort(), []);
  async function run(operation: () => Promise<void>) {
    setError("");
    try {
      await mutation.mutateAsync(operation);
      window.location.assign("/app/");
    } catch (failure) {
      setError(
        failure instanceof ApiError && failure.status === 429
          ? t(
              "尝试次数过多，请稍后再试。",
              "Too many attempts. Please try again later.",
            )
          : failure instanceof DOMException &&
              failure.name === "NotAllowedError"
            ? t(
                "通行密钥操作已取消或超时。",
                "The passkey request was cancelled or timed out.",
              )
            : t(
                "登录验证失败，请重试或重新使用 GitHub 登录。",
                "Verification failed. Try again or sign in with GitHub again.",
              ),
      );
    } finally {
      mutation.reset();
    }
  }
  async function request<T>(path: string, body?: unknown) {
    const result = await api<Envelope<T>>(base + path, {
      method: "POST",
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!result.success) throw new Error("Verification failed");
    return result.data;
  }
  function totpLogin(event: FormEvent) {
    event.preventDefault();
    const value = code;
    setCode("");
    void run(async () => {
      await request("/mfa/totp", { code: value });
    });
  }
  function passkeyLogin() {
    void run(async () => {
      controller.current?.abort();
      controller.current = new AbortController();
      const prefix = query.data?.mfa_pending ? "/mfa/passkeys" : "/passkeys";
      const options = await request<{
        challenge_id: string;
        public_key: AuthenticationOptions;
      }>(prefix + "/options");
      const credential = await navigator.credentials.get({
        publicKey: authenticationOptions(options.public_key),
        signal: controller.current.signal,
      });
      if (!(credential instanceof PublicKeyCredential))
        throw new Error("No assertion");
      await request(prefix + "/verify", {
        challenge_id: options.challenge_id,
        credential: authenticationPayload(credential),
      });
    });
  }
  if (query.isPending)
    return (
      <main className="login-panel login-state">
        <BrandMark size={32} />
        <LoadingState />
      </main>
    );
  if (query.isError)
    return (
      <main className="login-panel login-state">
        <BrandMark size={32} />
        <ErrorState retry={() => query.refetch()} />
      </main>
    );
  if (query.data.authenticated)
    return (
      <Navigate
        to={query.data.enrollment_required ? "/security" : "/"}
        replace
      />
    );
  const mfa = query.data.mfa_pending;
  const passkeySupported =
    typeof PublicKeyCredential !== "undefined" &&
    !!navigator.credentials &&
    window.isSecureContext;
  return (
    <main className="login-panel">
      <section className="login-intro" aria-labelledby="login-title">
        <a className="login-brand" href="/app/login">
          <BrandMark size={28} />
          <span>Sakura AI</span>
        </a>
        <h1 id="login-title">
          {mfa ? (
            t("确认是你本人", "Verify it's you")
          ) : (
            <>
              <span>{t("登录你的", "Sign in to")}</span>
              <span>{t("工作空间", "your workspace")}</span>
            </>
          )}
        </h1>
        <p className="login-description">
          {mfa
            ? t(
                "再完成一步验证，即可继续。",
                "One more step to securely continue.",
              )
            : t(
                "代码审查、Issue 分析与 Agent 任务，从这里继续。",
                "Pick up where you left off with your reviews, issues and Agent tasks.",
              )}
        </p>
        {mfa && query.data.username && (
          <div className="login-identity">
            <ShieldCheck size={16} />
            <span>{query.data.username}</span>
          </div>
        )}
      </section>
      <section
        className="login-options"
        aria-label={t("登录方式", "Sign-in options")}
      >
        <div className="login-options-heading">
          <h2>
            {mfa
              ? t("两步验证", "Two-step verification")
              : t("欢迎回来", "Welcome back")}
          </h2>
          <p>
            {mfa
              ? t(
                  "使用你为账号设置的验证方式。",
                  "Use a verification method set up for your account.",
                )
              : t("选择一种方式继续", "Choose how to continue")}
          </p>
        </div>
        {error && (
          <p className="public-auth-error" role="alert">
            {error}
          </p>
        )}
        {!mfa && query.data.github_available && (
          <a className="login-button login-button-main" href={base + "/github"}>
            <GitHubMark />
            <span>{t("使用 GitHub 继续", "Continue with GitHub")}</span>
            <ArrowUpRight size={17} className="login-action-arrow" />
          </a>
        )}
        {mfa && query.data.totp_enabled && (
          <form className="login-code-form" onSubmit={totpLogin}>
            <label htmlFor="login-code">
              {t("验证码或恢复码", "Authenticator or recovery code")}
            </label>
            <input
              id="login-code"
              autoFocus
              required
              autoComplete="one-time-code"
              autoCapitalize="off"
              spellCheck={false}
              value={code}
              onChange={(event) => setCode(event.target.value)}
            />
            <button
              className="login-button login-button-main"
              disabled={mutation.isPending || !code.trim()}
            >
              {mutation.isPending
                ? t("正在验证…", "Verifying…")
                : t("验证并登录", "Verify and sign in")}
            </button>
          </form>
        )}
        {query.data.passkeys_available && (
          <>
            {((!mfa && query.data.github_available) ||
              (mfa && query.data.totp_enabled)) && (
              <div className="login-divider">
                <span>{t("或", "or")}</span>
              </div>
            )}
            <button
              className={`login-button ${!query.data.github_available && !query.data.totp_enabled ? "login-button-main" : "login-button-secondary"}`}
              disabled={mutation.isPending || !passkeySupported}
              onClick={passkeyLogin}
            >
              <Fingerprint size={21} />
              <span>
                {mutation.isPending && !code
                  ? t("正在验证…", "Verifying…")
                  : t("使用通行密钥", "Use a passkey")}
              </span>
            </button>
            <p className="login-passkey-note">
              {passkeySupported
                ? t(
                    "通过指纹、面容或设备密码登录。",
                    "Sign in with your fingerprint, face or device PIN.",
                  )
                : t(
                    "通行密钥需要支持的浏览器与 HTTPS 连接。",
                    "Passkeys require a supported browser and an HTTPS connection.",
                  )}
            </p>
          </>
        )}
        {mfa && query.data.github_available && (
          <a className="login-restart" href={base + "/github"}>
            {t("换个账号登录", "Use a different account")}
            <ArrowUpRight size={14} />
          </a>
        )}
        {!query.data.github_available &&
          !query.data.passkeys_available &&
          !(mfa && query.data.totp_enabled) && (
            <p className="public-auth-error">
              {t(
                "暂时没有可用的登录方式，请联系管理员。",
                "No sign-in method is available. Contact your administrator.",
              )}
            </p>
          )}
      </section>
    </main>
  );
}

function Enrollment() {
  const { t } = useLocale();
  const query = useAuthState();
  if (query.isPending)
    return (
      <main className="public-auth-card">
        <LoadingState />
      </main>
    );
  if (query.isError)
    return (
      <main className="public-auth-card">
        <ErrorState retry={() => query.refetch()} />
      </main>
    );
  if (!query.data.authenticated) return <Navigate to="/login" replace />;
  return (
    <main className="public-security">
      <h1>{t("完善账户安全", "Secure your account")}</h1>
      <p className="muted">
        {t(
          "添加一种验证方式即可继续。",
          "Add a verification method to continue.",
        )}
      </p>
      <SecurityPanel
        enrollmentOnly
        onEnrolled={() => window.location.assign("/app/")}
      />
    </main>
  );
}
