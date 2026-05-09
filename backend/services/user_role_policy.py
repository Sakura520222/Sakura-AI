"""用户角色权限策略。"""

ADMIN_ROLES = {"admin", "super_admin"}
VALID_USER_ROLES = {"user", "admin", "super_admin"}


def _is_valid_role(role: str | None) -> bool:
    """判断角色值是否有效。"""
    return bool(role) and role in VALID_USER_ROLES


def can_update_user_role(
    actor_role: str | None, target_role: str | None, new_role: str | None
) -> bool:
    """判断操作者是否可将目标用户角色改为指定角色。"""
    if not all(_is_valid_role(role) for role in (actor_role, target_role, new_role)):
        return False

    if actor_role == "super_admin":
        return True

    return target_role not in ADMIN_ROLES and new_role not in ADMIN_ROLES


def can_toggle_user_status(actor_role: str | None, target_role: str | None) -> bool:
    """判断操作者是否可启用或禁用目标用户。"""
    if not all(_is_valid_role(role) for role in (actor_role, target_role)):
        return False

    return actor_role == "super_admin" or target_role not in ADMIN_ROLES
