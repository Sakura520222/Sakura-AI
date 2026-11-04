import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Check as CheckIcon,
  CircleAlert,
  Download,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import { api, ApiError } from "../../api/client";
import { useAuth } from "../../app/auth";
import { useLocale } from "../../app/locale";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import {
  Disclosure,
  ErrorState,
  LoadingState,
  RefreshButton,
  Section,
  StatusBadge,
  Timestamp,
} from "../../components/workflow/Workflow";
import {
  deploymentMatches,
  jobTarget,
  readinessChecks,
  targetForImage,
  transientUpdateError,
  unwrap,
  updateRequest,
  type Catalog,
  type Envelope,
  type Readiness,
  type Target,
  type UpdateJob,
  type VersionInfo,
} from "./state";
import "./version.css";
import versionMessages from "./messages.json";
import VersionBrowser, { type ReleaseRecord } from "./VersionBrowser";

const base = "/api/v1/version";

export default function VersionPanel() {
  const { t, language } = useLocale();
  const labels: Record<string, string> = versionMessages[language];
  const { user } = useAuth();
  const confirm = useConfirm();
  const storageKey = `sakura.update.job.${user.id}`;
  const [savedJob, setSavedJob] = useState(() =>
    sessionStorage.getItem(storageKey),
  );
  const [selected, setSelected] = useState<Target | null>(null);
  const updateControls = useRef<HTMLDivElement>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [unknownSubmission, setUnknownSubmission] = useState(
    () => sessionStorage.getItem(storageKey + ".pending") === "true",
  );
  const pendingInfoAt = useRef(0);
  const info = useQuery({
    queryKey: ["version-info"],
    queryFn: ({ signal }) => api<VersionInfo>(base + "/info", { signal }),
    refetchInterval: unknownSubmission ? 3000 : false,
  });
  const catalog = useQuery({
    queryKey: ["version-catalog"],
    queryFn: ({ signal }) => api<Catalog>(base + "/images", { signal }),
  });
  const releases = useQuery({
    queryKey: ["version-releases"],
    queryFn: ({ signal }) =>
      api<{
        releases: ReleaseRecord[];
      }>(base + "/releases", { signal }),
  });
  const preflight = useMutation({
    mutationFn: async (target: Target | null) =>
      unwrap(
        await api<Readiness | Envelope<Readiness>>(
          base + (target ? "/preflight" : "/readiness"),
          {
            method: "POST",
            body: target
              ? JSON.stringify({ target, confirm_channel_switch: confirmed })
              : undefined,
          },
        ),
      ),
  });
  const check = useMutation({
    mutationFn: () => api(base + "/check", { method: "POST" }),
    onSuccess: () => {
      void info.refetch();
      void catalog.refetch();
      void releases.refetch();
      preflight.reset();
    },
  });
  function remember(jobId: string) {
    sessionStorage.setItem(storageKey, jobId);
    sessionStorage.removeItem(storageKey + ".pending");
    setSavedJob(jobId);
    setUnknownSubmission(false);
  }
  const effectiveJob = info.data?.active_job_id || savedJob;
  useEffect(() => {
    if (info.data?.active_job_id) {
      sessionStorage.setItem(storageKey, info.data.active_job_id);
      sessionStorage.removeItem(storageKey + ".pending");
      setSavedJob(info.data.active_job_id);
      setUnknownSubmission(false);
    }
  }, [info.data?.active_job_id, storageKey]);
  const start = useMutation({
    mutationFn: async () => {
      const ready = await preflight.mutateAsync(selected);
      if (
        !(ready.can_update === true || ready.update_ready === true) ||
        ready.update_available === false
      )
        throw new Error("preflight_failed");
      const target = selected || ready.target;
      if (!target?.version) throw new Error("missing_target");
      const body = JSON.stringify(
        updateRequest(target, selected ? confirmed : false),
      );
      pendingInfoAt.current = info.dataUpdatedAt;
      sessionStorage.setItem(storageKey + ".pending", "true");
      setUnknownSubmission(true);
      try {
        const response = unwrap(
          await api<Envelope<{ job_id: string }> | { job_id: string }>(
            base + "/update",
            {
              method: "POST",
              body,
            },
          ),
        );
        if (!response.job_id) throw new Error("missing_job_id");
        remember(response.job_id);
      } catch (error) {
        if (
          error instanceof ApiError &&
          error.status === 409 &&
          error.body &&
          typeof error.body === "object" &&
          "job_id" in error.body &&
          typeof error.body.job_id === "string"
        )
          remember(error.body.job_id);
        else {
          if (
            error instanceof ApiError &&
            error.status >= 400 &&
            error.status < 500
          ) {
            sessionStorage.removeItem(storageKey + ".pending");
            setUnknownSubmission(false);
          }
          throw error;
        }
      }
    },
  });
  async function select(image: Catalog["images"][number]) {
    const target = targetForImage(image);
    if (!target || catalog.data?.stale) return;
    const needsConfirmation =
      target.channel === "development" ||
      info.data?.build.channel !== target.channel;
    if (
      needsConfirmation &&
      !(await confirm({
        title: t("确认切换更新目标", "Confirm update target"),
        description:
          target.channel === "development"
            ? t(
                "开发版可能包含尚未稳定的变更。选择后仍需通过部署预检，才能执行更新。",
                "Development builds may contain unstable changes. Deployment checks must pass before an update can run.",
              )
            : t(
                "将切换当前部署的更新通道，请确认目标通道。",
                "This changes the deployment update channel. Confirm the selected channel.",
              ),
        destructive: true,
      }))
    )
      return;
    setSelected(target);
    setConfirmed(needsConfirmation);
    preflight.reset();
    start.reset();
    updateControls.current?.scrollIntoView({
      block: "nearest",
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
        ? "auto"
        : "smooth",
    });
  }
  const busy =
    start.isPending ||
    check.isPending ||
    preflight.isPending ||
    !!effectiveJob ||
    info.data?.has_active_job ||
    unknownSubmission;
  if (info.isPending) return <LoadingState />;
  if (info.isError) return <ErrorState retry={() => void info.refetch()} />;
  return (
    <div className="version-panel">
      <Section
        title={t("版本与部署", "Version and deployment")}
        aside={
          <RefreshButton
            busy={info.isFetching || check.isPending}
            onClick={() => check.mutate()}
          />
        }
      >
        <div className="version-summary">
          <div>
            <span>{t("当前版本", "Current version")}</span>
            <strong>{info.data.current_version}</strong>
            <small>
              {info.data.build.channel || info.data.deployment_type}
            </small>
          </div>
          <div>
            <span>{t("Host Updater", "Host Updater")}</span>
            <strong>{info.data.updater_version || "—"}</strong>
            <small>
              {info.data.updater_connected
                ? t("已连接", "Connected")
                : t("未连接", "Not connected")}
            </small>
          </div>
          <div>
            <span>{t("上次检查", "Last checked")}</span>
            <Timestamp value={info.data.last_checked} />
          </div>
        </div>
        {!info.data.update_supported && (
          <p className="version-notice">
            {info.data.deployment_type === "source"
              ? t(
                  "源码部署可检查版本；更新请使用现有源码部署流程。",
                  "Source deployments can check versions. Apply updates using the source deployment workflow.",
                )
              : t(
                  "当前无法通过界面更新。请检查 Host Updater 连接、版本与部署配置。",
                  "Updates are unavailable here. Check the Host Updater connection, version and deployment configuration.",
                )}
          </p>
        )}
        {check.isError && (
          <p className="version-error" role="alert">
            {t(
              "检查更新失败，请重试。",
              "Could not check for updates. Please try again.",
            )}
          </p>
        )}
      </Section>
      {effectiveJob && (
        <JobProgress
          key={effectiveJob}
          id={effectiveJob}
          mayDismissMissing={!info.data.has_active_job && !info.isFetching}
          onClose={() => {
            sessionStorage.removeItem(storageKey);
            setSavedJob(null);
            void info.refetch();
            preflight.reset();
          }}
        />
      )}
      {unknownSubmission && !effectiveJob && (
        <div className="version-notice" role="status">
          <CircleAlert size={18} />
          <span>
            {t(
              "尚未确认更新请求的结果。正在查询 Host Updater，请勿重复提交。",
              "The update request outcome is not confirmed. Checking Host Updater; do not submit it again.",
            )}
          </span>
          <button className="button" onClick={() => void info.refetch()}>
            {t("重新查询", "Check again")}
          </button>
          {!start.isPending &&
            info.isSuccess &&
            !info.isFetching &&
            !info.data.has_active_job &&
            info.dataUpdatedAt > pendingInfoAt.current && (
              <button
                className="button"
                onClick={async () => {
                  if (
                    await confirm({
                      title: t(
                        "已核对部署状态？",
                        "Deployment status checked?",
                      ),
                      description: t(
                        "Host Updater 当前没有活跃任务，但此前请求的结果仍未确认。核对部署状态后，可解除本页提交锁并重新预检。",
                        "Host Updater reports no active job, but the previous request outcome remains unconfirmed. After checking the deployment, unlock this page and run preflight again.",
                      ),
                    })
                  ) {
                    sessionStorage.removeItem(storageKey + ".pending");
                    setUnknownSubmission(false);
                    start.reset();
                    preflight.reset();
                  }
                }}
              >
                {t("我已核对，重新预检", "Status checked; allow preflight")}
              </button>
            )}
        </div>
      )}
      <div ref={updateControls} className="version-update-controls">
        <Section title={t("更新目标", "Update target")}>
          <div className="version-target">
            <div>
              <strong>
                {selected
                  ? `${selected.channel} · ${selected.version}`
                  : t(
                      "当前通道的最新版本",
                      "Latest version on the current channel",
                    )}
              </strong>
              {selected && <code>{selected.digest}</code>}
            </div>
            <button
              className="button"
              disabled={!!busy || !info.data.update_supported}
              onClick={() => preflight.mutate(selected)}
            >
              <ShieldCheck size={16} />
              {t("运行预检", "Run preflight")}
            </button>
            <button
              className="button primary"
              disabled={
                !!busy ||
                !info.data.update_supported ||
                catalog.data?.stale === true
              }
              onClick={async () => {
                if (
                  await confirm({
                    title: t("开始部署更新？", "Start deployment update?"),
                    description: t(
                      "系统将先重新检查目标，再交给 Host Updater 执行。过程中连接可能暂时中断。",
                      "The target will be checked again before Host Updater applies it. The connection may briefly be interrupted.",
                    ),
                    confirmLabel: t("开始更新", "Start update"),
                  })
                )
                  start.mutate();
              }}
            >
              <Download size={16} />
              {t("开始更新", "Start update")}
            </button>
          </div>
          {(preflight.isError || start.isError) && (
            <p className="version-error" role="alert">
              {t(
                "预检或更新请求未完成。请检查当前目标与 Host Updater 状态。",
                "The preflight or update request did not complete. Check the target and Host Updater status.",
              )}
            </p>
          )}
          {preflight.data && (
            <div className="version-checks">
              <p>
                {preflight.data.can_update === true ||
                preflight.data.update_ready === true
                  ? t("预检通过", "Preflight passed")
                  : t("尚不满足更新条件", "Update requirements are not met")}
              </p>
              {readinessChecks(preflight.data).map((check) => (
                <div
                  key={check.name}
                  className={
                    check.passed
                      ? "version-check-passed"
                      : "version-check-failed"
                  }
                >
                  {check.passed ? <CheckIcon size={16} /> : <X size={16} />}
                  <span>
                    {labels[`check_${check.name}`] ||
                      check.name.replaceAll("_", " ")}
                    {check.detail && ` · ${check.detail}`}
                  </span>
                </div>
              ))}
            </div>
          )}
        </Section>
      </div>
      <VersionBrowser
        catalog={catalog}
        releases={releases}
        selected={selected}
        selectionDisabled={!!busy || !info.data.update_supported}
        onSelect={select}
      />
    </div>
  );
}

function JobProgress({
  id,
  onClose,
  mayDismissMissing,
}: {
  id: string;
  onClose: () => void;
  mayDismissMissing: boolean;
}) {
  const { t, language } = useLocale();
  const labels: Record<string, string> = versionMessages[language];
  const confirm = useConfirm();
  const transientSince = useRef<number | null>(null);
  const [deferred, setDeferred] = useState(false);
  const query = useQuery({
    queryKey: ["update-job", id],
    queryFn: async ({ signal }) => {
      try {
        const result = unwrap(
          await api<UpdateJob | Envelope<UpdateJob>>(
            `${base}/jobs/${encodeURIComponent(id)}`,
            { signal },
          ),
        );
        transientSince.current = null;
        return result;
      } catch (error) {
        if (transientUpdateError(error)) {
          transientSince.current ??= performance.now();
          // Preserve the existing manager's two-minute transport recovery window.
          if (performance.now() - transientSince.current >= 120_000)
            setDeferred(true);
        }
        throw error;
      }
    },
    retry: false,
    refetchInterval: (current) =>
      deferred ||
      ["success", "complete", "failed", "rolled_back"].includes(
        current.state.data?.state ?? "",
      ) ||
      (current.state.error && !transientUpdateError(current.state.error))
        ? false
        : 1500,
  });
  const job = query.data;
  const health = useQuery({
    queryKey: ["update-health", id, job?.target_digest],
    queryFn: ({ signal }) =>
      api<{ version?: string; build?: Partial<Target> }>("/health", { signal }),
    enabled:
      !!job &&
      ["success", "complete"].includes(job.state) &&
      job.deployment_verified === true,
    retry: false,
  });
  const verified =
    !!job &&
    query.isSuccess &&
    health.isSuccess &&
    deploymentMatches(job, health.data);
  const failed = !!job && ["failed", "rolled_back"].includes(job.state);
  const missing = query.error instanceof ApiError && query.error.status === 404;
  const terminal = verified || failed || (missing && mayDismissMissing);
  return (
    <Section
      title={t("更新进度", "Update progress")}
      aside={
        terminal ? (
          <button
            className="button"
            onClick={async () => {
              if (
                missing &&
                !(await confirm({
                  title: t(
                    "关闭已不可用的任务记录？",
                    "Close unavailable job record?",
                  ),
                  description: t(
                    "当前没有活跃更新任务。关闭记录后，仍需重新预检才能提交更新。",
                    "No update job is active. A fresh preflight is still required before another update can be submitted.",
                  ),
                }))
              )
                return;
              onClose();
            }}
          >
            {t("关闭记录", "Close record")}
          </button>
        ) : undefined
      }
    >
      <div className="version-job" role="status">
        <StatusBadge
          value={verified ? "completed" : failed ? "failed" : "processing"}
        />
        <span>
          {verified
            ? t(
                "部署与运行版本均已验证",
                "Deployment and running version verified",
              )
            : failed
              ? t("更新失败或已回滚", "Update failed or rolled back")
              : query.isError
                ? t(
                    "正在恢复连接，更新结果尚未确认",
                    "Reconnecting; the update outcome is not yet confirmed",
                  )
                : (job?.step && (labels[`step_${job.step}`] || job.step)) ||
                  t("正在读取更新状态", "Reading update status")}
        </span>
        <code>{id}</code>
      </div>
      {job && (
        <p className="muted">
          {jobTarget(job).channel} · {job.target_version} · {job.target_digest}
        </p>
      )}
      {job && ["success", "complete"].includes(job.state) && !verified && (
        <p className="version-notice">
          {t(
            "尚未验证完整部署结果，请检查 Host Updater 与运行版本。",
            "The complete deployment outcome is not verified. Check Host Updater and the running version.",
          )}
        </p>
      )}
      {(deferred ||
        query.isError ||
        health.isError ||
        (job && ["success", "complete"].includes(job.state) && !verified)) && (
        <button
          className="button"
          onClick={() => {
            transientSince.current = null;
            setDeferred(false);
            void query.refetch();
            if (job?.deployment_verified) void health.refetch();
          }}
        >
          <RefreshCw size={15} />
          {t("继续查询此任务", "Continue checking this job")}
        </button>
      )}
      <Disclosure title={t("更新诊断日志", "Update diagnostic logs")}>
        <JobLogs id={id} />
      </Disclosure>
      {verified && (
        <button
          className="button primary"
          onClick={() => window.location.reload()}
        >
          {t("刷新以使用新版本", "Reload to use the new version")}
        </button>
      )}
    </Section>
  );
}

function JobLogs({ id }: { id: string }) {
  const query = useQuery({
    queryKey: ["update-job-logs", id],
    queryFn: ({ signal }) =>
      api<unknown>(`${base}/jobs/${encodeURIComponent(id)}/logs`, { signal }),
    gcTime: 0,
  });
  return query.isPending ? (
    <LoadingState />
  ) : query.isError ? (
    <ErrorState retry={() => void query.refetch()} />
  ) : (
    <pre className="version-logs">{JSON.stringify(query.data, null, 2)}</pre>
  );
}
