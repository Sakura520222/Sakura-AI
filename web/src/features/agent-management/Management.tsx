import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { useLocale } from "../../app/locale";
import { useAuth } from "../../app/auth";
import { EmptyState } from "../../components/common/states";
import "./management.css";

export function ManagementGuard({ children }: { children: ReactNode }) {
  const { permissions } = useAuth();
  const { t } = useLocale();
  return permissions.super_admin ? (
    children
  ) : (
    <EmptyState
      title={t(
        "此页面需要超级管理员权限",
        "Super administrator access required",
      )}
    />
  );
}

export function ManagementNav() {
  const { t } = useLocale();
  return (
    <nav
      className="management-nav"
      aria-label={t("Agent 管理", "Agent management")}
    >
      <NavLink to="/agent" end>
        {t("任务", "Tasks")}
      </NavLink>
      <NavLink to="/agent/skills">{t("技能", "Skills")}</NavLink>
      <NavLink to="/agent/workspaces">{t("工作区", "Workspaces")}</NavLink>
      <NavLink to="/settings/agent#plugins">
        {t("插件与权限", "Plugins & permissions")}
      </NavLink>
    </nav>
  );
}

export function formatBytes(value: number) {
  const units = ["B", "KiB", "MiB", "GiB"];
  let size = value,
    index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index++;
  }
  return `${index ? size.toFixed(1) : size} ${units[index]}`;
}
