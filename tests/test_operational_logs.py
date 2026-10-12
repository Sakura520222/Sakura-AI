import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from fastapi import HTTPException

from backend.core import logging_bridge
from backend.core.time_service import SystemClock
from backend.services import operational_log_service as log_service
from backend.services.activity_observability.access_service import ActivityNotFoundError
from backend.webui.routes.activity_observability import activity_operational_logs


def test_session_log_lookup_matches_exact_ids_and_never_interprets_message_text(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(logging_bridge, "APP_LOG_DIRECTORY", tmp_path)
    path = tmp_path / "app_test.log"
    entries = [
        {
            "record": {"extra": {"session_id": 12}},
            "text": "other session with session_id=1\n",
        },
        {
            "record": {"extra": {"session_id": 1}},
            "text": "RuntimeError at worker.py:123 api_key=opaque-secret\n",
        },
        {"record": {"extra": {"session_id": "agent:1"}}, "text": "different domain\n"},
    ]
    path.write_text(
        "legacy text\n"
        + "\n".join(json.dumps(entry) for entry in entries)
        + '\n{"truncated":',
        encoding="utf-8",
    )
    result = log_service.session_logs(1)
    assert "RuntimeError" in result and "worker.py:123" in result
    assert "opaque-secret" not in result
    assert "other session" not in result and "different domain" not in result


def test_log_query_enforces_byte_and_record_budgets(monkeypatch, tmp_path):
    monkeypatch.setattr(logging_bridge, "APP_LOG_DIRECTORY", tmp_path)
    monkeypatch.setattr(log_service, "LOG_QUERY_MAX_BYTES", 2000)
    monkeypatch.setattr(log_service, "LOG_QUERY_MAX_RECORDS", 2)
    lines = [
        json.dumps({"record": {"extra": {"session_id": 1}}, "text": f"record-{i}\n"})
        for i in range(100)
    ]
    (tmp_path / "app_test.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert log_service.session_logs(1) == "record-98\nrecord-99\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,allowed", [("user", False), ("admin", False), ("super_admin", True)]
)
async def test_activity_logs_require_superadmin_and_session_access(
    monkeypatch, role, allowed
):
    access = SimpleNamespace(require_session_access=AsyncMock())
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(activity_access_service=access))
    )
    monkeypatch.setattr(
        log_service, "session_logs", lambda session_id: f"session-{session_id}"
    )
    user = {"role": role, "user_id": 7}
    if not allowed:
        with pytest.raises(HTTPException) as error:
            await activity_operational_logs(1, request, user, object())
        assert error.value.status_code == 404
        access.require_session_access.assert_not_awaited()
        return
    response = await activity_operational_logs(1, request, user, object())
    assert response.body == b"session-1"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("text/plain")
    access.require_session_access.assert_awaited_once()
    access.require_session_access.side_effect = ActivityNotFoundError()
    with pytest.raises(HTTPException) as error:
        await activity_operational_logs(2, request, user, object())
    assert error.value.status_code == 404


@pytest.mark.parametrize("filename", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_every_compose_service_has_bounded_docker_logging(filename):
    compose = yaml.safe_load((Path("docker") / filename).read_text(encoding="utf-8"))
    for name, service in compose["services"].items():
        assert service["logging"] == {
            "driver": "json-file",
            "options": {"max-size": "50m", "max-file": "3"},
        }, name


def test_sandbox_controller_uses_same_log_budget_as_compose():
    source = Path("start.sh").read_text(encoding="utf-8")
    command = source.split("run_args=(", 1)[1].split(
        "--label ai.sakura.managed-by=sandboxd-daemon", 1
    )[0]
    assert "--log-driver json-file" in command
    assert "--log-opt max-size=50m" in command
    assert "--log-opt max-file=3" in command


def test_app_log_retention_enforces_age_and_total_capacity(monkeypatch, tmp_path):
    monkeypatch.setattr(logging_bridge, "APP_LOG_DIRECTORY", tmp_path)
    monkeypatch.setattr(logging_bridge, "APP_LOG_MAX_BYTES", 25)
    now = SystemClock.now_utc().timestamp()
    for name, age in [("expired", 11 * 86400), ("old", 100), ("recent", 10)]:
        path = tmp_path / f"app_{name}.log"
        path.write_bytes(b"x" * 20)
        os.utime(path, (now - age, now - age))
    unrelated = tmp_path / "other.log"
    unrelated.write_bytes(b"keep")
    logging_bridge._cleanup_expired_app_logs()
    assert not (tmp_path / "app_expired.log").exists()
    assert not (tmp_path / "app_old.log").exists()
    assert (tmp_path / "app_recent.log").exists()
    assert unrelated.read_bytes() == b"keep"
