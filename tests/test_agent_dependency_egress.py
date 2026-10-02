"""Execution-scoped dependency egress through real Backend entry points."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from loguru import logger

from backend.services.agent_team import network_policy, sandbox_client
from backend.services.agent_team.execution import (
    ExecutionError,
    ExecutionRequest,
    LocalExecutionRunner,
)
from backend.services.agent_team.git_workspace_service import (
    AgentTeamGitWorkspaceService,
)
from backend.services.agent_team.network_policy import (
    AgentTeamNetworkPolicy,
    AgentTeamNetworkPolicyState,
    network_mode_for_policy,
)
from backend.services.agent_team.sandbox_client import (
    SandboxCleanupError,
    SandboxExecutionRunner,
    SandboxPolicyError,
    SandboxProtocolError,
    SandboxRemoteError,
    SandboxUnavailableError,
)
from backend.services.agent_team.tools.base import ToolContext
from backend.services.agent_team.tools.shell_tool import ShellTool
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


def test_dependency_policy_matrix():
    assert network_mode_for_policy("web_tools") == "none"
    assert network_mode_for_policy("web_tools", profile="dependency") == "egress"
    assert (
        network_mode_for_policy("web_tools", capability="dependency_egress") == "egress"
    )
    assert network_mode_for_policy("offline", profile="dependency") == "none"
    assert network_mode_for_policy("full_access") == "egress"
    assert network_policy.AgentTeamNetworkPolicy.WEB_TOOLS.allows_dependency_network


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("owner", "repo")
    runner = SandboxExecutionRunner(
        workspace,
        service,
        socket_path=str(tmp_path / "sandboxd.sock"),
        deploy_mode="source",
        cleanup_margin_seconds=0.05,
    )
    payloads = []
    state = SimpleNamespace(
        policy=AgentTeamNetworkPolicy.WEB_TOOLS, revision="revision-1"
    )

    async def read_state():
        return AgentTeamNetworkPolicyState(policy=state.policy, revision=state.revision)

    async def transport(method, path, json_body=None, **kwargs):
        assert method == "POST" and path == "/v1/executions"
        payloads.append(json_body)
        return envelope(json_body["request_id"])

    monkeypatch.setattr(
        sandbox_client, "get_agent_team_network_policy_state", read_state
    )
    monkeypatch.setattr(runner, "_request", transport)
    ctx = ToolContext(
        workspace=str(workspace), workspace_service=service, execution_runner=runner
    )
    return runner, ctx, payloads, state


def envelope(request_id, **changes):
    return {
        "protocol_version": 2,
        "sandboxd_version": "test",
        "data": {
            "request_id": request_id,
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
            "timed_out": False,
            "cancelled": False,
            "output_truncated": False,
            **changes,
        },
    }


@pytest.mark.parametrize("command", ["npm install", "pytest -q", "printf fixture"])
@pytest.mark.asyncio
async def test_shell_temporary_egress_then_ordinary_shell_is_offline(sandbox, command):
    _runner, ctx, payloads, _state = sandbox
    tool = ShellTool()
    online = await tool.execute(
        {"command": command, "network_capability": "dependency_egress"}, ctx
    )
    offline = await tool.execute({"command": "npm test"}, ctx)
    assert online.success and offline.success
    assert [p["network_mode"] for p in payloads] == ["egress", "none"]
    assert all(p["profile"] == "agent" for p in payloads)
    assert all(
        set(p)
        == {
            "request_id",
            "workspace_key",
            "cwd",
            "profile",
            "timeout_seconds",
            "env",
            "network_mode",
            "command",
        }
        for p in payloads
    )
    schema = tool.get_schema()["function"]["parameters"]
    assert schema["properties"]["network_capability"]["enum"] == [
        "none",
        "dependency_egress",
    ]
    assert "network_capability" not in schema["required"]


@pytest.mark.parametrize(
    "invalid",
    [
        "egress",
        "host",
        "DEPENDENCY_EGRESS",
        " dependency_egress",
        None,
        True,
        1,
        [],
        {},
    ],
)
@pytest.mark.asyncio
async def test_shell_rejects_invalid_capability_before_transport(sandbox, invalid):
    _runner, ctx, payloads, _state = sandbox
    result = await ShellTool().execute(
        {"command": "echo ok", "network_capability": invalid}, ctx
    )
    assert not result.success
    assert "network_capability" in result.error
    assert payloads == []


@pytest.mark.parametrize("invalid", ["egress", None, True, 1, [], {}])
def test_execution_request_rejects_invalid_capability(invalid):
    with pytest.raises(ValueError, match="network_capability"):
        ExecutionRequest(
            workspace_key="task", command="echo ok", network_capability=invalid
        )


def test_trusted_git_cannot_accept_dependency_capability():
    with pytest.raises(ValueError, match="trusted_control"):
        ExecutionRequest(
            workspace_key="task",
            argv=("git", "status"),
            profile="trusted_control",
            network_capability="dependency_egress",
        )


@pytest.mark.asyncio
async def test_fresh_revocation_denies_explicit_shell_without_transport(sandbox):
    _runner, ctx, payloads, state = sandbox
    tool = ShellTool()
    args = {"command": "pip install example", "network_capability": "dependency_egress"}
    assert (await tool.execute(args, ctx)).success
    state.policy = AgentTeamNetworkPolicy.OFFLINE
    state.revision = "revision-2"
    denied = await tool.execute(args, ctx)
    assert not denied.success and "offline" in denied.error
    assert len(payloads) == 1
    assert (await tool.execute({"command": "echo ok"}, ctx)).success
    assert payloads[-1]["network_mode"] == "none"


@pytest.mark.asyncio
async def test_implicit_dependency_rechecks_policy_after_revocation(sandbox):
    runner, _ctx, payloads, state = sandbox
    request = ExecutionRequest(
        workspace_key=runner.workspace_key,
        command="pip install example-package",
        profile="dependency",
    )

    await runner.execute(request)
    state.policy = AgentTeamNetworkPolicy.OFFLINE
    state.revision = "revision-2"
    await runner.execute(request)

    assert [payload["network_mode"] for payload in payloads] == ["egress", "none"]


@pytest.mark.parametrize(
    ("policy", "profile", "capability", "mode"),
    [
        ("offline", "agent", "none", "none"),
        ("offline", "dependency", "none", "none"),
        ("offline", "agent", "dependency_egress", None),
        ("offline", "dependency", "dependency_egress", None),
        ("web_tools", "agent", "none", "none"),
        ("web_tools", "dependency", "none", "egress"),
        ("web_tools", "agent", "dependency_egress", "egress"),
        ("web_tools", "dependency", "dependency_egress", "egress"),
        ("full_access", "agent", "none", "egress"),
        ("full_access", "dependency", "none", "egress"),
        ("full_access", "agent", "dependency_egress", "egress"),
        ("full_access", "dependency", "dependency_egress", "egress"),
    ],
)
@pytest.mark.asyncio
async def test_execution_policy_matrix_preserves_wire_and_audit(
    sandbox, audits, policy, profile, capability, mode
):
    runner, _ctx, payloads, state = sandbox
    state.policy = AgentTeamNetworkPolicy(policy)
    request = ExecutionRequest(
        workspace_key=runner.workspace_key,
        command="printf fixture",
        profile=profile,
        network_capability=capability,
    )
    if mode is None:
        with pytest.raises(SandboxPolicyError, match="offline"):
            await runner.execute(request)
        assert payloads == []
    else:
        await runner.execute(request)
        assert len(payloads) == 1 and payloads[0]["network_mode"] == mode
        assert "network_capability" not in payloads[0]
        assert "action" not in payloads[0]
    assert audits[-1]["extra"]["capability"] == (
        "dependency_egress" if profile == "dependency" else capability
    )
    assert audits[-1]["extra"]["action"] == (
        "dependency_resolution"
        if profile == "dependency" or capability == "dependency_egress"
        else "agent_command"
    )


@pytest.mark.asyncio
async def test_policy_read_cancellation_is_audited_and_never_transported(
    sandbox, monkeypatch, audits
):
    runner, _ctx, payloads, _state = sandbox

    async def cancelled_policy_read():
        raise asyncio.CancelledError

    monkeypatch.setattr(
        sandbox_client, "get_agent_team_network_policy_state", cancelled_policy_read
    )
    request = ExecutionRequest(
        workspace_key=runner.workspace_key,
        command="echo secret-command",
        network_capability="dependency_egress",
    )
    with pytest.raises(asyncio.CancelledError):
        await runner.execute(request)
    assert payloads == []
    assert len(audits) == 1
    assert audits[0]["extra"]["result"] == "error_CancelledError"
    assert audits[0]["extra"]["capability"] == "dependency_egress"
    assert audits[0]["extra"]["action"] == "dependency_resolution"
    assert "secret-command" not in str(audits)


@pytest.mark.parametrize("cleanup_fails", [False, True])
@pytest.mark.asyncio
async def test_event_cancellation_audits_confirmed_cleanup_only(
    sandbox, monkeypatch, audits, cleanup_fails
):
    runner, _ctx, _payloads, _state = sandbox
    event = asyncio.Event()
    started = asyncio.Event()
    cancels = []

    async def transport(method, path, json_body=None, **kwargs):
        if path.endswith("/cancel"):
            cancels.append(path.split("/")[-2])
            if cleanup_fails:
                raise SandboxUnavailableError("secret-transport-token")
            return {
                "protocol_version": 2,
                "sandboxd_version": "test",
                "data": {
                    "request_id": cancels[-1],
                    "cancelled": True,
                    "state": "cancelled",
                },
            }
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "_request", transport)
    request = ExecutionRequest(
        workspace_key=runner.workspace_key,
        command="echo secret-command-token",
        profile="dependency",
        cancel_event=event,
    )
    task = asyncio.create_task(runner.execute(request))
    await asyncio.wait_for(started.wait(), timeout=1)
    event.set()
    if cleanup_fails:
        with pytest.raises(SandboxCleanupError):
            await asyncio.wait_for(task, timeout=1)
        expected = "error_SandboxCleanupError"
    else:
        result = await asyncio.wait_for(task, timeout=1)
        assert result.cancelled
        expected = "cancelled"
    assert len(cancels) == 1
    assert [a["extra"]["result"] for a in audits] == ["admitted", expected]
    for record in audits:
        assert record["extra"]["request"] == cancels[0]
        assert record["extra"]["capability"] == "dependency_egress"
        assert record["extra"]["action"] == "dependency_resolution"
        assert "secret-" not in str(record)


@pytest.mark.parametrize("policy", ["offline", "web_tools"])
@pytest.mark.asyncio
async def test_capability_does_not_bypass_local_full_access_gate(
    sandbox, monkeypatch, policy
):
    _runner, ctx, payloads, _state = sandbox

    async def read_policy():
        return AgentTeamNetworkPolicy(policy)

    monkeypatch.setattr(
        "backend.services.agent_team.execution.get_agent_team_network_policy",
        read_policy,
    )
    local = LocalExecutionRunner(ctx.workspace, ctx.workspace_service)
    with pytest.raises(ExecutionError, match="full_access"):
        await local.execute(
            ExecutionRequest(
                workspace_key=local.workspace_key,
                command="echo ok",
                network_capability="dependency_egress",
            )
        )
    assert payloads == []


@pytest.mark.asyncio
async def test_bootstrap_uses_sandbox_egress_under_web_tools(sandbox, monkeypatch):
    runner, ctx, payloads, _state = sandbox
    runner._egress_capability = "egress"
    workspace = runner.workspace
    (workspace / "requirements.txt").write_text("example-package\n", encoding="utf-8")
    service = AgentTeamGitWorkspaceService(workspace_service=ctx.workspace_service)

    async def config(key, **kwargs):
        return True if key == "agent_team_auto_install_deps" else None

    async def read_policy():
        return AgentTeamNetworkPolicy.WEB_TOOLS

    async def transport(method, path, json_body=None, **kwargs):
        payloads.append(json_body)
        if "-m venv" in json_body["command"]:
            venv = workspace / ".venv" / "sandbox"
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            (venv / "pyvenv.cfg").write_text("home = fixture\n", encoding="utf-8")
            for name in ("python", "pip"):
                (venv / "bin" / name).write_text("launcher", encoding="utf-8")
        return envelope(json_body["request_id"])

    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_dynamic_config", config
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_agent_team_network_policy",
        read_policy,
    )
    monkeypatch.setattr(runner, "_request", transport)
    report = await service.install_workspace_dependencies(workspace, runner)
    assert report is not None and report.status == "succeeded"
    assert [p["profile"] for p in payloads] == ["dependency", "dependency"]
    assert [p["network_mode"] for p in payloads] == ["egress", "egress"]
    assert (await ShellTool().execute({"command": "echo after-bootstrap"}, ctx)).success
    assert payloads[-1]["network_mode"] == "none"


@pytest.fixture
def audits():
    records = []
    sink = logger.add(
        lambda message: records.append(message.record),
        filter=lambda record: record["extra"].get("event") == "agent_sandbox_execution",
    )
    try:
        yield records
    finally:
        logger.remove(sink)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("success", "completed"),
        ("nonzero", "completed_nonzero"),
        ("timeout", "completed_timeout"),
        ("cancelled", "cancelled"),
        ("transport", "error_SandboxUnavailableError"),
        ("cleanup", "error_SandboxCleanupError"),
        ("task_cancel", "error_CancelledError"),
        ("policy", "denied_network_capability"),
        ("policy_read", "denied_policy_unavailable"),
        ("workspace", "denied_workspace_mismatch"),
        ("profile", "denied_unsupported_profile"),
        ("environment", "denied_environment"),
        ("protocol", "error_SandboxProtocolError"),
        ("remote", "error_SandboxRemoteError"),
    ],
)
@pytest.mark.asyncio
async def test_audits_every_outcome_with_capability_and_no_secrets(
    sandbox, monkeypatch, audits, outcome, expected
):
    runner, _ctx, _payloads, state = sandbox
    digest = "sha256:" + "a" * 64

    async def health():
        return sandbox_client._HealthData(
            ready=True,
            runtime="docker",
            profiles=["agent", "dependency"],
            egress_capability="egress",
            instance_id="fixture",
            workspace_root=str(runner.workspace_service.base_dir),
            runner_image_digest=digest,
        )

    monkeypatch.setattr(runner, "health", health)
    await runner.ensure_ready()
    if outcome == "policy":
        state.policy = AgentTeamNetworkPolicy.OFFLINE
    if outcome == "policy_read":

        async def unavailable():
            raise RuntimeError("database password=secret-database")

        monkeypatch.setattr(
            sandbox_client, "get_agent_team_network_policy_state", unavailable
        )

    async def transport(method, path, json_body=None, **kwargs):
        if outcome == "transport":
            raise SandboxUnavailableError("secret-transport")
        if outcome == "cleanup":
            raise SandboxCleanupError("secret-cleanup")
        if outcome == "task_cancel":
            raise asyncio.CancelledError
        if outcome == "protocol":
            raise SandboxProtocolError("secret-protocol-token")
        if outcome == "remote":
            raise SandboxRemoteError("RUNTIME_UNAVAILABLE", "secret-runtime-token")
        changes = {
            "nonzero": {"exit_code": 1},
            "timeout": {"timed_out": True},
            "cancelled": {"cancelled": True, "exit_code": None},
        }.get(outcome, {})
        return envelope(json_body["request_id"], **changes)

    monkeypatch.setattr(runner, "_request", transport)
    profile = "trusted_control" if outcome == "profile" else "agent"
    request = ExecutionRequest(
        workspace_key="other-task" if outcome == "workspace" else runner.workspace_key,
        command="echo secret-command-token",
        profile=profile,
        network_capability="none" if outcome == "profile" else "dependency_egress",
    )
    if outcome == "environment":
        # Exercise the runner's independent defensive boundary after request
        # validation, including secrecy of a subsequently injected environment.
        object.__setattr__(request, "env", {"GIT_ASKPASS": "secret-env-token"})
    if outcome in {
        "transport",
        "cleanup",
        "policy",
        "policy_read",
        "workspace",
        "profile",
        "environment",
        "protocol",
        "remote",
    }:
        with pytest.raises(
            (
                SandboxPolicyError,
                SandboxUnavailableError,
                SandboxCleanupError,
                SandboxProtocolError,
                SandboxRemoteError,
            )
        ):
            await runner.execute(request)
    elif outcome == "task_cancel":
        with pytest.raises(asyncio.CancelledError):
            await runner.execute(request)
    else:
        await runner.execute(request)
    assert audits
    for record in audits:
        data = record["extra"]
        assert data["capability"] == (
            "none" if outcome == "profile" else "dependency_egress"
        )
        assert data["action"] == (
            "agent_command" if outcome == "profile" else "dependency_resolution"
        )
        assert data["digest"] == digest
        assert set(data) == {
            "event",
            "task",
            "request",
            "profile",
            "policy",
            "mode",
            "revision",
            "digest",
            "result",
            "capability",
            "action",
        }
        assert "secret-" not in str(record)
    assert audits[-1]["extra"]["result"] == expected
    if outcome == "policy":
        assert audits[-1]["extra"]["policy"] == "offline"
        assert audits[-1]["extra"]["revision"] == "revision-1"
