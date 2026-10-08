"""Dependency bootstrap resilience without weakening runner admission."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services.agent_team.execution import (
    ExecutionError,
    ExecutionProfile,
    ExecutionRequest,
    ExecutionResult,
    LocalExecutionRunner,
)
from backend.services.agent_team.git_workspace_service import (
    AgentTeamGitWorkspaceService,
)
from backend.services.agent_team.network_policy import AgentTeamNetworkPolicy
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


@pytest.fixture
def dependency_workspace(monkeypatch, tmp_path):
    workspace_service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = workspace_service.ensure_workspace("owner", "repo")
    (workspace / "requirements.txt").write_text("example-package\n", encoding="utf-8")
    service = AgentTeamGitWorkspaceService(workspace_service=workspace_service)
    settings = SimpleNamespace(
        agent_team_auto_install_deps=True,
        agent_team_dependency_install_attempts=3,
        agent_team_dependency_retry_delay_seconds=0,
    )

    async def config(key, *, fresh=False):
        return True if key == "agent_team_auto_install_deps" else None

    async def policy():
        return AgentTeamNetworkPolicy.FULL_ACCESS

    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_dynamic_config", config
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_agent_team_network_policy",
        policy,
    )
    return service, workspace, settings


def dependency_runner(service, workspace, backend, results, monkeypatch):
    requests: list[ExecutionRequest] = []
    responses = iter(results)

    async def execute(request):
        requests.append(request)
        assert request.profile is ExecutionProfile.DEPENDENCY
        command = request.command or " ".join(request.argv or ())
        if "-m venv" in command:
            venv = workspace / ".venv" / backend
            scripts = venv / (
                "Scripts" if backend == "local" and os.name == "nt" else "bin"
            )
            scripts.mkdir(parents=True, exist_ok=True)
            for name in ("python", "pip"):
                suffix = ".exe" if backend == "local" and os.name == "nt" else ""
                (scripts / (name + suffix)).write_text("launcher", encoding="utf-8")
            (venv / "pyvenv.cfg").write_text("home = fixture\n", encoding="utf-8")
            return ExecutionResult(exit_code=0)
        assert "--quiet" not in command
        result = next(responses)
        return result(request) if callable(result) else result

    if backend == "local":
        runner = LocalExecutionRunner(workspace, service.workspace_service)
        monkeypatch.setattr(runner, "execute", execute)
    else:
        runner = SimpleNamespace(
            egress_capability="egress",
            supports_profile=lambda profile: profile is ExecutionProfile.DEPENDENCY,
            execute=execute,
        )
    return runner, requests


@pytest.mark.parametrize("backend", ["sandbox", "local"])
@pytest.mark.parametrize("editable", [False, True])
@pytest.mark.asyncio
async def test_transient_failure_retries_and_keeps_both_streams(
    dependency_workspace, monkeypatch, backend, editable
):
    service, workspace, _settings = dependency_workspace
    if editable:
        (workspace / "pyproject.toml").write_text(
            '[project]\nname = "example"\n', encoding="utf-8"
        )
    runner, requests = dependency_runner(
        service,
        workspace,
        backend,
        [
            ExecutionResult(
                exit_code=1,
                stdout="WARNING: Temporary failure in name resolution",
                stderr="ERROR: No matching distribution found for example-package",
            ),
            ExecutionResult(
                exit_code=0, stdout="Successfully installed example-package"
            ),
        ],
        monkeypatch,
    )

    report = await service.install_workspace_dependencies(workspace, runner)

    assert report.status == "succeeded"
    assert len(report.attempts) == 2
    assert report.attempts[0].category == "DNS_FAILURE"
    assert "Temporary failure" in report.attempts[0].stdout
    assert "No matching distribution" in report.attempts[0].stderr
    assert len(requests) == 3  # bootstrap once, pip twice in the same venv
    assert requests[1].command == requests[2].command
    assert requests[1].argv == requests[2].argv
    assert service.dependency_setup_report is report


@pytest.mark.parametrize(
    ("stderr", "category"),
    [
        (
            "Could not find a version that satisfies example==2 (from versions: 1.0, 1.1)\n"
            "No matching distribution found for example==2",
            "PACKAGE_NOT_FOUND",
        ),
        (
            "ResolutionImpossible: conflicting dependencies",
            "DEPENDENCY_RESOLUTION_ERROR",
        ),
        ("Could not build wheels for example-package", "UNKNOWN_DEPENDENCY_FAILURE"),
        ("SSLError: [SSL: WRONG_VERSION_NUMBER] wrong version number", "TLS_FAILURE"),
    ],
)
@pytest.mark.asyncio
async def test_permanent_failure_is_reported_without_retry(
    dependency_workspace, monkeypatch, stderr, category
):
    service, workspace, _settings = dependency_workspace
    runner, requests = dependency_runner(
        service,
        workspace,
        "sandbox",
        [ExecutionResult(exit_code=1, stderr=stderr)],
        monkeypatch,
    )

    report = await service.install_workspace_dependencies(workspace, runner)

    assert report.status == "failed"
    assert len(report.attempts) == 1
    assert report.attempts[0].category == category
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_exhausted_retries_preserve_sanitized_diagnostics(
    dependency_workspace, monkeypatch
):
    service, workspace, _settings = dependency_workspace
    failure = ExecutionResult(
        exit_code=1,
        stdout="GET https://name:password@registry.example/simple/?token=query-secret HTTP 503",
        stderr="Authorization: Bearer header-secret\nAPI_KEY=env-secret\nConnection reset by peer",
        output_truncated=True,
    )
    runner, requests = dependency_runner(
        service, workspace, "sandbox", [failure] * 3, monkeypatch
    )

    report = await service.install_workspace_dependencies(workspace, runner)

    assert report.status == "failed"
    assert len(report.attempts) == 3
    assert len(requests) == 4
    assert report.attempts[0].output_truncated is True
    serialized = json.dumps(report.to_dict())
    for secret in ("password", "query-secret", "header-secret", "env-secret"):
        assert secret not in serialized
    assert "registry.example" in serialized
    assert "Connection reset by peer" in serialized
    assert "dependency_setup=failed" in report.agent_context()


@pytest.mark.asyncio
async def test_retry_backoff_is_interruptible(dependency_workspace, monkeypatch):
    service, workspace, settings = dependency_workspace
    settings.agent_team_dependency_retry_delay_seconds = 30
    cancel_event = asyncio.Event()
    install_started = asyncio.Event()

    def fail(_request):
        install_started.set()
        return ExecutionResult(exit_code=1, stderr="Connection reset by peer")

    runner, requests = dependency_runner(
        service, workspace, "sandbox", [fail], monkeypatch
    )
    install = asyncio.create_task(
        service.install_workspace_dependencies(
            workspace, runner, cancel_event=cancel_event
        )
    )
    try:
        await asyncio.wait_for(install_started.wait(), timeout=1)
        cancel_event.set()
        report = await asyncio.wait_for(install, timeout=1)
    finally:
        if not install.done():
            install.cancel()
        await asyncio.gather(install, return_exceptions=True)

    assert report.status == "cancelled"
    assert len(requests) == 2


@pytest.mark.parametrize("backend", ["sandbox", "local"])
@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        ("", ""),
        ("ReadTimeoutError: registry.example timed out", ""),
        ("", "npm ERR! ECONNRESET"),
        ("", "Could not fetch URL https://registry.example: connection error"),
        ("SSLError: unexpected EOF during TLS handshake", ""),
    ],
)
@pytest.mark.asyncio
async def test_execution_timeout_does_not_claim_network_failure(
    dependency_workspace, monkeypatch, backend, stdout, stderr
):
    service, workspace, _settings = dependency_workspace
    runner, requests = dependency_runner(
        service,
        workspace,
        backend,
        [
            ExecutionResult(exit_code=-9, timed_out=True, stdout=stdout, stderr=stderr),
            ExecutionResult(exit_code=0),
        ],
        monkeypatch,
    )

    report = await service.install_workspace_dependencies(workspace, runner)

    assert report.status == "failed"
    assert len(report.attempts) == 1
    assert report.attempts[0].timed_out is True
    assert report.attempts[0].category == "UNKNOWN_DEPENDENCY_FAILURE"
    assert report.attempts[0].retryable is False
    assert report.attempts[0].stdout == stdout
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_dependency_infrastructure_failure_still_fails_closed(
    dependency_workspace, monkeypatch
):
    service, workspace, _settings = dependency_workspace
    runner, _requests = dependency_runner(
        service,
        workspace,
        "sandbox",
        [ExecutionResult(exit_code=1, infrastructure_error="cleanup failed")],
        monkeypatch,
    )

    with pytest.raises(ExecutionError, match="cleanup failed"):
        await service.install_workspace_dependencies(workspace, runner)


@pytest.mark.parametrize(
    ("diagnostic", "category", "retryable"),
    [
        ("Temporary failure in name resolution", "DNS_FAILURE", True),
        ("getaddrinfo failed", "DNS_FAILURE", True),
        ("npm ERR! code EAI_AGAIN", "DNS_FAILURE", True),
        ("ConnectTimeoutError: connection timed out", "CONNECT_TIMEOUT", True),
        ("ReadTimeoutError: HTTPSConnectionPool", "CONNECT_TIMEOUT", True),
        ("Connection reset by peer", "CONNECTION_RESET", True),
        ("npm ERR! code ECONNRESET", "CONNECTION_RESET", True),
        ("Network is unreachable", "NETWORK_UNREACHABLE", True),
        ("SSLError: EOF occurred in violation of protocol", "TLS_FAILURE", True),
        ("SSLError: TLS handshake failed", "TLS_FAILURE", False),
        (
            "SSLError: [SSL: WRONG_VERSION_NUMBER] wrong version number",
            "TLS_FAILURE",
            False,
        ),
        ("SSLError: TLSV1_ALERT_PROTOCOL_VERSION", "TLS_FAILURE", False),
        (
            "503 Server Error: Service Unavailable for url: https://registry.example",
            "HTTP_5XX",
            True,
        ),
        ("npm ERR! code E503", "HTTP_5XX", True),
        (
            "Could not fetch URL https://registry.example: connection error",
            "INDEX_UNAVAILABLE",
            True,
        ),
        ("ResolutionImpossible", "DEPENDENCY_RESOLUTION_ERROR", False),
        (
            "WARNING: Connection reset by peer\nResolutionImpossible",
            "DEPENDENCY_RESOLUTION_ERROR",
            False,
        ),
        ("certificate verify failed: self-signed certificate", "TLS_FAILURE", False),
        ("No matching distribution found", "UNKNOWN_DEPENDENCY_FAILURE", False),
        (
            "Could not find a version that satisfies example==2 (from versions: none)\n"
            "No matching distribution found for example==2",
            "UNKNOWN_DEPENDENCY_FAILURE",
            False,
        ),
        (
            "Could not find a version that satisfies example==2 (from versions: 1.0, 1.1)\n"
            "No matching distribution found for example==2",
            "PACKAGE_NOT_FOUND",
            False,
        ),
        ("401 Client Error: Unauthorized", "UNKNOWN_DEPENDENCY_FAILURE", False),
        ("compiler failed", "UNKNOWN_DEPENDENCY_FAILURE", False),
    ],
)
def test_dependency_failure_classification(diagnostic, category, retryable):
    from backend.services.agent_team.dependency_bootstrap import (
        classify_dependency_failure,
    )

    failure = classify_dependency_failure(
        ExecutionResult(exit_code=1, stderr=diagnostic)
    )
    assert failure.category == category
    assert failure.retryable is retryable


@pytest.mark.asyncio
async def test_failed_setup_reaches_agent_and_is_saved_before_execution(
    dependency_workspace, monkeypatch
):
    from backend.services.agent_team.fullstack_expert import FullStackResult
    from backend.services.agent_team.iteration_loop import IterationLoopService

    service, workspace, _settings = dependency_workspace
    runner, _requests = dependency_runner(
        service,
        workspace,
        "sandbox",
        [
            ExecutionResult(
                exit_code=1, stderr="No matching distribution found for example"
            )
        ],
        monkeypatch,
    )
    report = await service.install_workspace_dependencies(workspace, runner)
    saved = []
    received = []

    async def save_result(session_id, payload):
        assert session_id == 507
        saved.append(payload)

    async def finish_session(session_id, outcome, payload):
        assert outcome == "success"
        await save_result(session_id, payload)

    async def execute(**kwargs):
        assert saved[0]["dependency_setup"]["status"] == "failed"
        received.append(kwargs["reference_context"])
        return FullStackResult(
            success=True, summary="static repair", modified_files=["main.py"]
        )

    loop = IterationLoopService(
        workspace,
        service.workspace_service,
        checkpoint=SimpleNamespace(
            save_session_result=save_result, finish_session=finish_session
        ),
    )
    monkeypatch.setattr(
        loop,
        "_create_agent",
        AsyncMock(return_value=SimpleNamespace(session_id=507, execute=execute)),
    )
    monkeypatch.setattr(loop, "_complete_session", AsyncMock())
    monkeypatch.setattr(
        loop.conversation_context,
        "build_agent_context",
        AsyncMock(return_value="history"),
    )
    monkeypatch.setattr(loop.conversation_context, "record_agent_turn", AsyncMock())

    outcome = await loop.run(
        task_title="Fix issue",
        task_summary="repair the code",
        reference_context="original source",
        dependency_setup=report,
    )

    assert outcome.success is True
    assert "original source" in received[0]
    assert "dependency_setup=failed" in received[0]
    assert "No matching distribution" in received[0]
    assert (
        saved[-1]["dependency_setup"]["attempts"][0]["stderr"]
        == "No matching distribution found for example"
    )
    assert saved[-1]["success"] is True


@pytest.mark.asyncio
async def test_retry_rechecks_paths_before_running_package_hooks(
    dependency_workspace, monkeypatch, tmp_path
):
    service, workspace, _settings = dependency_workspace
    external = tmp_path / "outside.txt"
    external.write_text("secret", encoding="utf-8")

    def replace_manifest(_request):
        manifest = workspace / "requirements.txt"
        manifest.unlink()
        try:
            manifest.symlink_to(external)
        except OSError as exc:
            pytest.skip(f"symlink unavailable: {exc}")
        return ExecutionResult(exit_code=1, stderr="Connection reset by peer")

    runner, requests = dependency_runner(
        service, workspace, "sandbox", [replace_manifest], monkeypatch
    )
    with pytest.raises(ExecutionError, match="路径不在工作区内"):
        await service.install_workspace_dependencies(workspace, runner)
    assert len(requests) == 2


@pytest.mark.parametrize("attempts", [2, "bad", 0, float("inf")])
@pytest.mark.asyncio
async def test_retry_policy_uses_dynamic_config_and_settings_defaults(
    dependency_workspace, monkeypatch, attempts
):
    service, workspace, _settings = dependency_workspace

    async def config(key, *, fresh=False):
        if key == "agent_team_auto_install_deps":
            return True
        if key == "agent_team_dependency_install_attempts":
            return attempts
        if key == "agent_team_dependency_retry_delay_seconds":
            return 0
        return None

    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_dynamic_config", config
    )
    failure = ExecutionResult(exit_code=1, stderr="Connection reset by peer")
    runner, _requests = dependency_runner(
        service, workspace, "sandbox", [failure] * 3, monkeypatch
    )
    report = await service.install_workspace_dependencies(workspace, runner)
    assert len(report.attempts) == (2 if attempts == 2 else 3)


@pytest.mark.parametrize("backend", ["sandbox", "local"])
@pytest.mark.asyncio
async def test_retry_policy_reads_saved_values_at_each_admission(
    dependency_workspace, monkeypatch, backend
):
    from backend.core import config as config_module
    from backend.services.agent_team import git_workspace_service as workspace_module

    service, workspace, _settings = dependency_workspace
    attempts_key = "agent_team_dependency_install_attempts"
    delay_key = "agent_team_dependency_retry_delay_seconds"
    saved_values = {
        "agent_team_auto_install_deps": True,
        attempts_key: 1,
        delay_key: 0,
    }
    # A save by another worker leaves this worker's unexpired cache untouched.
    monkeypatch.setattr(
        config_module,
        "_dynamic_config_cache",
        {
            attempts_key: (5, monotonic() + 60),
            delay_key: (59, monotonic() + 60),
        },
    )

    async def read_saved_value(key, *, fail_closed=False):
        return saved_values.get(key)

    monkeypatch.setattr(config_module, "_read_config_from_db", read_saved_value)
    monkeypatch.setattr(
        workspace_module, "get_dynamic_config", config_module.get_dynamic_config
    )
    sleep = AsyncMock()
    monkeypatch.setattr(workspace_module.asyncio, "sleep", sleep)
    failure = ExecutionResult(exit_code=1, stderr="ECONNRESET")

    for attempts, delay in ((1, 0), (3, 0.25)):
        saved_values.update({attempts_key: attempts, delay_key: delay})
        runner, _requests = dependency_runner(
            service, workspace, backend, [failure] * 5, monkeypatch
        )
        report = await service.install_workspace_dependencies(workspace, runner)
        assert report.status == "failed"
        assert len(report.attempts) == attempts

    assert [call.args[0] for call in sleep.await_args_list] == [0.25, 0.5]


def test_sanitization_covers_headers_json_and_credential_urls():
    from backend.services.agent_team.dependency_bootstrap import (
        sanitize_dependency_diagnostic,
    )

    raw = (
        "Cookie: session=cookie-secret; xsrf=xsrf-secret\n"
        "Proxy-Authorization: Basic proxy-secret\n"
        '{"apiKey": "json-secret", "access_token": "token-secret"}\n'
        "https://name:pass-secret@registry.example/private-secret?api_key=query-secret#fragment-secret\n"
        "npm_config_//registry.example/:_authToken=npm-secret\n"
        "pypi-package-secret github_pat_github-secret\n"
        "ConnectTimeoutError: registry.example"
    )
    safe = sanitize_dependency_diagnostic(raw)
    for secret in (
        "cookie-secret",
        "xsrf-secret",
        "proxy-secret",
        "json-secret",
        "token-secret",
        "pass-secret",
        "private-secret",
        "query-secret",
        "fragment-secret",
        "npm-secret",
        "pypi-package-secret",
        "github_pat_github-secret",
    ):
        assert secret not in safe
    assert "registry.example" in safe
    assert "ConnectTimeoutError" in safe


@pytest.mark.parametrize("word", ["token", "token-"])
def test_large_diagnostics_do_not_cause_redaction_backtracking(word):
    # Run in a disposable child: the old quadratic regex must fail within a
    # bounded time instead of stalling pytest or the application's event loop.
    script = (
        "from backend.services.agent_team.dependency_bootstrap import sanitize_dependency_diagnostic\n"
        f"diagnostic = {word!r} * 20000\n"
        "assert sanitize_dependency_diagnostic(diagnostic) == diagnostic\n"
    )
    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )


@pytest.mark.parametrize(
    "warning", ["failed to download registry index ", "(from versions: "]
)
def test_repeated_incomplete_diagnostics_do_not_stall_failure_classification(warning):
    script = (
        "from backend.services.agent_team.dependency_bootstrap import classify_dependency_failure\n"
        "from backend.services.agent_team.execution import ExecutionResult\n"
        f"diagnostic = {warning!r} * 16000\n"
        "result = classify_dependency_failure(ExecutionResult(exit_code=1, stderr=diagnostic))\n"
        "assert result.category == 'UNKNOWN_DEPENDENCY_FAILURE'\n"
    )
    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )


@pytest.mark.asyncio
async def test_worker_enters_agent_execution_after_permanent_setup_failure(
    dependency_workspace, monkeypatch
):
    from backend.models.agent_team_models import AgentTeamTaskStatus
    from backend.services.agent_team.iteration_loop import IterationOutcome
    from backend.workers import agent_team_worker as worker_module

    service, workspace, _settings = dependency_workspace
    runner, _requests = dependency_runner(
        service,
        workspace,
        "sandbox",
        [ExecutionResult(exit_code=1, stderr="ResolutionImpossible")],
        monkeypatch,
    )
    task_id = 627
    task = SimpleNamespace(
        id=task_id,
        source_type="manual_issue",
        source_id=627,
        source_issue_number=627,
        repo_owner="owner",
        repo_name="repo",
        title="repair dependencies",
        summary="fix the issue",
        base_branch="develop",
        resume_count=0,
    )
    updates = []
    received = []
    worker = worker_module.AgentTeamWorker()

    async def update(_task_id, **kwargs):
        updates.append(kwargs)

    class Loop:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, **kwargs):
            report = kwargs.get("dependency_setup")
            assert report is not None and report.status == "failed"
            received.append(report)
            # Stop at the EDITING cancellation checkpoint without publishing
            # to GitHub or using persistent test services.
            worker_module.request_task_cancel(task_id)
            return IterationOutcome(success=False, reason="cancelled", iterations=0)

    info = SimpleNamespace(
        workspace=workspace,
        branch_name="feature/task",
        default_branch="develop",
        commit_sha="abc",
    )
    monkeypatch.setattr(service, "prepare_workspace", AsyncMock(return_value=info))
    monkeypatch.setattr(worker, "_load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(worker, "_update_task", update)
    monkeypatch.setattr(
        worker, "_create_agent_execution_runner", AsyncMock(return_value=runner)
    )
    monkeypatch.setattr(
        worker, "_load_task_reference_context", AsyncMock(return_value="source")
    )
    monkeypatch.setattr(worker, "_expire_pending_prompts_if_terminal", AsyncMock())
    monkeypatch.setattr(worker_module, "AgentTeamGitWorkspaceService", lambda: service)
    monkeypatch.setattr(worker_module, "IterationLoopService", Loop)
    monkeypatch.setattr(
        worker_module, "load_skills_context", AsyncMock(return_value=("", {}, {}))
    )
    monkeypatch.setattr(
        worker_module,
        "load_sakura_memory",
        AsyncMock(return_value={"text": "", "github_repo": None, "sakura_ref": None}),
    )

    assert await worker.process_task(task_id) == task_id
    assert len(received) == 1
    assert any(
        item.get("status") == AgentTeamTaskStatus.EDITING.value for item in updates
    )
    assert not any(
        item.get("status") == AgentTeamTaskStatus.FAILED.value for item in updates
    )


@pytest.fixture(autouse=True)
def worker_control_audit_store(monkeypatch):
    """Worker orchestration uses fake tasks; persistence has separate SQLite tests."""
    from unittest.mock import AsyncMock

    from backend.services.agent_team.conversation_checkpoint import (
        ConversationCheckpointService,
    )

    monkeypatch.setattr(
        ConversationCheckpointService, "record_control_event", AsyncMock()
    )
