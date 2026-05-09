"""用户角色变更权限策略。"""

ADMIN_ROLES = {"admin", "super_admin"}
VALID_USER_ROLES = {"user", "admin", "super_admin"}


def can_update_user_role(actor_role: str, target_role: str, new_role: str) -> bool:
    """判断操作者是否可将目标用户角色改为指定角色。"""
    if actor_role == "super_admin":
        return True

    return target_role not in ADMIN_ROLES and new_role not in ADMIN_ROLES
