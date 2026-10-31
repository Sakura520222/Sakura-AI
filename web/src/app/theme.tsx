import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { Toaster } from "sonner";

type Theme = "light" | "dark" | "system";
const ThemeContext = createContext({
  theme: "system" as Theme,
  setTheme: (_theme: Theme) => {},
});

export function ThemeProvider({
  children,
  initial = "system",
}: {
  children: ReactNode;
  initial?: Theme;
}) {
  const [theme, setTheme] = useState<Theme>(() => {
    const stored = localStorage.getItem("sakura.theme");
    return stored === "light" || stored === "dark" || stored === "system"
      ? stored
      : initial;
  });
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      document.documentElement.dataset.theme =
        theme === "system" ? (media.matches ? "dark" : "light") : theme;
    };
    apply();
    localStorage.setItem("sakura.theme", theme);
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [theme]);
  useEffect(() => {
    const update = (event: Event) => {
      const value: unknown = (event as CustomEvent).detail;
      if (value === "light" || value === "dark" || value === "system")
        setTheme(value);
    };
    window.addEventListener("sakura-theme", update);
    return () => window.removeEventListener("sakura-theme", update);
  }, []);
  return (
    <ThemeContext.Provider value={{ theme, setTheme }}>
      {children}
      <Toaster theme={theme} richColors position="bottom-right" />
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  return useContext(ThemeContext);
}
