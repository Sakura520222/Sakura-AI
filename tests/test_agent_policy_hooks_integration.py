"""Real executor effects obey fresh policy and trusted lifecycle hooks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services.agent_team.capability_policy import (
    CapabilitySession,
    PolicySnapshot,
)
from backend.services.agent_team.tools.base import (
    BaseTool,
    ToolContext,
    ToolExecutor,
    ToolResult,
)
from backend.services.agent_team.tools.finish_task_tool import FinishTaskTool


class WriteMarker(BaseTool):
    name = "write_file"

    async def execute(self, args, ctx):
        from pathlib import Path

        (Path(ctx.workspace) / "marker").write_text("changed")
        return ToolResult(True, {"_modified_file": "marker"})


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["local", "sandbox", "unmapped"])
async def test_hook_workspace_placeholder_uses_selected_runner_namespace(
    monkeypatch, tmp_path, backend
):
    from backend.services.agent_team import execution, sandbox_client
    from backend.services.agent_team.execution import LocalExecutionRunner
    from backend.services.agent_team.lifecycle_hooks import (
        HookConfig,
        HookFailure,
        LifecycleHooks,
    )
    from backend.services.agent_team.network_policy import (
        AgentTeamNetworkPolicy,
        AgentTeamNetworkPolicyState,
    )
    from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService

    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("owner", "repo")
    captured = []

    async def policy():
        return PolicySnapshot("autonomous", "full_access")

    async def network():
        return AgentTeamNetworkPolicy.FULL_ACCESS

    async def network_state():
        return AgentTeamNetworkPolicyState(AgentTeamNetworkPolicy.FULL_ACCESS, "test")

    async def load():
        return (
            HookConfig(
                id="path", event="before_finish", argv=("test", "-d", "{workspace}")
            ),
        )

    async def audit(event):
        captured.append(event)

    monkeypatch.setattr(execution, "get_agent_team_network_policy", network)
    monkeypatch.setattr(
        sandbox_client, "get_agent_team_network_policy_state", network_state
    )
    if backend == "unmapped":
        runner = SimpleNamespace(execute=AsyncMock())
    elif backend == "local":
        runner = LocalExecutionRunner(workspace, service)
    else:
        runner = sandbox_client.SandboxExecutionRunner(
            str(workspace), service, socket_path=str(tmp_path / "sandboxd.sock")
        )

        async def transport(method, path, json_body=None, **kwargs):
            # Actual client serialization must name the daemon's mounted path,
            # not leak a host-only path into container argv.
            assert method == "POST" and path == "/v1/executions"
            assert json_body["argv"] == ["test", "-d", "/workspace"]
            assert json_body["cwd"] == "."
            return {
                "protocol_version": sandbox_client.PROTOCOL_VERSION,
                "sandboxd_version": "test",
                "data": {
                    "request_id": json_body["request_id"],
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "timed_out": False,
                    "cancelled": False,
                    "output_truncated": False,
                },
            }

        monkeypatch.setattr(runner, "_request", transport)
    hooks = LifecycleHooks(load, CapabilitySession(policy), audit)
    if backend == "unmapped":
        with pytest.raises(HookFailure, match="hook_workspace_mapping_unavailable"):
            await hooks.emit(
                "before_finish",
                ToolContext(str(workspace), service, execution_runner=runner),
            )
        runner.execute.assert_not_called()
        assert not any(e.get("kind") == "hook_effect" for e in captured)
        return
    await hooks.emit(
        "before_finish", ToolContext(str(workspace), service, execution_runner=runner)
    )
    assert captured[-1]["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sandbox", "local"])
async def test_workspace_write_profile_does_not_inherit_shell_network(
    monkeypatch, tmp_path, backend
):
    from backend.core import config
    from backend.services.agent_team import execution, sandbox_client
    from backend.services.agent_team.execution import (
        ExecutionRequest,
        LocalExecutionRunner,
    )
    from backend.services.agent_team.network_policy import (
        AgentTeamNetworkPolicy,
        AgentTeamNetworkPolicyState,
    )
    from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService

    original = config.get_dynamic_config_fresh

    async def setting(key):
        if key == "agent_team_permission_profile":
            return "workspace_write"
        return await original(key)

    async def network():
        return AgentTeamNetworkPolicy.FULL_ACCESS

    async def network_state():
        return AgentTeamNetworkPolicyState(AgentTeamNetworkPolicy.FULL_ACCESS, "test")

    monkeypatch.setattr(config, "get_dynamic_config_fresh", setting)
    monkeypatch.setattr(execution, "get_agent_team_network_policy", network)
    monkeypatch.setattr(
        sandbox_client, "get_agent_team_network_policy_state", network_state
    )
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("o", "r")
    if backend == "local":
        runner = LocalExecutionRunner(workspace, service)
        with pytest.raises(execution.ExecutionError):
            await runner.execute(
                ExecutionRequest(workspace_key=runner.workspace_key, argv=("true",))
            )
        return
    runner = sandbox_client.SandboxExecutionRunner(
        str(workspace), service, socket_path=str(tmp_path / "sandboxd.sock")
    )
    payloads = []

    async def transport(method, path, json_body=None, **kwargs):
        payloads.append(json_body)
        return {
            "protocol_version": sandbox_client.PROTOCOL_VERSION,
            "sandboxd_version": "test",
            "data": {
                "request_id": json_body["request_id"],
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "timed_out": False,
                "cancelled": False,
                "output_truncated": False,
            },
        }

    monkeypatch.setattr(runner, "_request", transport)
    await runner.execute(
        ExecutionRequest(workspace_key=runner.workspace_key, argv=("true",))
    )
    assert payloads[0]["network_mode"] == "none"


@pytest.mark.asyncio
async def test_executor_policy_is_fresh_and_not_forged_context(tmp_path):
    current = "autonomous"

    async def load():
        return PolicySnapshot(current, "web_tools")

    session = CapabilitySession(load)
    executor = ToolExecutor([WriteMarker()], capabilities=session)
    ctx = ToolContext(str(tmp_path), None, extra={"permission_profile": "full_access"})
    assert (await executor.execute_raw("write_file", {}, ctx)).success
    (tmp_path / "marker").unlink()
    current = "read_only"
    denied = await executor.execute_raw("write_file", {}, ctx)
    assert denied.error_code == "CAPABILITY_DENIED"
    assert not (tmp_path / "marker").exists()
    current = "autonomous"
    await session.close()
    assert not (await executor.execute_raw("write_file", {}, ctx)).success


@pytest.mark.asyncio
async def test_required_finish_hook_vetoes_then_repair_can_finish(tmp_path):
    from backend.services.agent_team.lifecycle_hooks import HookConfig, LifecycleHooks

    async def policy():
        return PolicySnapshot("autonomous", "web_tools")

    session = CapabilitySession(policy)
    hooks_config = [
        HookConfig(id="verify", event="before_finish", argv=("pytest", "-q"))
    ]

    async def load():
        return tuple(hooks_config)

    class Runner:
        success = False

        async def execute(self, request):
            assert request.command is None
            assert request.argv == ("pytest", "-q")
            return SimpleNamespace(
                exit_code=0 if self.success else 1,
                stdout="repair tests",
                stderr="",
                timed_out=False,
                infrastructure_error="",
            )

    runner = Runner()
    ctx = ToolContext(str(tmp_path), None, execution_runner=runner)
    events = []

    async def audit(value):
        events.append(value)

    hooks = LifecycleHooks(load, session, audit)
    executor = ToolExecutor([FinishTaskTool()], capabilities=session, hooks=hooks)
    args = {
        "summary": "done",
        "modified_files": [],
        "risk_level": "low",
        "test_result": "checked",
    }
    failed = await executor.execute_raw("finish_task", args, ctx)
    assert not failed.is_terminal and failed.error_code == "HOOK_FAILED"
    assert "repair tests" in failed.error
    runner.success = True
    result = await executor.execute_raw("finish_task", args, ctx)
    assert result.is_terminal
    assert [e["event"] for e in events if e.get("kind") == "lifecycle"] == [
        "before_tool",
        "before_finish",
        "after_tool",
        "before_tool",
        "before_finish",
        "after_tool",
        "after_finish",
    ]


@pytest.mark.asyncio
async def test_readonly_effect_denied_even_with_mcp_only_capability():
    async def policy():
        return PolicySnapshot("read_only", "web_tools")

    session = CapabilitySession(policy)
    assert not (await session.evaluate(("mcp.invoke",), read_only=False)).allowed
    assert (await session.evaluate(("mcp.invoke",), read_only=True)).allowed


@pytest.mark.asyncio
async def test_agent_lifecycle_reaches_actual_model_and_finish(monkeypatch, tmp_path):
    configured_harness(monkeypatch)
    import json
    from unittest.mock import AsyncMock

    from backend.services.agent_team import fullstack_expert as runtime
    from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService

    model = SimpleNamespace(
        resolve_role_primary_candidate=AsyncMock(return_value=None),
        call_with_retry=AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="",
                            tool_calls=[
                                SimpleNamespace(
                                    id="done",
                                    function=SimpleNamespace(
                                        name="finish_task",
                                        arguments=json.dumps({"summary": "done"}),
                                    ),
                                )
                            ],
                        )
                    )
                ],
                usage=None,
            )
        ),
    )
    monkeypatch.setattr(
        runtime,
        "create_agent_team_client",
        AsyncMock(return_value=(model, SimpleNamespace(agent_role="agent_team"))),
    )
    agent = runtime.FullStackExpertAgent(tmp_path, AgentTeamWorkspaceService(tmp_path))
    assert (await agent.execute("task", "task")).success
    events = [
        m["metadata"]["harness_event"]["event"]
        for m in agent.messages
        if "harness_event" in m.get("metadata", {})
        and m["metadata"]["harness_event"].get("kind") == "lifecycle"
    ]
    assert events == [
        "session_start",
        "before_model",
        "after_model",
        "before_tool",
        "before_finish",
        "after_tool",
        "after_finish",
    ]
    sent = model.call_with_retry.call_args.kwargs["messages"]
    assert not any("harness_event" in m.get("metadata", {}) for m in sent)
    assert agent._harness.capabilities._closed


@pytest.mark.asyncio
async def test_finish_verification_and_admission_share_exclusive_barrier(tmp_path):

    from backend.services.agent_team.execution import ExecutionResult
    from backend.services.agent_team.lifecycle_hooks import HookConfig, LifecycleHooks
    from backend.services.agent_team.tool_scheduler import workspace_barrier

    async def policy():
        return PolicySnapshot("autonomous", "web_tools")

    session = CapabilitySession(policy)

    async def load():
        return (HookConfig(id="verify", event="before_finish", argv=("verify",)),)

    events = []

    async def audit(event):
        events.append(event)

    class Runner:
        async def execute(self, request):
            assert workspace_barrier(str(tmp_path)).writer
            return ExecutionResult(exit_code=0)

    class ObservedFinish(FinishTaskTool):
        async def execute(self, args, ctx):
            assert workspace_barrier(str(tmp_path)).writer
            return await super().execute(args, ctx)

    hooks = LifecycleHooks(load, session, audit)
    # Exact production FinishTaskTool remains the only terminal type.
    executor = ToolExecutor([FinishTaskTool()], capabilities=session, hooks=hooks)
    original = executor.get_tool("finish_task").execute

    async def observed(args, ctx):
        assert workspace_barrier(str(tmp_path)).writer
        assert any(e.get("kind") == "hook_effect" for e in events)
        return await original(args, ctx)

    executor.get_tool("finish_task").execute = observed
    result = await executor.execute_raw(
        "finish_task",
        {"summary": "done"},
        ToolContext(str(tmp_path), None, execution_runner=Runner()),
    )
    assert result.is_terminal


@pytest.mark.asyncio
async def test_child_emits_lifecycle_but_main_command_hooks_do_not_block_finish(
    tmp_path,
):
    from backend.services.agent_team.capability_policy import READ_ONLY_CAPABILITIES
    from backend.services.agent_team.lifecycle_hooks import HookConfig, LifecycleHooks

    async def policy():
        return PolicySnapshot("autonomous", "web_tools")

    session = CapabilitySession(policy, ceiling=READ_ONLY_CAPABILITIES)

    async def load():
        return (HookConfig(id="lint", event="before_finish", argv=("lint",)),)

    events = []

    async def audit(event):
        events.append(event)

    hooks = LifecycleHooks(load, session, audit, read_only=True)
    executor = ToolExecutor(
        [FinishTaskTool()], read_only=True, capabilities=session, hooks=hooks
    )
    result = await executor.execute_raw(
        "finish_task",
        {"summary": "analysis complete"},
        ToolContext(str(tmp_path), None),
    )
    assert result.is_terminal
    assert any(e.get("event") == "before_finish" for e in events)
    assert not any(e.get("kind") == "hook_effect" for e in events)


@pytest.mark.asyncio
async def test_worker_publication_requires_fresh_capability_and_persists_denial(
    monkeypatch,
):
    from unittest.mock import AsyncMock

    from backend.services.agent_team import capability_policy
    from backend.services.agent_team.conversation_checkpoint import (
        ConversationCheckpointService,
    )
    from backend.workers.agent_team_worker import AgentTeamWorker

    async def readonly():
        return PolicySnapshot("read_only", "web_tools")

    monkeypatch.setattr(capability_policy, "load_runtime_policy", readonly)
    audit = AsyncMock()
    monkeypatch.setattr(ConversationCheckpointService, "record_control_event", audit)
    worker = AgentTeamWorker()
    assert not await worker._check_control_capability(
        123, "git", ("git.write", "git.push")
    )
    assert audit.call_args.args[0]["event"] == "capability_denied"
    assert audit.call_args.args[0]["task_id"] == 123


def configured_harness(monkeypatch, *, hooks=(), policy="autonomous"):
    from backend.services.agent_team import fullstack_expert
    from backend.services.agent_team.harness_runtime import HarnessRuntime
    from backend.services.agent_team.mcp_runtime import MCPConfig

    async def plugins():
        return SimpleNamespace(hooks=hooks, mcp=MCPConfig())

    async def load_policy():
        return PolicySnapshot(policy, "web_tools")

    def create(**kwargs):
        return HarnessRuntime(
            load_plugins=plugins,
            resolve_credential=None,
            load_policy=load_policy,
            **kwargs,
        )

    monkeypatch.setattr(fullstack_expert, "create_harness_runtime", create)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_failed_and_cancelled_model_lifecycle_drains_and_closes(
    monkeypatch, tmp_path, failure
):
    import asyncio
    from unittest.mock import AsyncMock

    from backend.services.agent_team import fullstack_expert as runtime
    from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService

    configured_harness(monkeypatch)
    model = SimpleNamespace(
        resolve_role_primary_candidate=AsyncMock(return_value=None),
        call_with_retry=AsyncMock(
            side_effect=RuntimeError("provider broke")
            if failure == "error"
            else asyncio.CancelledError()
        ),
    )
    monkeypatch.setattr(
        runtime,
        "create_agent_team_client",
        AsyncMock(return_value=(model, SimpleNamespace(agent_role="agent_team"))),
    )
    agent = runtime.FullStackExpertAgent(tmp_path, AgentTeamWorkspaceService(tmp_path))
    if failure == "error":
        with pytest.raises(RuntimeError, match="provider broke"):
            await agent.execute("task", "task")
    else:
        assert (await agent.execute("task", "task")).error == "cancelled"
    events = [
        m["metadata"]["harness_event"]["event"]
        for m in agent.messages
        if m.get("metadata", {}).get("harness_event", {}).get("kind") == "lifecycle"
    ]
    assert events == [
        "session_start",
        "before_model",
        "after_model",
        "task_failed" if failure == "error" else "task_cancelled",
    ]
    assert agent._harness.capabilities._closed and agent._harness.mcp._closed


@pytest.mark.asyncio
async def test_real_local_formatter_effect_is_audited_before_tool(
    monkeypatch, tmp_path
):
    from backend.services.agent_team import execution
    from backend.services.agent_team.execution import LocalExecutionRunner
    from backend.services.agent_team.lifecycle_hooks import HookConfig, LifecycleHooks
    from backend.services.agent_team.network_policy import AgentTeamNetworkPolicy
    from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService

    async def network():
        return AgentTeamNetworkPolicy.FULL_ACCESS

    monkeypatch.setattr(execution, "get_agent_team_network_policy", network)
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("o", "r")
    runner = LocalExecutionRunner(workspace, service)

    async def policy():
        return PolicySnapshot("autonomous", "full_access")

    session = CapabilitySession(policy)

    async def load():
        return (
            HookConfig(
                id="formatter",
                event="before_tool",
                argv=(
                    "python3",
                    "-c",
                    "from pathlib import Path; Path('formatted').write_text('ok')",
                ),
            ),
        )

    events = []

    async def audit(event):
        events.append(event)

    hooks = LifecycleHooks(load, session, audit)
    executor = ToolExecutor([WriteMarker()], capabilities=session, hooks=hooks)
    ctx = ToolContext(str(workspace), service, execution_runner=runner)
    result = await executor.execute_tool_call(
        SimpleNamespace(
            id="write", function=SimpleNamespace(name="write_file", arguments="{}")
        ),
        ctx,
    )
    assert (
        result.success
        and (workspace / "formatted").read_text() == "ok"
        and (workspace / "marker").exists()
    )
    assert any(
        e.get("kind") == "hook_effect" and e["tool_call_id"] == "write" for e in events
    )
    assert [e["event"] for e in events if e.get("kind") == "lifecycle"] == [
        "before_tool",
        "before_write",
        "after_write",
        "after_tool",
    ]


@pytest.mark.asyncio
async def test_interrupted_read_with_historical_hook_mutation_needs_reconciliation(
    tmp_path,
):
    from unittest.mock import AsyncMock

    from backend.services.agent_team.fullstack_expert import FullStackExpertAgent
    from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService

    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "read", "function": {"name": "read_file", "arguments": "{}"}}
            ],
        },
        {
            "role": "user",
            "content": "",
            "metadata": {
                "harness_event": {
                    "kind": "hook_effect",
                    "tool_call_id": "read",
                    "effect": "workspace_write",
                    "status": "admitted",
                }
            },
        },
    ]
    checkpoint = SimpleNamespace(
        load_tool_call_states=AsyncMock(
            return_value={"read": {"status": "running", "name": "read_file"}}
        ),
        load_session_result=AsyncMock(return_value=None),
    )
    agent = FullStackExpertAgent(
        tmp_path,
        AgentTeamWorkspaceService(tmp_path),
        checkpoint=checkpoint,
        session_id=1,
        initial_messages=messages,
    )
    result = await agent._recover(agent._build_context())
    assert result.error == "reconciliation_required"


@pytest.mark.asyncio
async def test_after_hook_cannot_replace_original_tool_error(tmp_path):
    from backend.services.agent_team.execution import ExecutionResult
    from backend.services.agent_team.lifecycle_hooks import HookConfig, LifecycleHooks

    async def policy():
        return PolicySnapshot("autonomous", "web_tools")

    async def config():
        return (HookConfig(id="lint", event="after_tool", argv=("lint",)),)

    async def audit(event):
        pass

    class Failing(BaseTool):
        name = "read_file"

        def is_read_only(self):
            return True

        async def execute(self, args, ctx):
            return ToolResult(False, error="original failure", error_code="ORIGINAL")

    class Runner:
        async def execute(self, request):
            return ExecutionResult(exit_code=1, stderr="hook failure")

    session = CapabilitySession(policy)
    executor = ToolExecutor(
        [Failing()], capabilities=session, hooks=LifecycleHooks(config, session, audit)
    )
    result = await executor.execute_raw(
        "read_file", {}, ToolContext(str(tmp_path), None, execution_runner=Runner())
    )
    assert result.error == "original failure" and result.error_code == "ORIGINAL"


@pytest.mark.asyncio
async def test_hook_cancellation_drains_operation_before_releasing_workspace(tmp_path):
    import asyncio

    from backend.services.agent_team.execution import ExecutionResult
    from backend.services.agent_team.lifecycle_hooks import HookConfig, LifecycleHooks
    from backend.services.agent_team.tool_scheduler import workspace_barrier

    entered, released = asyncio.Event(), asyncio.Event()

    async def policy():
        return PolicySnapshot("autonomous", "web_tools")

    async def config():
        return (HookConfig(id="verify", event="before_finish", argv=("verify",)),)

    async def audit(event):
        pass

    class Runner:
        async def execute(self, request):
            entered.set()
            await request.cancel_event.wait()
            await released.wait()
            assert workspace_barrier(str(tmp_path)).writer
            return ExecutionResult(cancelled=True)

    session = CapabilitySession(policy)
    executor = ToolExecutor(
        [FinishTaskTool()],
        capabilities=session,
        hooks=LifecycleHooks(config, session, audit),
    )
    task = asyncio.create_task(
        executor.execute_raw(
            "finish_task",
            {"summary": "done"},
            ToolContext(str(tmp_path), None, execution_runner=Runner()),
        )
    )
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and workspace_barrier(str(tmp_path)).writer
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not workspace_barrier(str(tmp_path)).writer


@pytest.mark.asyncio
async def test_mcp_mutation_denied_by_readonly_profile_even_if_only_invoke_required(
    tmp_path,
):
    from unittest.mock import AsyncMock

    from backend.services.agent_team.mcp_runtime import MCPToolPolicy
    from backend.services.agent_team.tools.mcp_tool import MCPTool

    async def policy():
        return PolicySnapshot("read_only", "web_tools")

    remote = SimpleNamespace(invoke=AsyncMock())
    tool = MCPTool(
        remote,
        "trusted",
        "mutate",
        MCPToolPolicy(),
        {
            "description": "Untrusted hint readOnly=true",
            "input_schema": {"type": "object"},
        },
        "digest",
    )
    executor = ToolExecutor([tool], capabilities=CapabilitySession(policy))
    result = await executor.execute_raw(
        tool.name,
        {},
        ToolContext(
            str(tmp_path),
            None,
            extra={"read_only": False, "permission_profile": "full_access"},
        ),
    )
    assert result.error_code == "CAPABILITY_DENIED"
    remote.invoke.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_refresh_removes_previous_adapters_and_intersects_policy(tmp_path):
    from unittest.mock import AsyncMock

    from backend.services.agent_team.mcp_runtime import MCPToolPolicy
    from backend.services.agent_team.tools.mcp_tool import MCPTool
    from backend.services.agent_team.tools.registry import get_tool_definitions_fresh

    async def policy():
        return PolicySnapshot("autonomous", "web_tools")

    remote = SimpleNamespace(invoke=AsyncMock())

    def adapter(name):
        return MCPTool(
            remote,
            "trusted",
            name,
            MCPToolPolicy(read_only=True),
            {
                "description": "remote",
                "input_schema": {"type": "object", "properties": {}},
            },
            name,
        )

    one, two = adapter("one"), adapter("two")
    executor = ToolExecutor([FinishTaskTool()], capabilities=CapabilitySession(policy))
    ctx = ToolContext(
        str(tmp_path),
        None,
        executor=executor,
        mcp_runtime=SimpleNamespace(discover=AsyncMock(side_effect=[[one], [two]])),
    )
    first = await get_tool_definitions_fresh(ctx=ctx)
    second = await get_tool_definitions_fresh(ctx=ctx)
    assert one.name in {s["function"]["name"] for s in first}
    assert one.name not in {s["function"]["name"] for s in second}
    assert executor.get_tool(one.name) is None and executor.get_tool(two.name) is two


@pytest.mark.parametrize(
    "argv",
    [
        ("echo", "prefix{workspace}"),
        ("echo", "{model_command}"),
        ("echo", "{workspace}/path"),
    ],
)
def test_hook_placeholders_cannot_interpolate_model_or_repository_strings(argv):
    from backend.services.agent_team.lifecycle_hooks import HookConfig

    with pytest.raises(ValueError):
        HookConfig(id="bad", event="before_tool", argv=argv)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    ["session_start", "before_model", "after_model", "task_failed", "task_cancelled"],
)
@pytest.mark.parametrize("completed", [False, True])
async def test_non_tool_hook_effect_recovery_requires_durable_completion(
    tmp_path, event, completed
):
    from backend.services.agent_team.fullstack_expert import FullStackExpertAgent
    from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService

    marker = {
        "kind": "hook_effect",
        "event": event,
        "hook_id": "format",
        "effect_id": "invocation",
        "effect": "workspace_write",
        "status": "admitted",
    }
    messages = [{"role": "user", "content": "", "metadata": {"harness_event": marker}}]
    if completed:
        messages.append(
            {
                "role": "user",
                "content": "",
                "metadata": {"harness_event": {**marker, "status": "completed"}},
            }
        )
    agent = FullStackExpertAgent(
        tmp_path, AgentTeamWorkspaceService(tmp_path), initial_messages=messages
    )
    result = await agent._recover(agent._build_context())
    assert result is None if completed else result.error == "reconciliation_required"


@pytest.mark.asyncio
async def test_hook_effect_completion_is_persisted_after_actual_effect(tmp_path):
    from backend.services.agent_team.execution import ExecutionResult
    from backend.services.agent_team.lifecycle_hooks import HookConfig, LifecycleHooks

    async def policy():
        return PolicySnapshot("autonomous", "web_tools")

    async def config():
        return (HookConfig(id="format", event="session_start", argv=("format",)),)

    events = []

    async def audit(event):
        events.append(event)

    class Runner:
        async def execute(self, request):
            assert events[-1]["status"] == "admitted"
            (tmp_path / "effect").write_text("ran")
            return ExecutionResult(exit_code=0)

    hooks = LifecycleHooks(config, CapabilitySession(policy), audit)
    await hooks.emit(
        "session_start", ToolContext(str(tmp_path), None, execution_runner=Runner())
    )
    effects = [e for e in events if e.get("kind") == "hook_effect"]
    assert [e["status"] for e in effects] == ["admitted", "completed"]
    assert effects[0]["effect_id"] == effects[1]["effect_id"]
    assert (tmp_path / "effect").read_text() == "ran"


@pytest.mark.asyncio
async def test_real_hook_process_is_drained_on_cancellation(monkeypatch, tmp_path):
    import asyncio
    import os

    from backend.services.agent_team import execution
    from backend.services.agent_team.execution import LocalExecutionRunner
    from backend.services.agent_team.lifecycle_hooks import HookConfig, LifecycleHooks
    from backend.services.agent_team.network_policy import AgentTeamNetworkPolicy
    from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService

    async def network():
        return AgentTeamNetworkPolicy.FULL_ACCESS

    monkeypatch.setattr(execution, "get_agent_team_network_policy", network)
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("o", "r")

    async def policy():
        return PolicySnapshot("autonomous", "full_access")

    async def config():
        return (
            HookConfig(
                id="long",
                event="session_start",
                argv=(
                    "python3",
                    "-c",
                    "import os,time; from pathlib import Path; Path('pid').write_text(str(os.getpid())); time.sleep(60)",
                ),
            ),
        )

    events = []

    async def audit(event):
        events.append(event)

    hooks = LifecycleHooks(config, CapabilitySession(policy), audit)
    task = asyncio.create_task(
        hooks.emit(
            "session_start",
            ToolContext(
                str(workspace),
                service,
                execution_runner=LocalExecutionRunner(workspace, service),
            ),
        )
    )
    try:
        async with asyncio.timeout(5):
            while not (workspace / "pid").exists():
                await asyncio.sleep(0.01)
        pid = int((workspace / "pid").read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        effects = [event for event in events if event.get("kind") == "hook_effect"]
        assert [event["status"] for event in effects] == ["admitted"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_dependency_denial_skips_installer_and_reports_reason(
    monkeypatch, tmp_path
):
    from unittest.mock import AsyncMock

    from backend.services.agent_team import capability_policy
    from backend.workers.agent_team_worker import AgentTeamWorker

    async def policy():
        return PolicySnapshot("read_only", "web_tools")

    monkeypatch.setattr(capability_policy, "load_runtime_policy", policy)
    service = SimpleNamespace(
        workspace_service=object(),
        prepare_workspace_for_execution_backend=AsyncMock(),
        install_workspace_dependencies=AsyncMock(),
    )
    runner = object()
    worker = AgentTeamWorker()
    worker._create_agent_execution_runner = AsyncMock(return_value=runner)
    assert await worker._admit_workspace_runner(service, tmp_path) is runner
    service.prepare_workspace_for_execution_backend.assert_awaited_once()
    service.install_workspace_dependencies.assert_not_awaited()
    assert service.dependency_setup_report.status == "policy_denied"


@pytest.mark.asyncio
async def test_harness_closes_mcp_before_capability_session():
    from backend.services.agent_team.harness_runtime import HarnessRuntime
    from backend.services.agent_team.mcp_runtime import MCPConfig

    async def config():
        return SimpleNamespace(mcp=MCPConfig(), hooks=())

    async def audit(event):
        pass

    runtime = HarnessRuntime(load_plugins=config, resolve_credential=None, audit=audit)

    async def close_mcp():
        assert not runtime.capabilities._closed
        raise RuntimeError("transport close failed")

    runtime.mcp.close = close_mcp
    with pytest.raises(RuntimeError, match="transport close failed"):
        await runtime.close()
    assert runtime.capabilities._closed


@pytest.mark.parametrize("argv", ["echo", None, {"command": "echo"}])
def test_hook_arguments_must_be_an_array(argv):
    from backend.services.agent_team.lifecycle_hooks import HookConfig

    with pytest.raises(ValueError):
        HookConfig(id="bad", event="session_start", argv=argv)


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", [False, True])
async def test_pr_body_update_rechecks_publication_policy_after_generation(
    monkeypatch, revoked
):
    from unittest.mock import AsyncMock

    from backend.services.agent_team import capability_policy
    from backend.services.agent_team.conversation_checkpoint import (
        ConversationCheckpointService,
    )
    from backend.workers.agent_team_worker import AgentTeamWorker

    profile = "autonomous"

    async def policy():
        return PolicySnapshot(profile, "web_tools")

    async def generate(**kwargs):
        nonlocal profile
        if revoked:
            profile = "workspace_write"
        return "generated"

    audit = AsyncMock()
    monkeypatch.setattr(capability_policy, "load_runtime_policy", policy)
    monkeypatch.setattr(ConversationCheckpointService, "record_control_event", audit)
    service = SimpleNamespace(
        build_pr_body=lambda **kwargs: "fallback",
        generate_pr_body=generate,
        update_pull_request_body=AsyncMock(),
    )
    task = SimpleNamespace(
        id=42,
        title="task",
        summary="",
        source_type="issue",
        source_issue_number=1,
        repo_owner="o",
        repo_name="r",
        pr_number=2,
    )
    outcome = SimpleNamespace(
        fullstack_result=None, review_result=None, modified_files=[]
    )
    await AgentTeamWorker()._update_pr_body(service, task, outcome, 1)
    assert service.update_pull_request_body.await_count == (0 if revoked else 1)
    assert audit.call_args.args[0]["event"] == (
        "capability_denied" if revoked else "capability_allowed"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [False, True])
async def test_after_hook_audit_failure_preserves_tool_error_and_sanitizes_success(
    tmp_path, success
):
    from backend.services.agent_team.lifecycle_hooks import LifecycleHooks

    async def policy():
        return PolicySnapshot("autonomous", "web_tools")

    async def config():
        return ()

    async def audit(event):
        if event.get("event") == "after_tool":
            raise RuntimeError("private driver credentials")

    class Tool(BaseTool):
        name = "read_file"

        async def execute(self, args, ctx):
            return ToolResult(
                success,
                error="" if success else "original failure",
                error_code="" if success else "ORIGINAL",
            )

    session = CapabilitySession(policy)
    executor = ToolExecutor(
        [Tool()], capabilities=session, hooks=LifecycleHooks(config, session, audit)
    )
    result = await executor.execute_raw(
        "read_file", {}, ToolContext(str(tmp_path), None)
    )
    assert not result.success
    assert result.error == ("hook_audit_unavailable" if success else "original failure")
    assert result.error_code == ("HOOK_FAILED" if success else "ORIGINAL")
