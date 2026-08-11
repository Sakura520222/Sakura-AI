from pathlib import Path


def test_version_manager_contains_reconnectable_job_flow():
    template = Path("backend/webui/templates/version_manager.html").read_text(encoding="utf-8")
    for marker in (
        "/version/readiness",
        "/version/preflight",
        "/version/update",
        "/version/jobs/",
        "sessionStorage",
        "reconnecting",
        "health",
        "target_version",
    ):
        assert marker in template
    assert "var/run/docker.sock" not in template


def test_failed_preflight_keeps_update_button_disabled_and_uses_vm_i18n():
    template = Path("backend/webui/templates/version_manager.html").read_text(encoding="utf-8")
    # The preflight result must put update_ready inside the envelope data that
    # showReadiness() actually reads; an outer sibling is silently ignored.
    assert "checks: result.checks, update_ready: result.can_update" in template
    assert "updateButton.disabled = payload.update_ready === false" in template
    for key in (
        "readinessError",
        "preflightPassed",
        "preflightFailed",
        "preflightError",
        "updateStatus",
        "updateSuccess",
        "updateFailed",
        "updateSubmitFailed",
        "target",
        "noTarget",
    ):
        assert f"VM_I18N.{key}" in template
    assert "function vmUpdaterError(error)" in template
    assert "release_unavailable:" in template
