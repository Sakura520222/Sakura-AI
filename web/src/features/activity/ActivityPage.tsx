import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router-dom";
import {
  Activity,
  ArrowLeft,
  ArrowUp,
  ChevronDown,
  Circle,
  LockKeyhole,
  Search,
  SlidersHorizontal,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import { api, ApiError } from "../../api/client";
import { connectStream, type ConnectionState } from "../../api/realtime/sse";
import { useLocale } from "../../app/locale";
import { useAuth } from "../../app/auth";
import {
  Disclosure,
  EmptyState,
  ErrorState,
  LoadingState,
  Markdown,
  PageHeader,
  RefreshButton,
  StatusBadge,
  Timestamp,
} from "../../components/workflow/Workflow";
import {
  mergeEntries,
  type ActivitySession,
  type Artifact,
  type Conversation,
  type Entry,
  type Snapshot,
  type Updates,
} from "./types";
import "./activity.css";

export default function ActivityPage() {
  const { id } = useParams();
  return id ? <SessionDetail key={id} id={id} /> : <SessionList />;
}

function SessionList() {
  const { t } = useLocale();
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = useState("");
  const kind = params.get("type") || "";
  const query = useQuery({
    queryKey: ["activity-sessions"],
    queryFn: ({ signal }) =>
      api<{ sessions: ActivitySession[] }>(
        "/api/v1/activity/sessions?limit=100",
        { signal },
      ),
    refetchInterval: 30_000,
    gcTime: 0,
  });
  const sessions =
    query.data?.sessions.filter(
      (session) =>
        (!kind || session.resource_type === kind) &&
        `${session.repo_full_name} ${session.resource_number}`
          .toLowerCase()
          .includes(search.toLowerCase()),
    ) ?? [];
  return (
    <div>
      <PageHeader
        title={t("运行活动", "Activity")}
        description={t(
          "查看模型调用、工具执行和任务对话。",
          "Follow model calls, tool execution, and task conversations.",
        )}
        actions={
          <RefreshButton
            busy={query.isFetching}
            onClick={() => void query.refetch()}
          />
        }
      />
      <div className="activity-filterbar">
        <label className="search-field">
          <Search size={17} />
          <input
            aria-label={t("搜索运行活动", "Search activity")}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={t("搜索仓库或编号…", "Search repository or number…")}
          />
        </label>
        <select
          aria-label={t("活动类型", "Activity type")}
          value={kind}
          onChange={(event) =>
            setParams(event.target.value ? { type: event.target.value } : {})
          }
        >
          <option value="">{t("全部类型", "All types")}</option>
          <option value="pr">Pull Request</option>
          <option value="issue">Issue</option>
          <option value="scan">{t("仓库扫描", "Repository scan")}</option>
          <option value="agent">Agent</option>
        </select>
      </div>
      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState retry={() => void query.refetch()} />
      ) : !sessions.length ? (
        <EmptyState
          title={t("暂无运行活动", "No activity yet")}
          description={t(
            "任务开始运行后，活动会显示在这里。",
            "Activity appears here when tasks start running.",
          )}
        />
      ) : (
        <div className="activity-session-list">
          {sessions.map((session) => (
            <Link
              to={`/activity/${session.session_id}`}
              key={session.session_id}
              className="activity-session-row"
            >
              <span className="activity-session-icon">
                <Activity size={20} />
              </span>
              <div>
                <strong>
                  {session.repo_full_name || session.session_kind}
                </strong>
                <p>
                  {session.resource_type}{" "}
                  {session.resource_number ? `#${session.resource_number}` : ""}
                  <span>
                    · {t("会话", "Session")} {session.session_id}
                  </span>
                </p>
              </div>
              <StatusBadge value={session.status} />
              <Timestamp value={session.last_active_at} />
            </Link>
          ))}
        </div>
      )}
      {query.data?.sessions.length === 100 && (
        <p className="muted activity-limit">
          {t(
            "显示最近 100 个有权访问的会话。",
            "Showing the latest 100 sessions you can access.",
          )}
        </p>
      )}
    </div>
  );
}

function SessionDetail({ id }: { id: string }) {
  const { t, language } = useLocale();
  const { permissions } = useAuth();
  const client = useQueryClient();
  const key = ["activity-conversation", id] as const;
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [kind, setKind] = useState("all");
  const [role, setRole] = useState("all");
  const [thread, setThread] = useState("all");
  const [invocation, setInvocation] = useState("all");
  const [diagnostics, setDiagnostics] = useState(false);
  const viewport = useRef<HTMLDivElement>(null);
  const snapshot = useQuery({
    queryKey: ["activity-snapshot", id],
    queryFn: ({ signal }) =>
      api<Snapshot>(`/api/v1/activity/sessions/${id}/snapshot`, { signal }),
    refetchInterval: 15_000,
    gcTime: 0,
  });
  const conversation = useQuery({
    queryKey: key,
    queryFn: async ({ signal }): Promise<Conversation> => {
      const existing = client.getQueryData<Conversation>(key);
      if (existing?.events_cursor) {
        try {
          const updates = await api<Updates>(
            `/api/v1/activity/sessions/${id}/conversation/events?cursor=${encodeURIComponent(existing.events_cursor)}`,
            { signal },
          );
          if (updates.projection_version === existing.projection_version)
            return {
              ...existing,
              entries: mergeEntries(existing.entries, updates.entries),
              events_cursor: updates.cursor,
              last_scanned_sequence: updates.last_scanned_sequence,
            };
        } catch (error) {
          if (!(error instanceof ApiError && error.status === 409)) throw error;
        }
      }
      return api<Conversation>(`/api/v1/activity/sessions/${id}/conversation`, {
        signal,
      });
    },
    refetchInterval: 10_000,
    gcTime: 0,
    retry: (count, error) =>
      !(error instanceof ApiError && [401, 403, 404].includes(error.status)) &&
      count < 1,
  });
  useEffect(
    () =>
      connectStream(`/api/v1/activity/sessions/${id}/stream`, {
        events: {
          "activity:notification": () => {
            void client.invalidateQueries({
              queryKey: ["activity-conversation", id],
            });
            void client.invalidateQueries({
              queryKey: ["activity-snapshot", id],
            });
          },
        },
        onState: setConnection,
      }),
    [id, client],
  );
  const earlier = useMutation({
    mutationFn: async () => {
      const existing = client.getQueryData<Conversation>(key);
      if (!existing?.before_cursor) return;
      try {
        const page = await api<Conversation>(
          `/api/v1/activity/sessions/${id}/conversation?cursor=${encodeURIComponent(existing.before_cursor)}`,
        );
        client.setQueryData<Conversation>(key, (current) =>
          current
            ? {
                ...current,
                entries: mergeEntries(current.entries, page.entries),
                before_cursor: page.before_cursor,
              }
            : page,
        );
      } catch (error) {
        if (error instanceof ApiError && error.status === 409) {
          client.setQueryData(key, undefined);
          await client.resetQueries({ queryKey: key });
          return;
        }
        throw error;
      }
    },
  });
  const entries = conversation.data?.entries ?? [];
  const roles = [
    ...new Set(
      entries
        .map((entry) => entry.source_role || entry.role)
        .filter((value): value is string => !!value),
    ),
  ];
  const threads = [
    ...new Set(
      entries
        .map((entry) => entry.thread_id)
        .filter((value): value is number => value != null),
    ),
  ];
  const invocations = [
    ...new Set(
      entries
        .map((entry) => entry.invocation_id)
        .filter((value): value is number => value != null),
    ),
  ];
  const groups: Record<string, string[]> = {
    messages: ["message"],
    model: ["attempt", "reasoning"],
    tools: ["tool_call"],
    context: ["context_operation", "thread_boundary", "invocation_boundary"],
  };
  const filtered = entries.filter(
    (entry) =>
      entry.type !== "tool_result" &&
      (kind === "all" || groups[kind]?.includes(entry.type)) &&
      (role === "all" || (entry.source_role || entry.role) === role) &&
      (thread === "all" || String(entry.thread_id) === thread) &&
      (invocation === "all" || String(entry.invocation_id) === invocation),
  );
  const session = snapshot.data?.session;
  const connectionLabel =
    connection === "connected"
      ? t("实时连接", "Live")
      : connection === "connecting"
        ? t("正在连接", "Connecting")
        : t(
            "正在重连，定时刷新仍在运行",
            "Reconnecting; periodic refresh is active",
          );
  return (
    <div>
      <Link className="activity-back" to="/activity">
        <ArrowLeft size={16} />
        {t("所有活动", "All activity")}
      </Link>
      <PageHeader
        title={session?.repo_full_name || `${t("会话", "Session")} #${id}`}
        description={`${session?.resource_type || ""} ${session?.resource_number ? `#${session.resource_number}` : ""}`}
        actions={
          <>
            <button
              className="button"
              aria-pressed={diagnostics}
              onClick={() => setDiagnostics(!diagnostics)}
            >
              <SlidersHorizontal size={16} />
              {t("运行详情", "Diagnostics")}
            </button>
            <RefreshButton
              busy={conversation.isFetching}
              onClick={() => {
                void conversation.refetch();
                void snapshot.refetch();
              }}
            />
          </>
        }
      />
      <div className="activity-stream-bar">
        <StatusBadge value={session?.status} />
        <span role="status" className="activity-connection">
          {connection === "connected" ? (
            <Wifi size={15} />
          ) : (
            <WifiOff size={15} />
          )}
          {connectionLabel}
        </span>
      </div>
      <div
        className={`activity-detail-layout${diagnostics ? " with-diagnostics" : ""}`}
      >
        <div className="activity-conversation">
          <div className="activity-timeline-filters">
            <select
              aria-label={t("内容类型", "Entry type")}
              value={kind}
              onChange={(event) => setKind(event.target.value)}
            >
              <option value="all">{t("全部内容", "All entries")}</option>
              <option value="messages">{t("对话", "Messages")}</option>
              <option value="model">{t("模型调用", "Model calls")}</option>
              <option value="tools">{t("工具", "Tools")}</option>
              <option value="context">{t("上下文", "Context")}</option>
            </select>
            <select
              aria-label={t("角色", "Role")}
              value={role}
              onChange={(event) => setRole(event.target.value)}
            >
              <option value="all">{t("全部角色", "All roles")}</option>
              {roles.map((item) => (
                <option key={item}>{item}</option>
              ))}
            </select>
            <select
              aria-label={t("线程", "Thread")}
              value={thread}
              onChange={(event) => setThread(event.target.value)}
            >
              <option value="all">{t("全部线程", "All threads")}</option>
              {threads.map((item) => (
                <option key={item}>{item}</option>
              ))}
            </select>
            <select
              aria-label={t("执行批次", "Invocation")}
              value={invocation}
              onChange={(event) => setInvocation(event.target.value)}
            >
              <option value="all">{t("全部执行", "All invocations")}</option>
              {invocations.map((item) => (
                <option key={item}>{item}</option>
              ))}
            </select>
          </div>
          <div className="activity-timeline" ref={viewport}>
            {conversation.isPending ? (
              <LoadingState />
            ) : conversation.isError ? (
              <ErrorState retry={() => void conversation.refetch()} />
            ) : (
              <>
                {conversation.data.before_cursor && (
                  <button
                    className="button activity-earlier"
                    disabled={earlier.isPending}
                    onClick={() => earlier.mutate()}
                  >
                    <ArrowUp size={15} />
                    {t("加载更早记录", "Load earlier entries")}
                  </button>
                )}
                {earlier.isError && (
                  <p className="danger-text" role="alert">
                    {t(
                      "历史记录加载失败，请重试。",
                      "Could not load earlier entries. Try again.",
                    )}
                  </p>
                )}
                {filtered.length ? (
                  filtered.map((entry) => (
                    <TimelineEntry
                      key={entry.id}
                      entry={entry}
                      sessionId={id}
                    />
                  ))
                ) : (
                  <EmptyState
                    title={t(
                      "没有符合筛选条件的记录",
                      "No entries match these filters",
                    )}
                  />
                )}
              </>
            )}
          </div>
          <button
            className="button activity-latest"
            onClick={() =>
              viewport.current?.scrollTo({
                top: viewport.current.scrollHeight,
                behavior: "smooth",
              })
            }
          >
            <ChevronDown size={15} />
            {t("跳至最新记录", "Jump to latest")}
          </button>
        </div>
        {diagnostics && (
          <aside className="activity-diagnostics">
            <h2>{t("运行详情", "Diagnostics")}</h2>
            {snapshot.isError ? (
              <ErrorState retry={() => void snapshot.refetch()} />
            ) : (
              <>
                <p className="muted">
                  {t("当前阶段", "Current phase")}:{" "}
                  {snapshot.data?.current_phase || "—"}
                </p>
                <dl>
                  {Object.entries(snapshot.data?.usage_totals ?? {}).map(
                    ([name, value]) => (
                      <div key={name}>
                        <dt>{name.replaceAll("_", " ")}</dt>
                        <dd>
                          {value == null
                            ? t("未知", "Unavailable")
                            : value.toLocaleString(language)}
                        </dd>
                      </div>
                    ),
                  )}
                </dl>
                {snapshot.data?.invocations?.map((item, index) => (
                  <Disclosure
                    key={index}
                    title={`${t("执行批次", "Invocation")} ${String(item.invocation_id ?? index + 1)}`}
                  >
                    <pre>{JSON.stringify(item, null, 2)}</pre>
                  </Disclosure>
                ))}
                {!!snapshot.data?.context_operations?.length && (
                  <Disclosure
                    title={t("压缩与上下文操作", "Context operations")}
                  >
                    <pre>
                      {JSON.stringify(
                        snapshot.data.context_operations,
                        null,
                        2,
                      )}
                    </pre>
                  </Disclosure>
                )}
                {permissions.super_admin && (
                  <a
                    href={`/api/v1/activity/sessions/${id}/logs`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {t("打开会话诊断日志", "Open session logs")} ↗
                  </a>
                )}
              </>
            )}
          </aside>
        )}
      </div>
    </div>
  );
}

function TimelineEntry({
  entry,
  sessionId,
}: {
  entry: Entry;
  sessionId: string;
}) {
  const { t } = useLocale();
  const isMessage = entry.type === "message";
  const title = isMessage
    ? entry.source_role || entry.role
    : entry.type === "tool_call"
      ? entry.name
      : {
          attempt: t("模型调用", "Model call"),
          reasoning: t("推理状态", "Reasoning status"),
          context_operation: t("上下文更新", "Context update"),
          thread_boundary: t("新线程", "New thread"),
          invocation_boundary: t("新的执行", "New invocation"),
        }[entry.type] || entry.type;
  return (
    <article className={`activity-entry activity-entry-${entry.type}`}>
      <header>
        <span>
          <Circle size={8} fill="currentColor" />
          <strong>{title}</strong>
          {entry.status && <StatusBadge value={entry.status} />}
        </span>
        <Timestamp value={entry.created_at} />
      </header>
      {isMessage &&
        (entry.content_visibility === "visible" && entry.content != null ? (
          <div className="activity-message-content">
            <Markdown>{entry.content}</Markdown>
          </div>
        ) : (
          <p className="activity-restricted">
            <LockKeyhole size={14} />
            {t(
              "此内容不可查看或未被记录。",
              "This content is restricted or was not captured.",
            )}
          </p>
        ))}
      {entry.attempt && (
        <div className="activity-attempt">
          <span>
            {String(
                entry.attempt.effective_model || entry.attempt.requested_model || "—",
            )}
          </span>
          <StatusBadge value={String(entry.attempt.status || "")} />
          <Disclosure title={t("调用详情", "Call details")}>
            <pre>{JSON.stringify(entry.attempt, null, 2)}</pre>
          </Disclosure>
        </div>
      )}
      {entry.type === "reasoning" && (
        <p className="muted">
          {entry.thinking_mode || "—"} · {entry.effort || "—"}
          {entry.tokens != null && ` · ${entry.tokens} tokens`}
        </p>
      )}
      {entry.type === "context_operation" && (
        <p className="muted">
          {entry.operation_type} · {entry.trigger_reason} ·{" "}
          {entry.before_revision_id ?? "—"} → {entry.after_revision_id ?? "—"}
        </p>
      )}
      {isMessage && entry.current_context === false && (
        <p className="activity-history-tag">
          {t("已压缩的历史记录", "Compacted history")}
        </p>
      )}
      {entry.artifacts?.map((artifact) => (
        <ArtifactView
          key={artifact.artifact_id}
          artifact={artifact}
          sessionId={sessionId}
        />
      ))}
    </article>
  );
}

function ArtifactView({
  artifact,
  sessionId,
}: {
  artifact: Artifact;
  sessionId: string;
}) {
  const { t } = useLocale();
  const request = useMutation({
    mutationFn: () =>
      api<{
        payload?: string;
        payload_safe_summary?: string;
        payload_unavailable_reason?: string;
      }>(
        `/api/v1/activity/sessions/${sessionId}/artifacts/${artifact.artifact_id}`,
      ),
    gcTime: 0,
  });
  const resetArtifact = request.reset;
  useEffect(() => {
    const clear = () => resetArtifact();
    window.addEventListener("sakura:session-expired", clear);
    return () => window.removeEventListener("sakura:session-expired", clear);
  }, [resetArtifact]);
  if (!artifact.can_request)
    return artifact.capture_error ? (
      <p className="muted">
        {t("详细内容未能记录", "Detailed content was not captured")}
      </p>
    ) : null;
  return (
    <div className="activity-artifact">
      {!request.data ? (
        <button
          className="button"
          disabled={request.isPending}
          onClick={() => request.mutate()}
        >
          <LockKeyhole size={14} />
          {t("查看受保护的内容", "View protected content")} ·{" "}
          {artifact.artifact_kind}
        </button>
      ) : (
        <div>
          <button
            className="icon-button"
            aria-label={t("清除受保护的内容", "Clear protected content")}
            onClick={() => request.reset()}
          >
            <X size={16} />
          </button>
          <pre>
            {request.data.payload ||
              request.data.payload_safe_summary ||
              request.data.payload_unavailable_reason ||
              "—"}
          </pre>
        </div>
      )}
      {request.isError && (
        <p className="danger-text" role="alert">
          {t("此内容当前无法读取。", "This content cannot be read right now.")}
        </p>
      )}
    </div>
  );
}
