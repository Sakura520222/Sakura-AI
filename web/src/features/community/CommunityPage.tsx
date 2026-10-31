import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Star, RefreshCw } from "lucide-react";
import { api } from "../../api/client";
import { useLocale } from "../../app/locale";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import {
  Disclosure,
  EmptyState,
  ErrorState,
  LoadingState,
  Markdown,
  PageHeader,
  Section,
  StatusBadge,
  safeExternalUrl,
} from "../../components/workflow/Workflow";
import { MutationError } from "../billing/BillingCommon";
import "./community.css";

interface Repository {
  id: number;
  full_name: string;
  html_url: string;
  description: string | null;
  ai_summary: string | null;
  ai_summary_status: string | null;
  stargazers_count: number;
  primary_language: string | null;
  topics: string[];
  is_displayed: boolean;
  disabled_by_admin: boolean;
}
interface Member {
  user_id: number;
  github_username: string;
  status: string;
  daily_star_used: number;
  daily_star_limit: number;
}
interface Page {
  page: number;
  total_pages: number;
  total: number;
}
interface State {
  feature_enabled: boolean;
  auto_star_enabled: boolean;
  is_admin: boolean;
  is_super_admin: boolean;
  member: Member | null;
  credential_status: string;
  daily_star_used: number;
  daily_star_limit: number;
  available_repos: Repository[];
  displayed_repos: Repository[];
  public_repos: Repository[];
  admin_members: Member[] | null;
  admin_member_page: Page | null;
  admin_repositories: Repository[] | null;
  admin_repository_page: Page | null;
}
const base = "/api/v1/community";

export default function CommunityPage() {
  const { t } = useLocale();
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = useState("");
  const [selection, setSelection] = useState<string[] | null>(null);
  const [visible, setVisible] = useState(20);
  const [reason, setReason] = useState("");
  const confirm = useConfirm();
  const query = useQuery({
    queryKey: ["community", params.toString()],
    queryFn: ({ signal }) => api<State>(`${base}?${params}`, { signal }),
  });
  const mutation = useMutation({
    mutationFn: ({
      path,
      body = {},
      method = "POST",
    }: {
      path: string;
      body?: unknown;
      method?: string;
    }) =>
      api<{ success?: boolean; redirect?: string }>(base + path, {
        method,
        body: JSON.stringify(body),
      }),
    onSuccess: (result) => {
      if (result.redirect?.startsWith("/star-aid/auth/start?"))
        window.location.assign(result.redirect);
      else {
        setSelection(null);
        void query.refetch();
      }
    },
  });
  function parameter(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    next.set(key.startsWith("member_") ? "member_page" : "page", "1");
    setParams(next);
  }
  async function action(
    path: string,
    title: string,
    body: unknown = {},
    destructive = false,
  ) {
    if (
      await confirm({
        title,
        description: t(
          "此操作会更新仓库互助状态。",
          "This updates the repository community state.",
        ),
        destructive,
      })
    )
      mutation.mutate({
        path,
        body: path.startsWith("/admin/") ? { reason } : body,
      });
  }
  if (query.isPending) return <LoadingState />;
  if (query.isError) return <ErrorState retry={() => void query.refetch()} />;
  const data = query.data,
    chosen = selection ?? data.displayed_repos.map((repo) => repo.full_name);
  const repos = data.public_repos.filter((repo) =>
    `${repo.full_name} ${repo.description || ""}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  const joined = data.member?.status === "active";
  return (
    <div>
      <PageHeader
        title={t("仓库互助", "Repository community")}
        description={t(
          "发现社区项目，管理你的展示仓库与互助参与状态。",
          "Discover community projects and manage the repositories you share.",
        )}
        actions={
          <button
            className="button"
            disabled={query.isFetching}
            onClick={() => void query.refetch()}
          >
            <RefreshCw size={16} />
            {t("刷新", "Refresh")}
          </button>
        }
      />
      <MutationError error={mutation.error} />
      <div className="community-membership">
        <div>
          <strong>
            {data.feature_enabled
              ? t("仓库互助已开启", "Repository community is enabled")
              : t("仓库互助已关闭", "Repository community is disabled")}
          </strong>
          <p>
            {t(
              "今日自动互助：{0} / {1}",
              "Automatic activity today: {0} / {1}",
              [data.daily_star_used, data.daily_star_limit],
            )}
          </p>
          {data.member && <StatusBadge value={data.member.status} />}
        </div>
        <div className="wf-actions">
          {data.credential_status !== "authorized" && (
            <a
              className="button primary"
              href="/star-aid/auth/start?intent=github_app&return_to=/app/community"
            >
              {t("授权 GitHub", "Authorize GitHub")}
            </a>
          )}
          {data.is_super_admin && (
            <button
              className="button"
              disabled={mutation.isPending}
              onClick={async () => {
                if (
                  await confirm({
                    title: data.feature_enabled
                      ? t("关闭仓库互助？", "Disable repository community?")
                      : t("开启仓库互助？", "Enable repository community?"),
                    description: t(
                      "更改全局仓库互助开关。",
                      "Change the global repository community setting.",
                    ),
                  })
                )
                  mutation.mutate({
                    path: "/feature",
                    method: "PUT",
                    body: { enabled: !data.feature_enabled },
                  });
              }}
            >
              {data.feature_enabled
                ? t("关闭功能", "Disable feature")
                : t("开启功能", "Enable feature")}
            </button>
          )}
        </div>
      </div>
      <Section
        title={t("我的展示仓库", "My shared repositories")}
        aside={
          <button
            className="button"
            disabled={mutation.isPending || !data.feature_enabled}
            onClick={() => mutation.mutate({ path: "/membership/sync" })}
          >
            {t("同步 GitHub 仓库", "Sync GitHub repositories")}
          </button>
        }
      >
        <div className="community-selection">
          {data.available_repos.map((repo) => (
            <label key={repo.id}>
              <input
                type="checkbox"
                checked={chosen.includes(repo.full_name)}
                disabled={repo.disabled_by_admin || mutation.isPending}
                onChange={() =>
                  setSelection(
                    chosen.includes(repo.full_name)
                      ? chosen.filter((value) => value !== repo.full_name)
                      : [...chosen, repo.full_name],
                  )
                }
              />
              <span>{repo.full_name}</span>
            </label>
          ))}
        </div>
        <div className="wf-actions">
          <button
            className="button primary"
            disabled={
              mutation.isPending ||
              !data.feature_enabled ||
              data.credential_status !== "authorized"
            }
            onClick={() =>
              void action(
                `/membership/${joined ? "repositories" : "join"}`,
                joined
                  ? t("保存展示仓库？", "Save shared repositories?")
                  : t("加入仓库互助计划？", "Join repository community?"),
                { repositories: chosen },
              )
            }
          >
            {joined
              ? t("保存选择", "Save selection")
              : t("加入计划", "Join community")}
          </button>
          {joined && (
            <>
              <button
                className="button"
                disabled={mutation.isPending}
                onClick={() =>
                  void action(
                    "/membership/leave",
                    t("退出互助计划？", "Leave repository community?"),
                    { unstar_created: false },
                    true,
                  )
                }
              >
                {t("退出计划", "Leave community")}
              </button>
              <button
                className="button danger-text"
                disabled={mutation.isPending}
                onClick={() =>
                  void action(
                    "/membership/leave",
                    t(
                      "退出并撤销本功能添加的 Star？",
                      "Leave and remove stars added by this feature?",
                    ),
                    { unstar_created: true },
                    true,
                  )
                }
              >
                {t("退出并撤销 Star", "Leave and remove stars")}
              </button>
            </>
          )}
        </div>
      </Section>
      <Section
        title={t("社区项目", "Community projects")}
        aside={
          <input
            type="search"
            aria-label={t("搜索社区项目", "Search community projects")}
            placeholder={t("搜索仓库…", "Search repositories…")}
            value={search}
            onChange={(event) => {
              setSearch(event.target.value);
              setVisible(20);
            }}
          />
        }
      >
        {!repos.length ? (
          <EmptyState title={t("暂无匹配的项目", "No matching projects")} />
        ) : (
          <div className="community-grid">
            {repos.slice(0, visible).map((repo) => (
              <article key={repo.id}>
                <header>
                  <a
                    href={safeExternalUrl(repo.html_url)}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {repo.full_name}
                  </a>
                  <span>★ {repo.stargazers_count}</span>
                </header>
                <p>{repo.description}</p>
                {repo.ai_summary && (
                  <Disclosure title={t("项目摘要", "Project summary")}>
                    <Markdown>{repo.ai_summary}</Markdown>
                  </Disclosure>
                )}
                <footer>
                  <span>{repo.primary_language}</span>
                  <button
                    className="button"
                    disabled={mutation.isPending || !data.feature_enabled}
                    onClick={() =>
                      void action(
                        `/repositories/${repo.id}/star`,
                        t("为 {0} 添加 Star？", "Star {0}?", [repo.full_name]),
                      )
                    }
                  >
                    <Star size={15} />
                    Star
                  </button>
                  {(data.is_admin ||
                    data.available_repos.some(
                      (item) => item.id === repo.id,
                    )) && (
                    <button
                      className="button"
                      disabled={mutation.isPending}
                      onClick={() =>
                        mutation.mutate({
                          path: `/repositories/${repo.id}/summary`,
                        })
                      }
                    >
                      {t("刷新摘要", "Refresh summary")}
                    </button>
                  )}
                </footer>
              </article>
            ))}
          </div>
        )}
        {repos.length > visible && (
          <button className="button" onClick={() => setVisible(visible + 20)}>
            {t("显示更多", "Show more")}
          </button>
        )}
      </Section>
      {data.is_admin && (
        <>
          <Section title={t("管理备注", "Moderation note")}>
            <label>
              {t(
                "封禁或停用原因（可选）",
                "Reason for banning or disabling (optional)",
              )}
              <input
                value={reason}
                maxLength={1000}
                onChange={(event) => setReason(event.target.value)}
              />
            </label>
          </Section>
          <Section title={t("成员管理", "Members")}>
            <div className="wf-actions">
              <input
                aria-label={t("搜索成员", "Search members")}
                placeholder={t("搜索成员…", "Search members…")}
                defaultValue={params.get("member_q") || ""}
                onBlur={(event) => parameter("member_q", event.target.value)}
              />
              <select
                aria-label={t("成员状态", "Member status")}
                value={params.get("member_status") || "all"}
                onChange={(event) =>
                  parameter("member_status", event.target.value)
                }
              >
                {["all", "active", "paused", "banned", "reauth_required"].map(
                  (value) => (
                    <option key={value}>{value}</option>
                  ),
                )}
              </select>
            </div>
            <div className="wf-table-wrap">
              <table className="wf-table">
                <tbody>
                  {data.admin_members?.map((member) => (
                    <tr key={member.user_id}>
                      <td>{member.github_username}</td>
                      <td>
                        <StatusBadge value={member.status} />
                      </td>
                      <td>
                        {member.daily_star_used} / {member.daily_star_limit}
                      </td>
                      <td>
                        <button
                          className="button"
                          disabled={mutation.isPending}
                          onClick={() =>
                            void action(
                              `/admin/members/${member.user_id}/${member.status === "banned" ? "unban" : "ban"}`,
                              member.status === "banned"
                                ? t("解除封禁？", "Unban member?")
                                : t("封禁成员？", "Ban member?"),
                              {},
                              true,
                            )
                          }
                        >
                          {member.status === "banned"
                            ? t("解封", "Unban")
                            : t("封禁", "Ban")}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <CommunityPagination
              page={data.admin_member_page}
              onPage={(page) => {
                const next = new URLSearchParams(params);
                next.set("member_page", String(page));
                setParams(next);
              }}
            />
          </Section>
          <Section title={t("展示仓库管理", "Shared repository management")}>
            <div className="wf-actions">
              <input
                aria-label={t("搜索管理仓库", "Search managed repositories")}
                placeholder={t("搜索仓库…", "Search repositories…")}
                defaultValue={params.get("q") || ""}
                onBlur={(event) => parameter("q", event.target.value)}
              />
              <select
                aria-label={t("仓库状态", "Repository status")}
                value={params.get("status") || "all"}
                onChange={(event) => parameter("status", event.target.value)}
              >
                {["all", "displayed", "not_displayed", "disabled"].map(
                  (value) => (
                    <option key={value}>{value}</option>
                  ),
                )}
              </select>
              <select
                aria-label={t("排序方式", "Sort by")}
                value={params.get("sort") || "stars"}
                onChange={(event) => parameter("sort", event.target.value)}
              >
                <option value="stars">{t("Star 数", "Stars")}</option>
                <option value="name">{t("仓库名称", "Repository name")}</option>
                <option value="pushed_at">{t("最近推送", "Last push")}</option>
                <option value="created_at">{t("创建时间", "Created")}</option>
              </select>
              <select
                aria-label={t("排序方向", "Sort order")}
                value={params.get("order") || "desc"}
                onChange={(event) => parameter("order", event.target.value)}
              >
                <option value="desc">{t("降序", "Descending")}</option>
                <option value="asc">{t("升序", "Ascending")}</option>
              </select>
            </div>
            <div className="wf-table-wrap">
              <table className="wf-table">
                <tbody>
                  {data.admin_repositories?.map((repo) => (
                    <tr key={repo.id}>
                      <td>{repo.full_name}</td>
                      <td>{repo.stargazers_count} ★</td>
                      <td>
                        <button
                          className="button"
                          disabled={mutation.isPending}
                          onClick={() =>
                            void action(
                              `/admin/repositories/${repo.id}/${repo.disabled_by_admin ? "enable" : "disable"}`,
                              repo.disabled_by_admin
                                ? t(
                                    "恢复仓库展示？",
                                    "Enable repository display?",
                                  )
                                : t(
                                    "停用仓库展示？",
                                    "Disable repository display?",
                                  ),
                              {},
                              true,
                            )
                          }
                        >
                          {repo.disabled_by_admin
                            ? t("恢复", "Enable")
                            : t("停用", "Disable")}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <CommunityPagination
              page={data.admin_repository_page}
              onPage={(page) => {
                const next = new URLSearchParams(params);
                next.set("page", String(page));
                setParams(next);
              }}
            />
          </Section>
        </>
      )}
    </div>
  );
}
function CommunityPagination({
  page,
  onPage,
}: {
  page: Page | null;
  onPage: (page: number) => void;
}) {
  const { t } = useLocale();
  if (!page) return null;
  return (
    <div className="wf-pagination">
      <span>
        {page.total} {t("条结果", "results")}
      </span>
      <div>
        <button
          className="button"
          disabled={page.page <= 1}
          onClick={() => onPage(page.page - 1)}
        >
          {t("上一页", "Previous")}
        </button>
        <span>
          {page.page} / {Math.max(1, page.total_pages)}
        </span>
        <button
          className="button"
          disabled={page.page >= page.total_pages}
          onClick={() => onPage(page.page + 1)}
        >
          {t("下一页", "Next")}
        </button>
      </div>
    </div>
  );
}
