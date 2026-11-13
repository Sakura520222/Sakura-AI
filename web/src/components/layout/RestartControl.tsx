import { useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { LoaderCircle, Power } from "lucide-react";
import { api } from "../../api/client";
import { useLocale } from "../../app/locale";
import { useConfirm } from "../ui/ConfirmProvider";

interface Health {
  status?: string;
  startup_time?: string | null;
}

export function RestartControl() {
  const { t } = useLocale();
  const confirm = useConfirm();
  const [waiting, setWaiting] = useState(false);
  const before = useRef<string | null>(null);
  const sawUnavailable = useRef(false);
  const began = useRef(0);
  const restart = useMutation({
    mutationFn: async () => {
      // Compare startup identity rather than treating a response from the old
      // process during its graceful-shutdown delay as a completed restart.
      try {
        before.current = (await api<Health>("/health")).startup_time ?? null;
      } catch {
        before.current = null;
      }
      const response = await api<{ success: boolean; restarting: boolean }>(
        "/api/v1/webui/system/restart",
        { method: "POST" },
      );
      if (!response.success || !response.restarting)
        throw new Error("restart_not_accepted");
      began.current = performance.now();
      sawUnavailable.current = false;
      setWaiting(true);
    },
  });
  const recovered = useQuery({
    queryKey: ["application-restart-health", restart.submittedAt],
    queryFn: async ({ signal }) => {
      try {
        const health = await api<Health>("/health", { signal });
        return (
          health.status === "healthy" &&
          (before.current
            ? !!health.startup_time && health.startup_time !== before.current
            : sawUnavailable.current)
        );
      } catch (error) {
        sawUnavailable.current = true;
        throw error;
      }
    },
    enabled: waiting,
    retry: false,
    refetchInterval: (query) =>
      query.state.data === true || performance.now() - began.current > 120_000
        ? false
        : 2000,
  });
  return (
    <>
      <button
        className="icon-button topbar-restart"
        title={t("重启应用", "Restart application")}
        aria-label={t("重启应用", "Restart application")}
        disabled={restart.isPending || waiting}
        onClick={async () => {
          if (
            await confirm({
              title: t("重启 Sakura AI？", "Restart Sakura AI?"),
              description: t(
                "当前实例将暂时停止服务。请先保存你的修改。",
                "This instance will briefly stop serving requests. Save your changes first.",
              ),
              confirmLabel: t("重启", "Restart"),
            })
          )
            restart.mutate();
        }}
      >
        {restart.isPending || waiting ? (
          <LoaderCircle size={18} className="spin" />
        ) : (
          <Power size={18} />
        )}
      </button>
      {waiting && (
        <div className="topbar-restart-status" role="status">
          <strong>
            {recovered.data
              ? t("服务已恢复", "Service is back")
              : t("正在重启应用", "Application restarting")}
          </strong>
          <p>
            {recovered.data
              ? t(
                  "刷新以连接新的服务实例。",
                  "Reload to connect to the new service instance.",
                )
              : t(
                  "正在等待服务恢复。请勿重复提交重启。",
                  "Waiting for service recovery. Do not submit another restart.",
                )}
          </p>
          <button className="button" onClick={() => window.location.reload()}>
            {t("刷新", "Refresh")}
          </button>
        </div>
      )}
      {restart.isError && (
        <div className="topbar-restart-status" role="alert">
          {t(
            "重启请求未完成，请检查服务状态后重试。",
            "The restart request did not complete. Check service status before retrying.",
          )}
        </div>
      )}
    </>
  );
}
