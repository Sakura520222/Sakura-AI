from __future__ import annotations

import pytest
from pydantic import ValidationError
from sakura_ai_sandboxer.models import ExecutionRequest


def _request(**overrides):
    values = {
        "request_id": "request-1",
        "workspace_key": "task-1",
        "command": "printf ok",
        "profile": "agent",
        "timeout_seconds": 1,
    }
    values.update(overrides)
    return values


def test_request_accepts_json_profile_and_rejects_unknown_runtime_controls():
    request = ExecutionRequest.model_validate(_request())
    assert request.profile.value == "agent"
    for field in ("image", "mount", "network", "runtime", "docker_argv"):
        with pytest.raises(ValidationError):
            ExecutionRequest.model_validate(_request(**{field: "attacker-controlled"}))


def test_request_requires_one_command_form_and_empty_environment():
    with pytest.raises(ValidationError):
        ExecutionRequest.model_validate(_request(command=None, argv=None))
    with pytest.raises(ValidationError):
        ExecutionRequest.model_validate(_request(command="echo", argv=["echo"]))
    with pytest.raises(ValidationError):
        ExecutionRequest.model_validate(_request(env={"SECRET": "no"}))


@pytest.mark.parametrize(
    "cwd",
    ["/etc", "../escape", "safe/../escape", r"safe\\path", "safe//path"],
)
def test_request_cwd_stays_relative_posix(cwd):
    with pytest.raises(ValidationError):
        ExecutionRequest.model_validate(_request(cwd=cwd))


def test_request_rejects_unknown_fields_even_when_the_value_is_null():
    with pytest.raises(ValidationError):
        ExecutionRequest.model_validate(_request(network=None))
