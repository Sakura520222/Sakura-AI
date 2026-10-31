import { NavLink, Route, Routes } from "react-router-dom";
import { useAuth } from "../../app/auth";
import { useLocale } from "../../app/locale";
import { EmptyState, PageHeader } from "../../components/workflow/Workflow";
import UsersPage from "./UsersPage";
import AuditPage from "./AuditPage";
import PlansAdmin from "./PlansAdmin";
import CodesAdmin from "./CodesAdmin";
import RefundsAdmin from "./RefundsAdmin";
import PricingAdmin from "./PricingAdmin";
import { lazy, Suspense } from "react";
import { LoadingState } from "../../components/common/states";
const VersionPanel = lazy(() => import("../version/VersionPanel"));
const AdminSecurityPage = lazy(() => import("../security/AdminSecurityPage"));
const StoragePage = lazy(() => import("../storage/StoragePage"));
const AnnouncementsPage = lazy(
  () => import("../announcements/AnnouncementsPage"),
);
import "../billing/billing.css";

export default function AdminPage() {
  const { t } = useLocale();
  const { permissions } = useAuth();
  if (!permissions.admin)
    return (
      <EmptyState
        title={t("需要管理员权限", "Administrator access required")}
      />
    );
  const superOnly = (page: React.ReactNode) =>
    permissions.super_admin ? (
      page
    ) : (
      <EmptyState
        title={t("需要超级管理员权限", "Super administrator access required")}
      />
    );
  return (
    <>
      <PageHeader
        title={t("管理中心", "Administration")}
        description={t(
          "管理用户、计费规则和操作记录。",
          "Manage users, billing configuration and audit history.",
        )}
      />
      <nav
        className="billing-tabs"
        aria-label={t("管理导航", "Administration navigation")}
      >
        <NavLink to="/admin/users">{t("用户", "Users")}</NavLink>
        <NavLink to="/admin/logs">{t("审计日志", "Audit logs")}</NavLink>
        {permissions.super_admin && (
          <>
            <NavLink to="/admin/security">{t("安全", "Security")}</NavLink>
            <NavLink to="/admin/announcements">
              {t("公告管理", "Announcement management")}
            </NavLink>
            <NavLink to="/admin/storage">
              {t("向量存储", "Vector storage")}
            </NavLink>
            <NavLink to="/admin/pricing">
              {t("模型定价", "Model pricing")}
            </NavLink>
            <NavLink to="/admin/plans">{t("套餐", "Plans")}</NavLink>
            <NavLink to="/admin/codes">{t("兑换码", "Codes")}</NavLink>
            <NavLink to="/admin/refunds">{t("退款审核", "Refunds")}</NavLink>
            <NavLink to="/admin/version">{t("版本更新", "Updates")}</NavLink>
          </>
        )}
      </nav>
      <Routes>
        <Route index element={<UsersPage />} />
        <Route path="users" element={<UsersPage />} />
        <Route path="users/:id" element={<UsersPage />} />
        <Route path="logs" element={<AuditPage />} />
        <Route path="plans" element={superOnly(<PlansAdmin />)} />
        <Route path="codes" element={superOnly(<CodesAdmin />)} />
        <Route path="refunds" element={superOnly(<RefundsAdmin />)} />
        <Route path="pricing" element={superOnly(<PricingAdmin />)} />
        <Route path="security" element={superOnly(<AdminSecurityPage />)} />
        <Route path="security/:id" element={superOnly(<AdminSecurityPage />)} />
        <Route
          path="announcements"
          element={superOnly(<AnnouncementsPage admin />)}
        />
        <Route path="storage" element={superOnly(<StoragePage />)} />
        <Route path="storage/:name" element={superOnly(<StoragePage />)} />
        <Route
          path="version"
          element={superOnly(
            <Suspense fallback={<LoadingState />}>
              <VersionPanel />
            </Suspense>,
          )}
        />
        <Route
          path="*"
          element={<EmptyState title={t("页面不存在", "Page not found")} />}
        />
      </Routes>
    </>
  );
}
