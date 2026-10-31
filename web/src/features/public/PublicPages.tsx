import { Link, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { useLocale } from "../../app/locale";
import { PublicShell } from "../../components/layout/PublicShell";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  Markdown,
  PageHeader,
} from "../../components/workflow/Workflow";
import { exactCredits, type Plan } from "../billing/billingTypes";
import policies from "./legal.json";
import "../billing/billing.css";
import "./public.css";

export function LegalPage() {
  return (
    <PublicShell>
      <LegalContent />
    </PublicShell>
  );
}
function LegalContent() {
  const { kind = "terms" } = useParams();
  const policy = policies[kind as keyof typeof policies];
  return (
    <main className="public-document">
      {kind === "pricing" ? (
        <Pricing />
      ) : policy ? (
        <Markdown>{policy}</Markdown>
      ) : (
        <EmptyState title="404" />
      )}
    </main>
  );
}
function Pricing() {
  const { t } = useLocale();
  const query = useQuery({
    queryKey: ["public-plans"],
    queryFn: ({ signal }) => api<Plan[]>("/api/v1/public/plans", { signal }),
  });
  const benefits = [
    ["pr_quota_bonus", t("PR 审查次数", "PR reviews")],
    ["pr_monthly_add", t("每月 PR 审查次数", "PR reviews per month")],
    ["issue_quota_bonus", t("Issue 分析次数", "Issue analyses")],
    ["issue_monthly_add", t("每月 Issue 分析次数", "Issue analyses per month")],
    ["agent_quota_bonus", t("Agent 任务次数", "Agent tasks")],
    ["agent_monthly_add", t("每月 Agent 任务次数", "Agent tasks per month")],
    ["pr_daily_add", t("每日 PR 审查次数", "PR reviews per day")],
    ["issue_daily_add", t("每日 Issue 分析次数", "Issue analyses per day")],
  ] as const;
  return (
    <>
      <PageHeader
        title={t("套餐与价格", "Plans & pricing")}
        description={t(
          "选择适合你的使用方案。",
          "Choose a plan for your usage.",
        )}
      />
      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState retry={() => void query.refetch()} />
      ) : query.data.length ? (
        <div className="billing-plans">
          {query.data.map((plan) => (
            <section key={plan.id}>
              <h2>{plan.name}</h2>
              <p>{plan.description}</p>
              <strong>
                {plan.currency} {plan.formatted_price ?? "—"}
              </strong>
              <span>{exactCredits(plan.credit_grant)} Credits</span>
              {!!plan.duration_days && (
                <span>
                  {plan.duration_days} {t("天", "days")}
                </span>
              )}
              <ul>
                {benefits.map(
                  ([key, label]) =>
                    Number(plan[key as keyof Plan]) > 0 && (
                      <li key={key}>
                        {String(plan[key as keyof Plan])} {label}
                      </li>
                    ),
                )}
              </ul>
              <a className="button primary" href="/app/billing/plans">
                {t("选择套餐", "Choose plan")}
              </a>
            </section>
          ))}
        </div>
      ) : (
        <EmptyState title={t("暂无可购买套餐", "No plans available")} />
      )}
      <p>
        <Link to="/legal/refund">{t("退款政策", "Refund policy")}</Link> ·{" "}
        <Link to="/legal/terms">{t("服务条款", "Terms of service")}</Link>
      </p>
    </>
  );
}
export function ErrorPage() {
  return (
    <PublicShell>
      <ErrorContent />
    </PublicShell>
  );
}
function ErrorContent() {
  const { t } = useLocale();
  const [params] = useSearchParams();
  const status = params.get("status");
  return (
    <main className="public-auth-card">
      <h1>
        {status === "403"
          ? t("无法访问此页面", "Access denied")
          : status === "404"
            ? t("页面不存在", "Page not found")
            : t("页面暂时无法打开", "Unable to open this page")}
      </h1>
      <p>{t("请返回首页，或稍后重试。", "Return home or try again later.")}</p>
      <a className="button primary" href="/app/">
        {t("返回首页", "Return home")}
      </a>
    </main>
  );
}
