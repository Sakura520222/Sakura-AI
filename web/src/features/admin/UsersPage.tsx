import { useState, type FormEvent } from "react";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { ArrowLeft, Plus } from "lucide-react";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { toast } from "sonner";
import { api } from "../../api/client";
import { useAuth } from "../../app/auth";
import { useLocale } from "../../app/locale";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import { Dialog } from "../../components/ui/Dialog";
import {
  EmptyState,
  ErrorState,
  Filters,
  FilterSelect,
  LoadingState,
  Pagination,
  Section,
  StatusBadge,
  Timestamp,
} from "../../components/workflow/Workflow";
import type { Envelope, PageData } from "../../components/workflow/types";
import { Field, MutationError } from "../billing/BillingCommon";
import { parseIntegerInput } from "../billing/billingTypes";

interface UserRow {
  id: number;
  github_username: string | null;
  telegram_id: number | null;
  role: string;
  is_active: boolean;
  created_at: string;
  daily_quota: number;
  weekly_quota: number;
  monthly_quota: number;
  daily_used: number;
  weekly_used: number;
  monthly_used: number;
  issue_daily_quota: number;
  issue_weekly_quota: number;
  issue_monthly_quota: number;
  issue_daily_used: number;
  issue_weekly_used: number;
  issue_monthly_used: number;
  usage_logs?: Array<{
    id: number;
    usage_type: string;
    usage_category: string;
    repo_name: string | null;
    created_at: string;
  }>;
}

export default function UsersPage() {
  const { id } = useParams();
  return id ? <UserDetail id={id} key={id} /> : <UserList />;
}

function UserList() {
  const { t } = useLocale();
  const { permissions } = useAuth();
  const [params] = useSearchParams();
  const [creating, setCreating] = useState(false);
  const query = useQuery({
    queryKey: ["users", params.toString()],
    queryFn: ({ signal }) =>
      api<Envelope<PageData<UserRow>>>(`/api/v1/users?${params}`, { signal }),
    placeholderData: keepPreviousData,
  });
  return (
    <>
      <Filters placeholder={t("搜索 GitHub 用户", "Search GitHub users")}>
        <FilterSelect
          name="role"
          label={t("全部角色", "All roles")}
          options={[
            ["user", t("用户", "User")],
            ["admin", t("管理员", "Admin")],
            ["super_admin", t("超级管理员", "Super admin")],
          ]}
        />
      </Filters>
      <Section
        title={t("用户", "Users")}
        aside={
          permissions.super_admin && (
            <button className="wf-button" onClick={() => setCreating(true)}>
              <Plus size={16} />
              {t("添加用户", "Add user")}
            </button>
          )
        }
      >
        {query.isPending ? (
          <LoadingState />
        ) : query.isError ? (
          <ErrorState retry={() => void query.refetch()} />
        ) : (
          query.data && (
            <>
              {query.data.data.items.length ? (
                <div className="wf-table-wrap">
                  <table className="wf-table">
                    <thead>
                      <tr>
                        <th>{t("用户", "User")}</th>
                        <th>{t("角色", "Role")}</th>
                        <th>{t("状态", "Status")}</th>
                        <th>{t("今日 PR", "PRs today")}</th>
                        <th>{t("创建时间", "Created")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {query.data.data.items.map((user) => (
                        <tr key={user.id}>
                          <td>
                            <Link
                              className="wf-record-title"
                              to={`/admin/users/${user.id}`}
                            >
                              {user.github_username || `#${user.id}`}
                            </Link>
                            <span className="wf-record-meta">ID {user.id}</span>
                          </td>
                          <td>{user.role}</td>
                          <td>
                            {user.is_active
                              ? t("已启用", "Active")
                              : t("已停用", "Disabled")}
                          </td>
                          <td>
                            {user.daily_used ?? 0} / {user.daily_quota ?? "—"}
                          </td>
                          <td>
                            <Timestamp value={user.created_at} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <EmptyState title={t("没有匹配的用户", "No matching users")} />
              )}
              <Pagination data={query.data.data} />
            </>
          )
        )}
      </Section>
      <Dialog
        open={creating}
        onOpenChange={setCreating}
        title={t("添加用户", "Add user")}
      >
        <CreateUser
          onComplete={() => {
            setCreating(false);
            void query.refetch();
          }}
        />
      </Dialog>
    </>
  );
}

function CreateUser({ onComplete }: { onComplete: () => void }) {
  const { t } = useLocale();
  const [invalid, setInvalid] = useState(false);
  const create = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api("/api/v1/users", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: onComplete,
  });
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const telegram = String(form.get("telegram_id") || "");
    try {
      create.mutate({
        github_username: String(form.get("github_username")),
        role: String(form.get("role")),
        telegram_id: telegram ? parseIntegerInput(telegram, 1) : null,
      });
      setInvalid(false);
    } catch {
      setInvalid(true);
    }
  }
  return (
    <form className="billing-form" onSubmit={submit}>
      <Field label={t("GitHub 用户名", "GitHub username")}>
        <input name="github_username" required />
      </Field>
      <Field label={t("角色", "Role")}>
        <select name="role">
          <option value="user">{t("用户", "User")}</option>
          <option value="admin">{t("管理员", "Admin")}</option>
          <option value="super_admin">{t("超级管理员", "Super admin")}</option>
        </select>
      </Field>
      <Field label={t("Telegram ID（可选）", "Telegram ID (optional)")}>
        <input name="telegram_id" inputMode="numeric" />
      </Field>
      {invalid && (
        <p role="alert">
          {t("请输入有效的整数 ID。", "Enter a valid integer ID.")}
        </p>
      )}
      <MutationError error={create.error} />
      <button
        className="wf-button wf-button-primary"
        disabled={create.isPending}
      >
        {t("创建用户", "Create user")}
      </button>
    </form>
  );
}

function UserDetail({ id }: { id: string }) {
  const { t } = useLocale();
  const auth = useAuth();
  const confirm = useConfirm();
  const navigate = useNavigate();
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["user", id],
    queryFn: ({ signal }) =>
      api<Envelope<UserRow>>(`/api/v1/users/${encodeURIComponent(id)}`, {
        signal,
      }),
  });
  const change = useMutation({
    mutationFn: ({
      action,
      body,
      method = "PATCH",
    }: {
      action: string;
      body?: unknown;
      method?: string;
    }) =>
      api<Envelope<unknown>>(
        `/api/v1/users/${encodeURIComponent(id)}${action}`,
        { method, body: body == null ? undefined : JSON.stringify(body) },
      ),
    onSuccess: (_, input) => {
      void client.invalidateQueries({ queryKey: ["user", id] });
      void client.invalidateQueries({ queryKey: ["users"] });
      toast.success(t("用户已更新", "User updated"));
      if (input.method === "DELETE") navigate("/admin/users");
    },
  });
  async function action(
    path: string,
    label: string,
    method = "POST",
    body?: unknown,
  ) {
    if (
      await confirm({
        title: label,
        description:
          method === "DELETE"
            ? t(
                "有财务记录的用户将停用并保留账务；其他用户将被删除。",
                "Users with financial records will be disabled while their records are retained. Other users will be deleted.",
              )
            : t(
                "该变更将立即应用于此用户。",
                "This change takes effect immediately for this user.",
              ),
        confirmLabel: label,
        destructive: true,
      })
    )
      change.mutate({ action: path, method, body });
  }
  if (query.isPending) return <LoadingState />;
  if (query.isError || !query.data)
    return <ErrorState retry={() => void query.refetch()} />;
  const user = query.data.data;
  const mayToggle =
    user.id !== auth.user.id &&
    (auth.permissions.super_admin || user.role === "user");
  return (
    <>
      <Link className="wf-back" to="/admin/users">
        <ArrowLeft size={15} />
        {t("所有用户", "All users")}
      </Link>
      <Section
        title={user.github_username || `#${user.id}`}
        aside={<StatusBadge value={user.is_active ? "active" : "disabled"} />}
      >
        <dl className="wf-detail-pairs">
          <div>
            <dt>ID</dt>
            <dd>{user.id}</dd>
          </div>
          <div>
            <dt>{t("角色", "Role")}</dt>
            <dd>{user.role}</dd>
          </div>
          <div>
            <dt>Telegram ID</dt>
            <dd>
              {user.telegram_id && user.telegram_id > 0
                ? user.telegram_id
                : "—"}
            </dd>
          </div>
        </dl>
        <div className="billing-form">
          <MutationError error={change.error} />
          <div className="wf-actions">
            {mayToggle && (
              <button
                className="wf-button"
                disabled={change.isPending}
                onClick={() =>
                  void action(
                    "/toggle",
                    user.is_active
                      ? t("停用用户", "Disable user")
                      : t("启用用户", "Enable user"),
                  )
                }
              >
                {user.is_active
                  ? t("停用用户", "Disable user")
                  : t("启用用户", "Enable user")}
              </button>
            )}
            {auth.permissions.super_admin && user.id !== auth.user.id && (
              <button
                className="wf-button"
                disabled={change.isPending}
                onClick={() =>
                  void action("", t("移除用户", "Remove user"), "DELETE")
                }
              >
                {t("移除用户", "Remove user")}
              </button>
            )}
          </div>
        </div>
      </Section>
      {auth.permissions.super_admin && (
        <Section title={t("账户信息", "Account details")}>
          <form
            className="billing-form"
            onSubmit={(event) => {
              event.preventDefault();
              const form = new FormData(event.currentTarget);
              const value = String(form.get("telegram_id") || "");
              try {
                change.mutate({
                  action: "/info",
                  body: {
                    github_username: String(form.get("github_username")),
                    telegram_id: value ? parseIntegerInput(value, 1) : null,
                  },
                });
              } catch {
                toast.error(
                  t("请输入有效的整数 ID。", "Enter a valid integer ID."),
                );
              }
            }}
          >
            <div className="billing-form-grid">
              <Field label={t("GitHub 用户名", "GitHub username")}>
                <input
                  name="github_username"
                  required
                  defaultValue={user.github_username ?? ""}
                />
              </Field>
              <Field
                label={t(
                  "Telegram ID（留空解绑）",
                  "Telegram ID (blank to unbind)",
                )}
              >
                <input
                  name="telegram_id"
                  inputMode="numeric"
                  defaultValue={
                    user.telegram_id && user.telegram_id > 0
                      ? user.telegram_id
                      : ""
                  }
                />
              </Field>
            </div>
            <button className="wf-button" disabled={change.isPending}>
              {t("保存账户信息", "Save account details")}
            </button>
          </form>
          <form
            className="billing-form"
            onSubmit={(event) => {
              event.preventDefault();
              void action("/role", t("更改角色", "Change role"), "PATCH", {
                role: new FormData(event.currentTarget).get("role"),
              });
            }}
          >
            <Field label={t("角色", "Role")}>
              <select name="role" defaultValue={user.role}>
                <option value="user">{t("用户", "User")}</option>
                <option value="admin">{t("管理员", "Admin")}</option>
                <option value="super_admin">
                  {t("超级管理员", "Super admin")}
                </option>
              </select>
            </Field>
            <button className="wf-button" disabled={change.isPending}>
              {t("更改角色", "Change role")}
            </button>
          </form>
        </Section>
      )}
      <QuotaForm user={user} type="pr" />
      <QuotaForm user={user} type="issue" />
      <Section title={t("最近配额使用", "Recent quota usage")}>
        {user.usage_logs?.length ? (
          <div className="wf-table-wrap">
            <table className="wf-table">
              <thead>
                <tr>
                  <th>{t("类型", "Type")}</th>
                  <th>{t("仓库", "Repository")}</th>
                  <th>{t("时间", "Date")}</th>
                </tr>
              </thead>
              <tbody>
                {user.usage_logs.map((log) => (
                  <tr key={log.id}>
                    <td>
                      {log.usage_type} · {log.usage_category}
                    </td>
                    <td>{log.repo_name || "—"}</td>
                    <td>
                      <Timestamp value={log.created_at} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title={t("暂无使用记录", "No usage yet")} />
        )}
      </Section>
    </>
  );
}

function QuotaForm({ user, type }: { user: UserRow; type: "pr" | "issue" }) {
  const { t } = useLocale();
  const client = useQueryClient();
  const [invalid, setInvalid] = useState(false);
  const prefix = type === "issue" ? "issue_" : "";
  const mutation = useMutation({
    mutationFn: (body: Record<string, number>) =>
      api(
        `/api/v1/users/${user.id}/${type === "issue" ? "issue-quota" : "quota"}`,
        { method: "PATCH", body: JSON.stringify(body) },
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["user", String(user.id)] });
      toast.success(t("配额已更新", "Quota updated"));
    },
  });
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      const body: Record<string, number> = {};
      for (const [key, value] of new FormData(event.currentTarget))
        body[key] = parseIntegerInput(String(value));
      mutation.mutate(body);
      setInvalid(false);
    } catch {
      setInvalid(true);
    }
  }
  return (
    <Section
      title={
        type === "pr"
          ? t("PR 配额", "PR quota")
          : t("Issue 配额", "Issue quota")
      }
    >
      <form className="billing-form" onSubmit={submit}>
        <div className="billing-form-grid">
          {[
            ["daily", t("每日", "Daily")],
            ["weekly", t("每周", "Weekly")],
            ["monthly", t("每月", "Monthly")],
          ].map(([period, label]) => (
            <Field key={period} label={label}>
              <input
                name={`${prefix}${period}_quota`}
                type="number"
                min="0"
                step="1"
                required
                defaultValue={String(
                  user[`${prefix}${period}_quota` as keyof UserRow] ?? 0,
                )}
              />
            </Field>
          ))}
        </div>
        {invalid && (
          <p role="alert">
            {t("配额必须是非负整数。", "Quota must be a nonnegative integer.")}
          </p>
        )}
        <MutationError error={mutation.error} />
        <button className="wf-button" disabled={mutation.isPending}>
          {t("保存配额", "Save quota")}
        </button>
      </form>
    </Section>
  );
}
