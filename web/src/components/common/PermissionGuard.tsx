import type { ReactNode } from "react";
import { useAuth, type Bootstrap } from "../../app/auth";
import { useLocale } from "../../app/locale";
import { EmptyState } from "./states";

export function PermissionGuard({
  permission,
  children,
}: {
  permission: keyof Bootstrap["permissions"];
  children: ReactNode;
}) {
  const { permissions } = useAuth();
  const { t } = useLocale();
  return permissions[permission] ? (
    children
  ) : (
    <EmptyState
      title={t(
        "当前账号无权访问此页面",
        "Your account cannot access this page",
      )}
    />
  );
}
