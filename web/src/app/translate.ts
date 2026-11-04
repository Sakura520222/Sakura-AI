import messages from "./messages.json";

const catalog: Readonly<Record<string, readonly string[]>> = messages;
export function translate(
  zh: string,
  en: string,
  language: "zh-CN" | "en",
  values: readonly unknown[] = [],
) {
  const pair = catalog[JSON.stringify([zh, en])] ?? [zh, en];
  return pair[language === "en" ? 1 : 0].replace(
    /\{(\d+)\}/g,
    (placeholder, index: string) =>
      Number(index) < values.length
        ? String(values[Number(index)] ?? "")
        : placeholder,
  );
}
