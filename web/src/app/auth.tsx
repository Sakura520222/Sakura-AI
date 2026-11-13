import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { LogIn, RefreshCw } from "lucide-react";
import { api, ApiError, setCsrfToken } from "../api/client";
import { LocaleProvider } from "./locale";
import { ThemeProvider } from "./theme";
import type { components } from "../api/generated";
import { BrandMark } from "../components/common/BrandMark";

export type Bootstrap = components["schemas"]["WebUIBootstrap"];

const AuthContext = createContext<Bootstrap | null>(null);
export function useAuth() {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error("Authentication provider is missing");
  return auth;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  const [expired, setExpired] = useState(false);
  const [enrollment, setEnrollment] = useState(false);
  const [activeIdentity, setActiveIdentity] = useState<string | null>(null);
  const query = useQuery({
    queryKey: ["bootstrap"],
    queryFn: async ({ signal }) => {
      const result = await api<Bootstrap>("/api/v1/webui/bootstrap", {
        signal,
      });
      setCsrfToken(result.csrf);
      return result;
    },
    retry: (count, error) =>
      !(error instanceof ApiError && [401, 403, 428].includes(error.status)) &&
      count < 1,
    staleTime: 60_000,
    enabled: !expired,
  });
  const identity = query.data
    ? JSON.stringify([
        query.data.user.id,
        query.data.user.username,
        query.data.user.role,
        query.data.permissions,
      ])
    : null;
  useEffect(() => {
    if (!identity || identity === activeIdentity) return;
    // Keep old account data and component state out of the next authenticated tree.
    const privateQueries = {
      predicate: (q: { queryKey: readonly unknown[] }) =>
        q.queryKey[0] !== "bootstrap",
    };
    client.removeQueries(privateQueries);
    setActiveIdentity(identity);
  }, [identity, activeIdentity, client]);
  useEffect(() => {
    const expire = () => {
      setExpired(true);
      setCsrfToken("");
      client.removeQueries({ predicate: (q) => q.queryKey[0] !== "bootstrap" });
    };
    window.addEventListener("sakura:session-expired", expire);
    const requireEnrollment = () => setEnrollment(true);
    window.addEventListener("sakura:enrollment-required", requireEnrollment);
    return () => {
      window.removeEventListener("sakura:session-expired", expire);
      window.removeEventListener(
        "sakura:enrollment-required",
        requireEnrollment,
      );
    };
  }, [client]);
  if (
    expired ||
    (query.error instanceof ApiError && query.error.status === 401)
  )
    return (
      <div className="auth-screen">
        <BrandMark size={38} />
        <h1>Sakura AI</h1>
        <p>请登录以继续 · Sign in to continue</p>
        <a className="button primary" href="/app/login">
          <LogIn size={18} />
          登录 / Sign in
        </a>
      </div>
    );
  if (query.isPending || (query.data && activeIdentity !== identity))
    return (
      <div className="auth-screen" role="status">
        <BrandMark size={38} />
        <p>正在载入工作空间 / Loading workspace…</p>
      </div>
    );
  if (
    enrollment ||
    (query.error instanceof ApiError && query.error.status === 428)
  )
    return (
      <div className="auth-screen">
        <h1>完善账号安全 / Secure your account</h1>
        <p>
          请先完成要求的两步验证设置。
          <br />
          Complete the required two-factor setup to continue.
        </p>
        <a className="button primary" href="/app/security">
          设置两步验证 / Set up verification
        </a>
      </div>
    );
  if (query.isError || !query.data)
    return (
      <div className="auth-screen" role="alert">
        <h1>暂时无法连接 / Unable to connect</h1>
        <p>请检查网络后重试 / Check your connection and try again.</p>
        <button className="button primary" onClick={() => query.refetch()}>
          <RefreshCw size={16} />
          重试 / Retry
        </button>
      </div>
    );
  return (
    <AuthContext.Provider key={identity} value={query.data}>
      <LocaleProvider
        initial={query.data.preferences.language}
        timezone={query.data.timezone}
      >
        <ThemeProvider initial={query.data.preferences.theme}>
          {children}
        </ThemeProvider>
      </LocaleProvider>
    </AuthContext.Provider>
  );
}
