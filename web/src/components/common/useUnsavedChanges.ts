import { useEffect, useRef } from "react";
import { useBlocker } from "react-router-dom";
import { useLocale } from "../../app/locale";
import { useConfirm } from "../ui/ConfirmProvider";

export function useUnsavedChanges(
  dirty: boolean,
  bypass: { current: boolean },
) {
  const { t } = useLocale();
  const confirm = useConfirm();
  const asking = useRef(false);
  const blocker = useBlocker(() => dirty && !bypass.current);
  useEffect(() => {
    if (blocker.state !== "blocked" || asking.current) return;
    asking.current = true;
    void confirm({
      title: t("放弃未保存的修改？", "Discard unsaved changes?"),
      description: t(
        "离开后，未保存的内容将丢失。",
        "Unsaved content will be lost when you leave.",
      ),
      destructive: true,
    }).then((accepted) => {
      asking.current = false;
      if (accepted) blocker.proceed();
      else blocker.reset();
    });
  }, [blocker, confirm, t]);
  useEffect(() => {
    const handle = (event: BeforeUnloadEvent) => {
      if (dirty && !bypass.current) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", handle);
    return () => window.removeEventListener("beforeunload", handle);
  }, [dirty, bypass]);
}
