import type { ReactNode } from "react";
import { LocaleProvider, useLocale } from "../../app/locale";
import { ThemeProvider } from "../../app/theme";
import { ConfirmProvider } from "../ui/ConfirmProvider";
import { BrandMark } from "../common/BrandMark";
import "../../features/auth/auth.css";

export function PublicShell({ children }: { children: ReactNode }) {
  return (
    <LocaleProvider initial="zh-CN">
      <ThemeProvider>
        <ConfirmProvider>
          <div className="public-auth">
            <header>
              <a className="public-auth-brand" href="/app/">
                <BrandMark />
                Sakura AI
              </a>
              <Language />
            </header>
            {children}
            <footer>
              <a href="/app/legal/terms">Terms</a> ·{" "}
              <a href="/app/legal/privacy">Privacy</a> ·{" "}
              <a href="/app/legal/refund">Refunds</a> ·{" "}
              <a href="/app/legal/pricing">Pricing</a> ·{" "}
              <a
                href="https://github.com/Sakura520222/Sakura-AI"
                target="_blank"
                rel="noreferrer"
              >
                GitHub
              </a>
            </footer>
          </div>
        </ConfirmProvider>
      </ThemeProvider>
    </LocaleProvider>
  );
}
function Language() {
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
