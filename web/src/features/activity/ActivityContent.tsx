import { useMemo, useState } from "react";
import { useLocale } from "../../app/locale";
import { Markdown } from "../../components/workflow/Workflow";

// Bound parsing and DOM work even after expansion. Copy retains the full payload.
const PREVIEW = 2000;
const PAGE = 20000;
export function ActivityContent({ content }: { content: string }) {
  const { t } = useLocale();
  const [expanded, setExpanded] = useState(false);
  const [offset, setOffset] = useState(0);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "error">(
    "idle",
  );
  const presentation = useMemo(() => {
    if (content.length > PAGE) return { text: content, json: false };
    try {
      const parsed: unknown = JSON.parse(content);
      if (parsed === null || typeof parsed !== "object")
        return { text: content, json: false };
      // Inspect depth iteratively before pretty-printing. Indentation of compact
      // deeply nested JSON can otherwise grow quadratically in its input size.
      const stack: { value: unknown; depth: number }[] = [
        { value: parsed, depth: 0 },
      ];
      let budget = 0;
      while (stack.length) {
        const { value, depth } = stack.pop()!;
        budget += 2 * depth + 4;
        if (depth > 16 || budget > PAGE * 2)
          return { text: content, json: true };
        if (value && typeof value === "object") {
          for (const child of Object.values(value))
            stack.push({ value: child, depth: depth + 1 });
        }
      }
      return { text: JSON.stringify(parsed, null, 2), json: true };
    } catch {
      return { text: content, json: false };
    }
  }, [content]);
  const text = expanded
    ? presentation.text.slice(offset, offset + PAGE)
    : presentation.text.slice(0, PREVIEW);
  return (
    <div className="activity-content">
      {presentation.json ? (
        <pre className="activity-json">
          <code>{text}</code>
        </pre>
      ) : (
        <Markdown>{text}</Markdown>
      )}
      <div className="wf-actions">
        {presentation.text.length > PREVIEW && (
          <button
            className="button"
            onClick={() => {
              setExpanded(!expanded);
              setOffset(0);
            }}
          >
            {expanded ? t("收起", "Collapse") : t("展开", "Expand")}
          </button>
        )}
        {expanded && offset > 0 && (
          <button
            className="button"
            onClick={() => setOffset(Math.max(0, offset - PAGE))}
          >
            {t("上一页", "Previous")}
          </button>
        )}
        {expanded && presentation.text.length > offset + PAGE && (
          <button className="button" onClick={() => setOffset(offset + PAGE)}>
            {t("显示更多", "Show more")}
          </button>
        )}
        <button
          className="button"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(content);
              setCopyState("copied");
            } catch {
              setCopyState("error");
            }
          }}
        >
          {t("复制", "Copy")}
        </button>
        {copyState === "copied" && (
          <span role="status">{t("已复制", "Copied")}</span>
        )}
        {copyState === "error" && (
          <span role="alert">{t("复制失败", "Copy failed")}</span>
        )}
      </div>
    </div>
  );
}
