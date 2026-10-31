import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { translate } from "./translate";

export type Language = "zh-CN" | "en";
const LocaleContext = createContext({
  timezone: 'UTC',
  language: "zh-CN" as Language,
  setLanguage: (_language: Language) => {},
  t: (zh: string, en: string, values?: readonly unknown[]) =>
    translate(zh, en, "zh-CN", values),
});

export function LocaleProvider({
  initial,
  timezone = 'UTC',
  children,
}: {
  initial: Language;
  timezone?: string;
  children: ReactNode;
}) {
  const [language, setLanguage] = useState<Language>(() => {
    const stored = localStorage.getItem("sakura.language");
    return stored === "en" || stored === "zh-CN" ? stored : initial;
  });
  const t = useCallback(
    (zh: string, en: string, values?: readonly unknown[]) =>
      translate(zh, en, language, values),
    [language],
  );
  useEffect(() => {
    document.documentElement.lang = language;
    localStorage.setItem("sakura.language", language);
  }, [language]);
  useEffect(() => {
    const update = (event: Event) => {
      const value: unknown = (event as CustomEvent).detail;
      if (value === "en" || value === "zh-CN") setLanguage(value);
    };
    window.addEventListener("sakura-language", update);
    return () => window.removeEventListener("sakura-language", update);
  }, []);
  return (
    <LocaleContext.Provider
      value={{
        timezone,
        language,
        setLanguage,
        t,
      }}
    >
      {children}
    </LocaleContext.Provider>
  );
}

export function useLocale() {
  return useContext(LocaleContext);
}
