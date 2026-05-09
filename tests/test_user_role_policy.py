from backend.services.user_role_policy import can_update_user_role


def test_admin_cannot_promote_user_to_admin():
    assert not can_update_user_role("admin", "user", "admin")


def test_admin_cannot_promote_user_to_super_admin():
    assert not can_update_user_role("admin", "user", "super_admin")


def test_admin_can_demote_regular_user_to_user():
    assert can_update_user_role("admin", "user", "user")


def test_admin_cannot_modify_existing_admin_roles():
    assert not can_update_user_role("admin", "admin", "user")
    assert not can_update_user_role("admin", "super_admin", "user")


def test_super_admin_can_assign_admin_roles():
    assert can_update_user_role("super_admin", "user", "admin")
    assert can_update_user_role("super_admin", "admin", "super_admin")
