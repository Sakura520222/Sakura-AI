import { useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, CheckCheck } from "lucide-react";
import { toast } from "sonner";
import { api } from "../../api/client";
import { useLocale } from "../../app/locale";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  RefreshButton,
  Timestamp,
} from "../../components/workflow/Workflow";
import NotificationsPanel, { eventLabel } from "./NotificationsPanel";
import AnnouncementsPage from "../announcements/AnnouncementsPage";
import "./notifications.css";

interface Inbox {
  items: Array<{
    id: number;
    type: string;
    title: string;
    content: string;
    severity: string;
    detail_url: string | null;
    read: boolean;
    occurred_at: string;
  }>;
  has_next: boolean;
  page: number;
  unread: number;
}
export function safeDetailPath(value: string | null): string | undefined {
  if (!value?.startsWith("/") || value.startsWith("//") || value.includes("\\"))
    return undefined;
  try {
    const url = new URL(value, window.location.origin);
    return url.origin === window.location.origin
      ? url.pathname + url.search + url.hash
      : undefined;
  } catch {
    return undefined;
  }
}

export default function NotificationsPage() {
  const { t } = useLocale();
  const client = useQueryClient();
  const { pathname } = useLocation();
  const tab = pathname.startsWith("/notifications/announcements")
    ? "announcements"
    : pathname === "/notifications/preferences"
      ? "preferences"
      : "inbox";
  const [page, setPage] = useState(1);
  const query = useQuery({
    queryKey: ["notification-inbox", page],
    queryFn: () => api<Inbox>(`/api/v1/notifications/history?page=${page}`),
    enabled: tab === "inbox",
  });
  const read = useMutation({
    mutationFn: (id: number | "all") =>
      api(`/api/v1/notifications/${id === "all" ? "read-all" : `${id}/read`}`, {
        method: "POST",
      }),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: ["notification-inbox"] }),
    onError: () =>
      toast.error(t("更新失败，请重试", "Unable to update. Try again.")),
  });
  return (
    <div>
      <PageHeader
        title={t("通知中心", "Notification center")}
        description={t(
          "任务进展、账户消息与管理员公告。",
          "Task updates, account messages and announcements.",
        )}
        actions={
          tab === "inbox" && (
            <>
              <RefreshButton
                busy={query.isFetching}
                onClick={() => query.refetch()}
              />
              <button
                className="button"
                disabled={read.isPending || !query.data?.unread}
                onClick={() => read.mutate("all")}
              >
                <CheckCheck size={16} />
                {t("全部已读", "Mark all read")}
              </button>
            </>
          )
        }
      />
      <nav
        className="notification-tabs"
        aria-label={t("通知视图", "Notification view")}
      >
        <NavLink to="/notifications" end>
          {t("消息", "Messages")}
          {query.data?.unread ? <span>{query.data.unread}</span> : null}
        </NavLink>
        <NavLink to="/notifications/announcements">
          {t("公告", "Announcements")}
        </NavLink>
        <NavLink to="/notifications/preferences">
          {t("渠道与偏好", "Channels & preferences")}
        </NavLink>
      </nav>
      {tab === "announcements" ? (
        <AnnouncementsPage embedded />
      ) : tab === "preferences" ? (
        <NotificationsPanel />
      ) : query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState retry={() => query.refetch()} />
      ) : (
        <>
          <div className="notification-inbox">
            {query.data.items.length === 0 ? (
              <EmptyState
                title={t("暂时没有通知", "You are all caught up")}
                description={t(
                  "新的任务进展和账户消息会显示在这里。",
                  "New task updates and account messages will appear here.",
                )}
              />
            ) : (
              query.data.items.map((item) => (
                <article
                  className={`notification-item ${item.read ? "" : "unread"}`}
                  key={item.id}
                >
                  <Bell size={20} />
                  <div>
                    <div className="notification-item-meta">
                      <span>{eventLabel(item.type, t)}</span>
                      <Timestamp value={item.occurred_at} />
                    </div>
                    <h2>{item.title}</h2>
                    <p>{item.content}</p>
                    <div className="notification-actions">
                      {safeDetailPath(item.detail_url) && (
                        <a
                          href={safeDetailPath(item.detail_url)}
                          onClick={() => {
                            if (!item.read) read.mutate(item.id);
                          }}
                        >
                          {t("查看详情", "View details")}
                        </a>
                      )}
                      {!item.read && (
                        <button
                          className="button"
                          disabled={read.isPending}
                          onClick={() => read.mutate(item.id)}
                        >
                          {t("标为已读", "Mark read")}
                        </button>
                      )}
                    </div>
                  </div>
                </article>
              ))
            )}
          </div>
          <nav
            className="notification-pagination"
            aria-label={t("通知分页", "Notification pages")}
          >
            <button
              className="button"
              disabled={page <= 1 || query.isFetching}
              onClick={() => setPage(page - 1)}
            >
              {t("上一页", "Previous")}
            </button>
            <span>{page}</span>
            <button
              className="button"
              disabled={!query.data.has_next || query.isFetching}
              onClick={() => setPage(page + 1)}
            >
              {t("下一页", "Next")}
            </button>
          </nav>
        </>
      )}
    </div>
  );
}
