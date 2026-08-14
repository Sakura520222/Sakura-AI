"""Version updater proxy route unit checks (dependency-light helpers)."""

import json

from backend.services.updater_client import (
    UpdaterActionError,
    UpdaterProtocolError,
    UpdaterUnavailableError,
)
from backend.webui.routes.version import _updater_error, _validate_target


def test_version_target_uses_strict_semver():
    assert _validate_target("3.1.0") is True
    assert _validate_target("v3.1.0") is False
    assert _validate_target("3.1") is False
    assert _validate_target("01.2.3") is False


def test_version_proxy_error_mapping():
    assert _updater_error(UpdaterUnavailableError("down")).status_code == 503
    assert _updater_error(UpdaterProtocolError("bad")).status_code == 502
    response = _updater_error(UpdaterActionError(409, {"error": "update_in_progress", "job_id": "upd_1"}))
    assert response.status_code == 409


def test_version_proxy_preserves_safe_release_failure_reason():
    response = _updater_error(
        UpdaterActionError(
            502,
            {
                "error": "release_unavailable",
                "detail": "file_not_found_base_library.zip",
            },
        )
    )

    assert response.status_code == 502
    assert json.loads(response.body) == {
        "error": "release_unavailable",
        "detail": "file_not_found_base_library.zip",
    }


def test_version_proxy_drops_untrusted_internal_error_details():
    response = _updater_error(
        UpdaterActionError(
            500,
            {"error": "internal_error", "detail": "https://signed.example/?token=secret"},
        )
    )

    assert response.status_code == 502
    assert json.loads(response.body) == {"error": "updater_internal_error"}


def test_version_proxy_preserves_registry_unavailable_status():
    response = _updater_error(
        UpdaterActionError(503, {"error": "registry_unavailable"})
    )

    assert response.status_code == 503
    assert json.loads(response.body) == {"error": "registry_unavailable"}
