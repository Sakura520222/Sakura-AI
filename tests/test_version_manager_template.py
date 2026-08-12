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


def test_version_manager_has_accessible_monotonic_progress_modal_and_refresh_gate():
    template = Path("backend/webui/templates/version_manager.html").read_text(encoding="utf-8")
    for marker in (
        'role="dialog"',
        'aria-modal="true"',
        'aria-labelledby="update-progress-title"',
        'role="progressbar"',
        'aria-valuemin="0"',
        'aria-valuemax="100"',
        'aria-valuenow="0"',
        'id="update-progress-percent"',
        'id="update-progress-fill"',
        "const UPDATE_PROGRESS",
        "materialize_current_anchor: 42",
        "setUpdateProgress(next)",
        "Math.max(lastProgress",
        "lastProgress = 0",
        "window.location.reload()",
        "openProgressModal({initial: true})",
        "const persistedJobId = sessionStorage.getItem(JOB_STORAGE_KEY)",
        "if (persistedJobId) {",
        "pollJob(persistedJobId)",
    ):
        assert marker in template

    for state in (
        "checking",
        "preflight",
        "materialize_current_anchor",
        "downloading",
        "activating",
        "restarting",
        "health_checking",
        "success",
        "complete",
    ):
        assert f"{state}:" in template or f"{state}]" in template

    health_check = template.index("await verifyHealth(target)")
    clear_job = template.index("sessionStorage.removeItem(JOB_STORAGE_KEY)", health_check)
    reload_page = template.index("scheduleRefresh();", health_check)
    assert health_check < clear_job < reload_page


def test_version_manager_failure_path_is_closeable_without_refresh():
    template = Path("backend/webui/templates/version_manager.html").read_text(encoding="utf-8")
    assert 'id="update-progress-close"' in template
    assert "function closeProgressModal()" in template
    assert "if (!progressTerminal) return;" in template
    assert "markProgressError(" in template
    assert "sessionStorage.removeItem(JOB_STORAGE_KEY)" in template
    assert "const canClose = kind === 'error' && progressTerminal" in template
    assert "readiness.update_ready === false || readiness.update_available === false" in template
    assert "updateProgressModal.addEventListener('keydown'" in template
    assert "scheduleRefresh();" in template
    assert template.index("markProgressError(") < template.index("scheduleRefresh();")
