import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  Link,
  Navigate,
  useLocation,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { api } from "../../api/client";
import { useAuth } from "../../app/auth";
import { useLocale } from "../../app/locale";
import { Dialog } from "../../components/ui/Dialog";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import {
  Disclosure,
  EmptyState,
  ErrorState,
  LoadingState,
  Markdown,
  PageHeader,
  Pagination,
  Section,
  StatusBadge,
  Timestamp,
} from "../../components/workflow/Workflow";
import { MutationError } from "../billing/BillingCommon";

interface Announcement {
  id: number;
  title: string;
  content: string;
  type: string;
  status: string;
  published_at: string | null;
  created_at: string;
  is_read?: boolean;
  read?: boolean;
  delivery?: Record<string, unknown>;
  publication_history?: unknown[];
}
interface Page {
  items: Announcement[];
  page: number;
  total_pages: number;
  total: number;
}
const base = "/api/v1/announcements";

export default function AnnouncementsPage({
  admin = false,
  embedded = false,
}: {
  admin?: boolean;
  embedded?: boolean;
}) {
  const { id } = useParams();
  return id && !admin ? (
    <AnnouncementDetail key={id} id={id} embedded={embedded} />
  ) : (
    <AnnouncementList admin={admin} embedded={embedded} />
  );
}

export function AnnouncementRedirect() {
  const { id } = useParams();
  const { search, hash } = useLocation();
  return (
    <Navigate
      replace
      to={{
        pathname: `/notifications/announcements${id ? "/" + encodeURIComponent(id) : ""}`,
        search,
        hash,
      }}
    />
  );
}

function AnnouncementDetail({
  id,
  embedded,
}: {
  id: string;
  embedded: boolean;
}) {
  const { t } = useLocale();
  const client = useQueryClient();
  const marked = useRef(false);
  const query = useQuery({
    queryKey: ["announcement", id],
    queryFn: ({ signal }) => api<Announcement>(`${base}/${id}`, { signal }),
  });
  const read = useMutation({
    mutationFn: () => api(`${base}/${id}/read`, { method: "POST" }),
    onSuccess: () =>
      void client.invalidateQueries({ queryKey: ["announcements"] }),
  });
  useEffect(() => {
    if (query.data && !marked.current) {
      marked.current = true;
      read.mutate();
    }
  }, [query.data, read.mutate]);
  if (query.isPending) return <LoadingState />;
  if (query.isError) return <ErrorState retry={() => void query.refetch()} />;
  return (
    <div>
      <Link to="/notifications/announcements">
        ← {t("公告", "Announcements")}
      </Link>
      {embedded ? (
        <h2 className="announcement-detail-title">{query.data.title}</h2>
      ) : (
        <PageHeader title={query.data.title} />
      )}
      <Timestamp value={query.data.published_at} />
      <Markdown>{query.data.content}</Markdown>
    </div>
  );
}

function AnnouncementList({
  admin,
  embedded,
}: {
  admin: boolean;
  embedded: boolean;
}) {
  const { t } = useLocale();
  const { permissions } = useAuth();
  const confirm = useConfirm();
  const [params, setParams] = useSearchParams();
  const [editing, setEditing] = useState<Announcement | "new" | null>(null);
  const query = useQuery({
    queryKey: ["announcements", admin, params.toString()],
    queryFn: ({ signal }) =>
      api<Page>(
        `${base}${admin ? "/admin" : params.get("unread") ? "/unread" : ""}?page=${params.get("page") || 1}&per_page=20`,
        { signal },
      ),
    enabled: !admin || permissions.super_admin,
  });
  const mutate = useMutation({
    mutationFn: (path: string) =>
      api(base + path.replace(/:delete$/, ""), {
        method: path.endsWith(":delete") ? "DELETE" : "POST",
      }),
    onSuccess: () => void query.refetch(),
  });
  if (admin && !permissions.super_admin)
    return (
      <EmptyState
        title={t("需要超级管理员权限", "Super administrator access required")}
      />
    );
  const actions = (
    <div className="wf-actions">
      {admin ? (
        <button className="button primary" onClick={() => setEditing("new")}>
          <Plus size={16} />
          {t("创建公告", "Create announcement")}
        </button>
      ) : (
        <>
          <button
            className="button"
            onClick={() =>
              setParams(params.get("unread") ? {} : { unread: "1" })
            }
          >
            {params.get("unread")
              ? t("查看全部", "Show all")
              : t("只看未读", "Unread only")}
          </button>
          <button
            className="button"
            disabled={mutate.isPending}
            onClick={() => mutate.mutate("/read-all")}
          >
            {t("全部标为已读", "Mark all read")}
          </button>
          {permissions.super_admin && (
            <Link className="button" to="/admin/announcements">
              {t("管理公告", "Manage announcements")}
            </Link>
          )}
        </>
      )}
    </div>
  );
  return (
    <>
      {admin ? (
        <Section
          title={t("公告管理", "Announcement management")}
          aside={actions}
        >
          <p className="muted">
            {t(
              "编辑、发布和管理公告投递。",
              "Edit, publish and manage announcement delivery.",
            )}
          </p>
        </Section>
      ) : embedded ? (
        <div className="notification-announcement-actions">{actions}</div>
      ) : (
        <PageHeader title={t("公告", "Announcements")} actions={actions} />
      )}
      <MutationError error={mutate.error} />
      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState retry={() => void query.refetch()} />
      ) : !query.data.items.length ? (
        <EmptyState title={t("暂无公告", "No announcements")} />
      ) : (
        <>
          {query.data.items.map((item) => (
            <Section
              key={item.id}
              title={item.title}
              aside={<StatusBadge value={item.status} />}
            >
              <Timestamp value={item.published_at || item.created_at} />
              {admin ? (
                <Disclosure title={t("预览内容", "Preview content")}>
                  <Markdown>{item.content}</Markdown>
                </Disclosure>
              ) : (
                <p>
                  <Link to={`/notifications/announcements/${item.id}`}>
                    {t("阅读全文", "Read announcement")} →
                  </Link>
                </p>
              )}
              {admin && (
                <div className="wf-actions">
                  <button className="button" onClick={() => setEditing(item)}>
                    {t("编辑", "Edit")}
                  </button>
                  {[
                    {
                      id: item.status === "published" ? "withdraw" : "publish",
                      label:
                        item.status === "published"
                          ? t("撤回", "Withdraw")
                          : t("发布", "Publish"),
                    },
                    ...(item.status === "published"
                      ? [
                          {
                            id: "retry",
                            label: t("重试失败投递", "Retry failed deliveries"),
                          },
                        ]
                      : []),
                    { id: ":delete", label: t("删除", "Delete") },
                  ].map((action) => (
                    <button
                      key={action.id}
                      className="button"
                      disabled={mutate.isPending}
                      onClick={async () => {
                        if (
                          await confirm({
                            title: `${action.label} · ${item.title}`,
                            description:
                              action.id === "publish"
                                ? t(
                                    "公告将按用户通知偏好发布和投递。",
                                    "The announcement will be published and delivered according to user preferences.",
                                  )
                                : t(
                                    "请确认执行此公告操作。",
                                    "Confirm this announcement action.",
                                  ),
                            destructive: action.id !== "publish",
                          })
                        )
                          mutate.mutate(
                            `/admin/${item.id}${action.id.startsWith(":") ? action.id : "/" + action.id}`,
                          );
                      }}
                    >
                      {action.label}
                    </button>
                  ))}
                </div>
              )}
              {admin && item.delivery && (
                <Disclosure title={t("投递统计", "Delivery statistics")}>
                  <pre>{JSON.stringify(item.delivery, null, 2)}</pre>
                </Disclosure>
              )}
              {admin && !!item.publication_history?.length && (
                <Disclosure title={t("发布历史", "Publication history")}>
                  <pre className="agent-pre">
                    {JSON.stringify(item.publication_history, null, 2)}
                  </pre>
                </Disclosure>
              )}
            </Section>
          ))}
          <Pagination data={query.data} />
        </>
      )}
      <Dialog
        open={editing !== null}
        onOpenChange={(open) => {
          if (!open) setEditing(null);
        }}
        title={t("编辑公告", "Edit announcement")}
      >
        {editing && (
          <AnnouncementEditor
            key={editing === "new" ? "new" : editing.id}
            value={editing === "new" ? undefined : editing}
            onComplete={() => {
              setEditing(null);
              void query.refetch();
            }}
          />
        )}
      </Dialog>
    </>
  );
}

function AnnouncementEditor({
  value,
  onComplete,
}: {
  value?: Announcement;
  onComplete: () => void;
}) {
  const { t } = useLocale();
  const confirm = useConfirm();
  const mutation = useMutation({
    mutationFn: (body: unknown) =>
      api(`${base}/admin${value ? "/" + value.id : ""}`, {
        method: value ? "PATCH" : "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: onComplete,
  });
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    const publish =
      (event.nativeEvent as SubmitEvent).submitter?.getAttribute("value") ===
      "publish";
    if (
      publish &&
      !(await confirm({
        title: t("发布公告？", "Publish announcement?"),
        description: t(
          "发布后会开始向符合条件的订阅者投递通知。",
          "Publication starts delivery to eligible subscribers.",
        ),
      }))
    )
      return;
    mutation.mutate({
      title: fields.get("title"),
      content: fields.get("content"),
      type: fields.get("type"),
      publish,
    });
  }
  return (
    <form className="billing-form" onSubmit={submit}>
      <label>
        {t("标题", "Title")}
        <input
          name="title"
          required
          maxLength={500}
          defaultValue={value?.title ?? ""}
        />
      </label>
      <label>
        {t("类型", "Type")}
        <select name="type" defaultValue={value?.type || "general"}>
          {[
            ["general", t("常规", "General")],
            ["important", t("重要", "Important")],
            ["feature", t("功能", "Feature")],
            ["maintenance", t("维护", "Maintenance")],
            ["release", t("版本发布", "Release")],
          ].map(([id, label]) => (
            <option key={id} value={id}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <label>
        {t("内容（Markdown）", "Content (Markdown)")}
        <textarea
          name="content"
          rows={12}
          required
          defaultValue={value?.content ?? ""}
        />
      </label>
      <MutationError error={mutation.error} />
      <div className="confirm-actions">
        <button className="button" value="draft" disabled={mutation.isPending}>
          {t("保存", "Save")}
        </button>
        <button
          className="button primary"
          value="publish"
          disabled={mutation.isPending}
        >
          {t("保存并发布", "Save and publish")}
        </button>
      </div>
    </form>
  );
}
