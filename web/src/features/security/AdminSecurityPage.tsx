import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { useLocale } from "../../app/locale";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  Section,
  Timestamp,
} from "../../components/workflow/Workflow";
import { MutationError } from "../billing/BillingCommon";

interface User {
  id: number;
  username: string | null;
  role: string;
  active: boolean;
  totp_enabled: boolean;
  mfa_required: boolean;
  recovery_codes: number;
  passkeys: number;
  last_event_at: string | null;
}
interface Event {
  id: number;
  type: string;
  result: string;
  actor_id: number | null;
  user_id: number | null;
  ip_address: string | null;
  created_at: string;
}
interface Key {
  id: number;
  name: string;
  created_at: string;
  last_used_at: string | null;
  backed_up: boolean;
}
const base = "/api/v1/admin/security";

export default function AdminSecurityPage() {
  const { id } = useParams();
  return id ? <UserSecurity id={id} /> : <SecurityOverview />;
}

function SecurityOverview() {
  const { t } = useLocale();
  const [search, setSearch] = useState("");
  const confirm = useConfirm();
  const query = useQuery({
    queryKey: ["security-admin"],
    queryFn: ({ signal }) =>
      api<{ users: User[]; events: Event[]; global_mfa_required: boolean }>(
        base,
        { signal },
      ),
  });
  const mutation = useMutation({
    mutationFn: (required: boolean) =>
      api(base + "/global-mfa", {
        method: "PUT",
        body: JSON.stringify({ required }),
      }),
    onSuccess: () => void query.refetch(),
  });
  if (query.isPending) return <LoadingState />;
  if (query.isError) return <ErrorState retry={() => void query.refetch()} />;
  return (
    <>
      <Section
        title={t("账号安全", "Account security")}
        aside={
          <button
            className="button"
            disabled={mutation.isPending}
            onClick={async () => {
              if (
                await confirm({
                  title: query.data.global_mfa_required
                    ? t(
                        "取消全局强制验证？",
                        "Remove global verification requirement?",
                      )
                    : t(
                        "要求所有用户启用验证？",
                        "Require verification for all users?",
                      ),
                  description: t(
                    "已有的身份验证器与通行密钥会保留。被要求启用验证的用户需要先完成安全设置。",
                    "Existing authenticators and passkeys are retained. Users required to enroll must complete security setup first.",
                  ),
                })
              )
                mutation.mutate(!query.data.global_mfa_required);
            }}
          >
            {query.data.global_mfa_required
              ? t("取消全局强制验证", "Remove global requirement")
              : t("启用全局强制验证", "Require verification globally")}
          </button>
        }
      >
        <MutationError error={mutation.error} />
        <input
          type="search"
          aria-label={t("搜索用户", "Search users")}
          placeholder={t("搜索用户…", "Search users…")}
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <div className="wf-table-wrap">
          <table className="wf-table">
            <thead>
              <tr>
                <th>{t("用户", "User")}</th>
                <th>{t("身份验证器", "Authenticator")}</th>
                <th>{t("通行密钥", "Passkeys")}</th>
                <th>{t("强制验证", "Enrollment required")}</th>
                <th>{t("最近安全事件", "Latest security event")}</th>
              </tr>
            </thead>
            <tbody>
              {query.data.users
                .filter((user) =>
                  `${user.username} ${user.id}`
                    .toLowerCase()
                    .includes(search.toLowerCase()),
                )
                .map((user) => (
                  <tr key={user.id}>
                    <td>
                      <Link to={`/admin/security/${user.id}`}>
                        {user.username || `#${user.id}`}
                      </Link>
                      <div className="muted">{user.role}</div>
                    </td>
                    <td>{user.totp_enabled ? t("已开启", "Enabled") : "—"}</td>
                    <td>{user.passkeys}</td>
                    <td>
                      {user.mfa_required ? t("是", "Yes") : t("否", "No")}
                    </td>
                    <td>
                      <Timestamp value={user.last_event_at} />
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </Section>
      <SecurityEvents events={query.data.events} />
    </>
  );
}

function UserSecurity({ id }: { id: string }) {
  const { t } = useLocale();
  const client = useQueryClient();
  const confirm = useConfirm();
  const query = useQuery({
    queryKey: ["security-user", id],
    queryFn: ({ signal }) =>
      api<{ user: User; passkeys: Key[]; events: Event[] }>(
        `${base}/users/${id}`,
        { signal },
      ),
  });
  const mutation = useMutation({
    mutationFn: ({ path, method }: { path: string; method: string }) =>
      api(`${base}/users/${id}/${path}`, { method }),
    onSuccess: () => {
      void query.refetch();
      void client.invalidateQueries({ queryKey: ["security-admin"] });
    },
  });
  async function apply(path: string, label: string, method = "POST") {
    if (
      await confirm({
        title: label,
        description: t(
          "此操作会改变该账号的登录验证方式，并写入安全审计记录。",
          "This changes the account verification methods and records a security audit event.",
        ),
        destructive: true,
      })
    )
      mutation.mutate({ path, method });
  }
  if (query.isPending) return <LoadingState />;
  if (query.isError) return <ErrorState retry={() => void query.refetch()} />;
  const user = query.data.user;
  return (
    <>
      <Link to="/admin/security">← {t("账号安全", "Account security")}</Link>
      <Section title={user.username || `#${id}`}>
        <MutationError error={mutation.error} />
        <p>
          {t(
            "恢复码 {0} 个，通行密钥 {1} 个。",
            "{0} recovery codes and {1} passkeys.",
            [user.recovery_codes, user.passkeys],
          )}
        </p>
        <div className="wf-actions">
          {[
            {
              id: "reset-totp",
              label: t("重置身份验证器", "Reset authenticator"),
            },
            {
              id: "reset-mfa",
              label: t("重置全部验证方式", "Reset all verification methods"),
            },
            {
              id: user.mfa_required ? "unrequire-mfa" : "require-mfa",
              label: user.mfa_required
                ? t("取消强制验证", "Remove enrollment requirement")
                : t("要求重新验证", "Require enrollment"),
            },
            {
              id: "delete-passkeys",
              label: t("删除全部通行密钥", "Delete all passkeys"),
            },
          ].map((action) => (
            <button
              key={action.id}
              className="button"
              disabled={mutation.isPending}
              onClick={() => void apply(action.id, action.label)}
            >
              {action.label}
            </button>
          ))}
        </div>
      </Section>
      <Section title={t("通行密钥", "Passkeys")}>
        {query.data.passkeys.length ? (
          query.data.passkeys.map((key) => (
            <div className="security-row" key={key.id}>
              <div>
                <h3>{key.name}</h3>
                <Timestamp value={key.last_used_at || key.created_at} />
              </div>
              <button
                className="button danger-text"
                disabled={mutation.isPending}
                onClick={() =>
                  void apply(
                    `passkeys/${key.id}`,
                    t("删除通行密钥 {0}？", "Delete passkey {0}?", [key.name]),
                    "DELETE",
                  )
                }
              >
                {t("删除", "Delete")}
              </button>
            </div>
          ))
        ) : (
          <EmptyState title={t("尚未添加通行密钥", "No passkeys added")} />
        )}
      </Section>
      <SecurityEvents events={query.data.events} />
    </>
  );
}

function SecurityEvents({ events }: { events: Event[] }) {
  const { t } = useLocale();
  return (
    <Section title={t("安全事件", "Security events")}>
      <div className="wf-table-wrap">
        <table className="wf-table">
          <thead>
            <tr>
              <th>{t("事件", "Event")}</th>
              <th>{t("结果", "Result")}</th>
              <th>{t("用户", "User")}</th>
              <th>IP</th>
              <th>{t("时间", "Time")}</th>
            </tr>
          </thead>
          <tbody>
            {events.map((event) => (
              <tr key={event.id}>
                <td>{event.type}</td>
                <td>{event.result}</td>
                <td>{event.user_id ?? "—"}</td>
                <td>{event.ip_address || "—"}</td>
                <td>
                  <Timestamp value={event.created_at} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}
