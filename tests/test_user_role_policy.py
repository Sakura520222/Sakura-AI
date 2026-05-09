from backend.services.user_role_policy import can_toggle_user_status, can_update_user_role


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


def test_super_admin_can_demote_admin_roles():
    assert can_update_user_role("super_admin", "admin", "user")
    assert can_update_user_role("super_admin", "super_admin", "user")


def test_invalid_roles_are_denied_for_role_updates():
    assert not can_update_user_role(None, "user", "admin")
    assert not can_update_user_role("admin", None, "user")
    assert not can_update_user_role("admin", "user", None)
    assert not can_update_user_role("", "", "")
    assert not can_update_user_role("admin", "user", "owner")


def test_admin_can_toggle_regular_user_status():
    assert can_toggle_user_status("admin", "user")


def test_admin_cannot_toggle_admin_roles():
    assert not can_toggle_user_status("admin", "admin")
    assert not can_toggle_user_status("admin", "super_admin")


def test_super_admin_can_toggle_admin_roles():
    assert can_toggle_user_status("super_admin", "admin")
    assert can_toggle_user_status("super_admin", "super_admin")


def test_invalid_roles_are_denied_for_status_toggle():
    assert not can_toggle_user_status(None, "user")
    assert not can_toggle_user_status("admin", None)
    assert not can_toggle_user_status("", "")
    assert not can_toggle_user_status("admin", "owner")
