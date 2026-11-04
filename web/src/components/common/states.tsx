import { AlertCircle, Inbox, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";
import { useLocale } from "../../app/locale";
import "../workflow/workflow.css";

export function LoadingState() {
  const { t } = useLocale();
  return (
    <div
      className="wf-loading"
      role="status"
      aria-label={t("正在加载", "Loading")}
    >
      <div />
      <div />
      <div />
    </div>
  );
}

export function ErrorState({ retry }: { retry: () => void }) {
  const { t } = useLocale();
  return (
    <div className="wf-state" role="alert">
      <AlertCircle size={26} />
      <h2>{t("暂时无法加载", "Unable to load this page")}</h2>
      <p>
        {t(
          "请检查网络后重试。若仍无法访问，请确认账户权限。",
          "Check your connection and try again. If this continues, check your account permissions.",
        )}
      </p>
      <button className="wf-button" onClick={retry}>
        <RefreshCw size={16} />
        {t("重试", "Try again")}
      </button>
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="wf-state">
      <Inbox size={28} />
      <h2>{title}</h2>
      {description && <p>{description}</p>}
      {action}
    </div>
  );
}
