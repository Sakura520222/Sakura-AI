"""Phase 2 acceptance regressions across direct admission and restoration."""

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services.agent_team import fullstack_expert as runtime
from backend.services.agent_team import repository_context as repository_module
from backend.services.agent_team.repository_context import (
    RepositoryContext,
    RepositoryContextError,
)
from backend.services.agent_team.skill_scope import SkillRestriction
from backend.services.agent_team.tools.base import BaseTool, ToolExecutor, ToolResult
from backend.services.agent_team.tools.read_tool import ReadTool
from backend.services.agent_team.tools.registry import get_tool_definitions_fresh
from backend.services.agent_team.tools.use_skill_tool import UseSkillTool
from tests.test_agent_harness_runtime import response
from tests.test_agent_repository_context import call, context, put, skill

pytest_plugins = ["tests.test_agent_harness_checkpoint"]


def admin_context(root, allowed='["read_file"]', content="BODY"):
    path = put(root, "admin/docs/SKILL.md", content)
    ctx = context(root)
    ctx.extra.update(
        skills_root=str(root / "admin"),
        skills_index={
            "docs": {
                "slug": "docs",
                "name": "Docs",
                "install_path": str(path),
                "allowed_tools": allowed,
            }
        },
        skills_cache={},
    )
    return ctx, path


class CommandTool(BaseTool):
    name = "run_command"

    async def execute(self, args, ctx):
        return ToolResult(True, output={"executed": args["command"]})


@pytest.mark.asyncio
async def test_disabled_direct_cached_schema_and_cleanup(tmp_path, monkeypatch):
    ctx, _ = admin_context(tmp_path)
    tool = UseSkillTool()
    assert (await tool.execute({"slug": "docs"}, ctx)).success
    monkeypatch.setattr(
        repository_module, "get_dynamic_config", AsyncMock(return_value=False)
    )
    for invoke in (
        lambda: tool.execute({"slug": "docs"}, ctx),
        lambda: ToolExecutor([tool]).execute_raw("use_skill", {"slug": "docs"}, ctx),
    ):
        result = await invoke()
        assert not result.success and result.error_code == "SKILLS_DISABLED"
        assert "BODY" not in str(result)
    schemas = await get_tool_definitions_fresh()
    assert "use_skill" not in [s["function"]["name"] for s in schemas]
    assert (await tool.execute({"slug": "docs", "end_skill": True}, ctx)).success
    assert not ctx.active_skill_tools


@pytest.mark.asyncio
async def test_disabled_resume_hides_stale_skills_but_keeps_repository_and_guidance(
    tmp_path, monkeypatch
):
    skill(tmp_path, ".agents/skills", body="REPO_BODY_SECRET")
    put(tmp_path, "AGENTS.md", "ROOT_CONVENTION")
    loaded = call("use_skill", "loaded", slug="docs")
    history = [
        {"role": "system", "content": "legacy"},
        {"role": "user", "content": "initial old metadata"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": loaded.id,
                    "function": {
                        "name": loaded.function.name,
                        "arguments": loaded.function.arguments,
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "loaded",
            "content": '{"content":"OLD_BODY_SECRET","description":"OLD_METADATA_SECRET"}',
        },
        {
            "role": "user",
            "content": "RAW HUMAN GUIDANCE",
            "metadata": {"guidance_ids": [7]},
        },
    ]
    agent = runtime.FullStackExpertAgent(
        tmp_path, context(tmp_path).workspace_service, initial_messages=history
    )
    client = SimpleNamespace(
        resolve_role_primary_candidate=AsyncMock(return_value=None),
        call_with_retry=AsyncMock(
            return_value=response(calls=[call("finish_task", "finish", summary="ok")])
        ),
    )
    monkeypatch.setattr(
        runtime,
        "create_agent_team_client",
        AsyncMock(return_value=(client, SimpleNamespace(agent_role="agent_team"))),
    )
    original = repository_module.get_dynamic_config

    async def dynamic(key, **kwargs):
        return (
            False
            if key == "agent_team_skills_enabled"
            else await original(key, **kwargs)
        )

    monkeypatch.setattr(repository_module, "get_dynamic_config", dynamic)
    result = await agent.execute(
        "task", "summary", skills_summary="ADMIN_METADATA_SECRET"
    )
    assert result.success
    request = client.call_with_retry.call_args.kwargs
    rendered = json.dumps(request["messages"])
    assert "SECRET" not in rendered
    assert "ROOT_CONVENTION" in rendered and "RAW HUMAN GUIDANCE" in rendered
    assert not agent._active_context.extra["skills_index"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selector,allowed,denied",
    [
        ("Shell(git status)", "git status", "git status --short"),
        ("Bash(git status:*)", "git status --short", "git commit -m x"),
        ("Bash(git *)", "git diff --stat", "git status; touch escaped"),
    ],
)
async def test_documented_selectors_enforced_and_projected(
    tmp_path, selector, allowed, denied
):
    ctx, _ = admin_context(tmp_path, json.dumps(["Read", selector]))
    executor = ToolExecutor([UseSkillTool(), CommandTool()])
    loaded = await executor.execute_raw("use_skill", {"slug": "docs"}, ctx)
    assert loaded.success, loaded.error
    assert (
        await executor.execute_raw("run_command", {"command": allowed}, ctx)
    ).success
    rejected = await executor.execute_raw("run_command", {"command": denied}, ctx)
    assert rejected.error_code == "SKILL_TOOL_RESTRICTED"
    schemas = await get_tool_definitions_fresh(ctx=ctx)
    names = [s["function"]["name"] for s in schemas]
    assert "run_command" in names and "read_file" in names and "write_file" not in names


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "git status && id",
        "git status\nid",
        "git status $(id)",
        "git status > out",
        "git status `id`",
        "git status | id",
    ],
)
async def test_prefix_selector_does_not_allow_shell_syntax(tmp_path, command):
    ctx, _ = admin_context(tmp_path, '["Bash(git *)"]')
    executor = ToolExecutor([UseSkillTool(), CommandTool()])
    assert (await executor.execute_raw("use_skill", {"slug": "docs"}, ctx)).success
    assert not (
        await executor.execute_raw("run_command", {"command": command}, ctx)
    ).success


@pytest.mark.asyncio
async def test_admin_size_contract_fresh_cache_and_nofollow(tmp_path):
    ctx, path = admin_context(tmp_path, content="A" * 70000)
    tool = UseSkillTool()
    first = await tool.execute({"slug": "docs"}, ctx)
    assert first.success and len(first.output["content"]) == 70000
    original_stat = path.stat()
    path.write_text("B" * 70000)
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    second = await tool.execute({"slug": "docs"}, ctx)
    assert second.success and second.output["content"] == "B" * 70000
    secret = put(tmp_path, "admin/innocent.txt", "INDIRECT_SECRET")
    path.unlink()
    path.symlink_to(secret)
    assert not (await tool.execute({"slug": "docs"}, ctx)).success


@pytest.mark.asyncio
async def test_listing_never_reads_main_body(tmp_path, monkeypatch):
    skill(tmp_path, ".agents/skills")
    repo = RepositoryContext(tmp_path)
    ctx = context(tmp_path, repo)
    ctx.extra["skills_index"] = repo.discover_skills()
    monkeypatch.setattr(
        repo, "read_text", lambda *a: pytest.fail("listing read Skill body")
    )
    result = await UseSkillTool().execute({"slug": "docs", "list_files": True}, ctx)
    assert result.success and "SKILL.md" in result.output["files"]


@pytest.mark.asyncio
async def test_scope_arrays_refresh_deletion_and_sibling_replacement(tmp_path):
    root = put(tmp_path, "AGENTS.md", "ROOT")
    backend = put(tmp_path, "backend/AGENTS.md", "BACKEND")
    put(tmp_path, "frontend/AGENTS.md", "FRONTEND")
    agent = runtime.FullStackExpertAgent(tmp_path, context(tmp_path).workspace_service)
    ctx = context(tmp_path, RepositoryContext(tmp_path))
    await agent._execute_tool_calls(
        [call("git_diff", "array", file_paths=["backend/x.py"])], ctx, 1
    )
    assert "BACKEND" in agent.messages[-1]["content"]
    await agent._execute_tool_calls(
        [call("write_file", "front", file_path="frontend/x.py", content="ok")], ctx, 2
    )
    snapshot = agent._repository_message(ctx)["content"]
    assert "FRONTEND" in snapshot and "BACKEND" not in snapshot
    backend.unlink()
    root.write_text("NEW_ROOT")
    await agent._execute_tool_calls(
        [call("write_file", "back", file_path="backend/x.py", content="ok")], ctx, 3
    )
    assert not (tmp_path / "backend/x.py").exists()
    assert "NEW_ROOT" in agent._repository_message(ctx)["content"]
    assert "BACKEND" not in agent._repository_message(ctx)["content"]


@pytest.mark.asyncio
async def test_atomic_skill_scope_resume_cannot_widen(
    tmp_path, persistence, monkeypatch
):
    monkeypatch.setattr(
        repository_module, "get_dynamic_config", AsyncMock(return_value=True)
    )
    service, _, _ = persistence
    session = await service.create_session(1, "agent")
    agent = runtime.FullStackExpertAgent(
        tmp_path,
        context(tmp_path).workspace_service,
        checkpoint=service,
        session_id=session.id,
    )
    ctx, _ = admin_context(tmp_path, '["Read", "Bash(git status:*)"]')
    loaded = call("use_skill", "skill", slug="docs")
    await agent._append_message(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": loaded.id,
                    "function": {
                        "name": loaded.function.name,
                        "arguments": loaded.function.arguments,
                    },
                }
            ],
        }
    )
    await agent._execute_tool_calls([loaded], ctx, 1)
    history = await service.load_messages(session.id)
    state = await service.load_tool_call_states(session.id)
    assert state["skill"]["status"] == "completed"
    assert history[-1].get("metadata", {}).get("skill_runtime_state")
    # Current metadata broadens; the persisted historical command ceiling remains.
    ctx.extra["skills_index"]["docs"]["allowed_tools"] = '["run_command", "write_file"]'
    restored = runtime.FullStackExpertAgent(
        tmp_path,
        context(tmp_path).workspace_service,
        checkpoint=service,
        session_id=session.id,
        initial_messages=history,
    )
    resume_ctx, _ = admin_context(tmp_path, '["run_command", "write_file"]')
    assert await restored._recover(resume_ctx) is None
    restored._restore_skill_workflows(resume_ctx)
    executor = ToolExecutor([CommandTool(), UseSkillTool()])
    assert (
        await executor.execute_raw(
            "run_command", {"command": "git status --short"}, resume_ctx
        )
    ).success
    assert not (
        await executor.execute_raw(
            "run_command", {"command": "git commit -m x"}, resume_ctx
        )
    ).success
    assert not (
        await executor.execute_raw("write_file", {"file_path": "x"}, resume_ctx)
    ).success


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime_state", [None, {}, {"version": 1, "active": {"docs": "corrupt"}}]
)
async def test_missing_corrupt_history_cannot_restore_unrestricted_skill(
    tmp_path, runtime_state
):
    history = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "s",
                    "function": {"name": "use_skill", "arguments": '{"slug":"docs"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "s",
            "content": '{"allowed_tools":["run_command"],"content":"untrusted"}',
            "metadata": {"skill_runtime_state": runtime_state},
        },
    ]
    agent = runtime.FullStackExpertAgent(
        tmp_path, context(tmp_path).workspace_service, initial_messages=history
    )
    ctx, _ = admin_context(tmp_path, '["run_command"]')
    agent._restore_skill_workflows(ctx)
    denied = await ToolExecutor([CommandTool()]).execute_raw(
        "run_command", {"command": "id"}, ctx
    )
    assert not denied.success and denied.error_code == "SKILL_TOOL_RESTRICTED"


@pytest.mark.asyncio
async def test_switch_changes_between_rounds_project_before_compression(
    tmp_path, monkeypatch
):
    ctx, _ = admin_context(tmp_path, content="OLD_BODY_SECRET")
    enabled = True
    requests = []
    compressed_inputs = []
    agent = runtime.FullStackExpertAgent(tmp_path, ctx.workspace_service)

    async def dynamic(key, **kwargs):
        return enabled if key == "agent_team_skills_enabled" else None

    async def model(**kwargs):
        nonlocal enabled
        requests.append(kwargs)
        if len(requests) == 1:
            enabled = False
            return response(calls=[call("use_skill", "disabled-race", slug="docs")])
        return response(calls=[call("finish_task", "finish", summary="ok")])

    async def compress(messages, **kwargs):
        compressed_inputs.append(json.dumps(messages))
        return messages

    client = SimpleNamespace(
        resolve_role_primary_candidate=AsyncMock(return_value=None),
        call_with_retry=model,
    )
    monkeypatch.setattr(repository_module, "get_dynamic_config", dynamic)
    monkeypatch.setattr(
        runtime,
        "create_agent_team_client",
        AsyncMock(return_value=(client, SimpleNamespace(agent_role="agent_team"))),
    )
    monkeypatch.setattr(runtime, "compress_agent_team_messages", compress)
    result = await agent.execute(
        "task", "task", skills_context=ctx.extra, skills_summary="OLD_METADATA_SECRET"
    )
    assert result.success
    assert "use_skill" in [s["function"]["name"] for s in requests[0]["tools"]]
    assert "use_skill" not in [s["function"]["name"] for s in requests[1]["tools"]]
    assert "SECRET" not in compressed_inputs[1]
    assert "OLD_METADATA_SECRET" in json.dumps(agent.messages)  # originals retained
    assert any(
        m.get("role") == "tool" and "SKILLS_DISABLED" in m.get("content", "")
        for m in agent.messages
    )


@pytest.mark.asyncio
async def test_metadata_listing_does_not_read_admin_body_and_exact_size_limit(
    tmp_path, monkeypatch
):
    ctx, path = admin_context(tmp_path, content="X" * (512 * 1024))
    original_read = RepositoryContext.read_text
    monkeypatch.setattr(
        RepositoryContext, "read_text", lambda *a: pytest.fail("listing loaded body")
    )
    assert (
        await UseSkillTool().execute({"slug": "docs", "list_files": True}, ctx)
    ).success
    monkeypatch.setattr(RepositoryContext, "read_text", original_read)
    assert (await UseSkillTool().execute({"slug": "docs"}, ctx)).success
    path.write_text("X" * (512 * 1024 + 1))
    assert not (await UseSkillTool().execute({"slug": "docs"}, ctx)).success


@pytest.mark.asyncio
async def test_empty_and_unsupported_selectors_fail_closed(tmp_path):
    for allowed in (
        "[]",
        '["Bash(git status && id)"]',
        '["Read(foo/**)"]',
        '["Bash(git* status)"]',
    ):
        ctx, _ = admin_context(tmp_path, allowed)
        executor = ToolExecutor([UseSkillTool(), CommandTool()])
        result = await executor.execute_raw("use_skill", {"slug": "docs"}, ctx)
        if allowed == "[]":
            assert result.success
            assert not (
                await executor.execute_raw("run_command", {"command": "id"}, ctx)
            ).success
        else:
            assert not result.success and result.error_code == "SKILL_METADATA_REJECTED"


@pytest.mark.asyncio
async def test_changed_repository_metadata_can_only_narrow_same_workflow(tmp_path):
    path = skill(tmp_path, ".agents/skills", tools='["Bash(git status:*)"]')
    repo = RepositoryContext(tmp_path)
    ctx = context(tmp_path, repo)
    ctx.extra["skills_index"] = repo.discover_skills()
    # Direct body API covers the same scoped workflow; executor command enforces it.
    assert (await UseSkillTool().execute({"slug": "docs"}, ctx)).success
    path.write_text(path.read_text().replace('"Bash(git status:*)"', "run_command"))
    ctx.extra["skills_index"] = repo.discover_skills()
    assert (await UseSkillTool().execute({"slug": "docs"}, ctx)).success
    executor = ToolExecutor([CommandTool()])
    assert not (
        await executor.execute_raw("run_command", {"command": "git commit -m x"}, ctx)
    ).success


@pytest.mark.asyncio
async def test_ending_workflow_is_persisted_and_preserves_other_skill(
    tmp_path, persistence, monkeypatch
):
    monkeypatch.setattr(
        repository_module, "get_dynamic_config", AsyncMock(return_value=True)
    )
    service, _, _ = persistence
    session = await service.create_session(1, "agent")
    agent = runtime.FullStackExpertAgent(
        tmp_path,
        context(tmp_path).workspace_service,
        checkpoint=service,
        session_id=session.id,
    )
    ctx, _ = admin_context(tmp_path)
    ctx.active_skill_tools.update(
        docs=SkillRestriction.from_metadata('["read_file"]'),
        other=SkillRestriction.from_metadata('["read_file"]'),
    )
    ended = call("use_skill", "end", slug="docs", end_skill=True)
    await agent._append_message(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": ended.id,
                    "function": {
                        "name": ended.function.name,
                        "arguments": ended.function.arguments,
                    },
                }
            ],
        }
    )
    await agent._execute_tool_calls([ended], ctx, 1)
    history = await service.load_messages(session.id)
    assert history[-1]["metadata"]["skill_runtime_state"]["operation"] == "end"
    assert "docs" not in ctx.active_skill_tools and "other" in ctx.active_skill_tools
    restored = runtime.FullStackExpertAgent(
        tmp_path, ctx.workspace_service, initial_messages=history
    )
    resume, _ = admin_context(tmp_path)
    resume.active_skill_tools.update(
        docs=SkillRestriction.deny(),
        other=SkillRestriction.from_metadata('["read_file"]'),
    )
    restored._restore_skill_workflows(resume)
    assert "docs" not in resume.active_skill_tools
    assert not resume.allows_skill_tool("run_command", {"command": "id"})


@pytest.mark.asyncio
async def test_runtime_scope_and_result_rollback_together(
    tmp_path, persistence, monkeypatch
):
    monkeypatch.setattr(
        repository_module, "get_dynamic_config", AsyncMock(return_value=True)
    )
    service, _, _ = persistence
    session = await service.create_session(1, "agent")
    agent = runtime.FullStackExpertAgent(
        tmp_path,
        context(tmp_path).workspace_service,
        checkpoint=service,
        session_id=session.id,
    )
    ctx, _ = admin_context(tmp_path)
    loaded = call("use_skill", "load", slug="docs")
    await agent._append_message(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": loaded.id,
                    "function": {
                        "name": loaded.function.name,
                        "arguments": loaded.function.arguments,
                    },
                }
            ],
        }
    )
    monkeypatch.setattr(service, "_get_tool_call", AsyncMock(return_value=None))
    with pytest.raises(ValueError, match="missing"):
        await agent._execute_tool_calls([loaded], ctx, 1)
    history = await service.load_messages(session.id)
    assert len(history) == 1  # neither successful result nor ceiling committed


def test_global_priority_remains_global_for_sakura_targets(tmp_path):
    put(tmp_path, "AGENTS.md", "ROOT")
    put(tmp_path, ".sakura/AGENTS.md", "GLOBAL")
    put(tmp_path, ".sakura/skills/docs/AGENTS.md", "SPECIFIC")
    repo = RepositoryContext(tmp_path)
    docs = repo.snapshot((".sakura/skills/docs/SKILL.md",))
    assert [doc.path for doc in docs].count(".sakura/AGENTS.md") == 1
    assert next(doc.scope for doc in docs if doc.path == ".sakura/AGENTS.md") == "."


@pytest.mark.asyncio
async def test_disabled_skills_do_not_censor_ordinary_repository_reads(
    tmp_path, monkeypatch
):
    put(tmp_path, ".agents/skills/docs/SKILL.md", "ordinary untrusted data")
    monkeypatch.setattr(
        repository_module, "get_dynamic_config", AsyncMock(return_value=False)
    )
    ctx = context(tmp_path)
    result = await ToolExecutor([ReadTool()]).execute_raw(
        "read_file", {"file_path": ".agents/skills/docs/SKILL.md"}, ctx
    )
    assert result.success and "ordinary untrusted data" in str(result.output)


@pytest.mark.asyncio
async def test_live_narrowing_survives_an_ordinary_tool_checkpoint(
    tmp_path, persistence, monkeypatch
):
    monkeypatch.setattr(
        repository_module, "get_dynamic_config", AsyncMock(return_value=True)
    )
    service, _, _ = persistence
    session = await service.create_session(1, "agent")
    agent = runtime.FullStackExpertAgent(
        tmp_path,
        context(tmp_path).workspace_service,
        checkpoint=service,
        session_id=session.id,
    )
    ctx, _ = admin_context(tmp_path, '["run_command"]')
    load = call("use_skill", "s", slug="docs")
    await agent._append_message(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": load.id,
                    "function": {
                        "name": load.function.name,
                        "arguments": load.function.arguments,
                    },
                }
            ],
        }
    )
    await agent._execute_tool_calls([load], ctx, 1)
    ctx.active_skill_tools["docs"] = ctx.active_skill_tools["docs"].intersect(
        SkillRestriction.from_metadata('["Bash(git status:*)"]')
    )
    agent.tool_executor.register(CommandTool())
    normal = call("run_command", "normal", command="git status --short")
    await agent._append_message(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": normal.id,
                    "function": {
                        "name": normal.function.name,
                        "arguments": normal.function.arguments,
                    },
                }
            ],
        }
    )
    await agent._execute_tool_calls([normal], ctx, 2)
    history = await service.load_messages(session.id)
    restored = runtime.FullStackExpertAgent(
        tmp_path, ctx.workspace_service, initial_messages=history
    )
    resume, _ = admin_context(tmp_path, '["run_command"]')
    restored._restore_skill_workflows(resume)
    assert not resume.allows_skill_tool("run_command", {"command": "git commit -m x"})
    assert resume.allows_skill_tool("run_command", {"command": "git status --short"})


@pytest.mark.asyncio
async def test_admin_fallback_and_attachments_reject_secret_indirection(tmp_path):
    ctx, _ = admin_context(tmp_path)
    secret = put(tmp_path, "admin/plain.txt", "SECRET")
    skill_dir = tmp_path / "admin/docs"
    (skill_dir / "helper.txt").symlink_to(secret)
    os.link(secret, skill_dir / "hard.txt")
    os.mkfifo(skill_dir / "pipe.txt")
    for filename in ("helper.txt", "hard.txt", "pipe.txt"):
        result = await UseSkillTool().execute({"slug": "docs", "file": filename}, ctx)
        assert not result.success and "SECRET" not in str(result)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "admin", target_is_directory=True)
    ctx.extra.pop("skills_root")
    ctx.extra["skills_index"]["docs"]["install_path"] = str(alias / "docs/SKILL.md")
    assert not (await UseSkillTool().execute({"slug": "docs"}, ctx)).success


def test_context_secret_directories_are_not_sources(tmp_path):
    put(tmp_path, ".aws/AGENTS.md", "SECRET")
    repo = RepositoryContext(tmp_path)
    assert "SECRET" not in repo.render(repo.instructions_for(".aws/x.py"))
    assert repo.diagnostics


@pytest.mark.asyncio
async def test_file_symlink_cannot_bypass_target_scope_guidance(tmp_path):
    put(tmp_path, "backend/AGENTS.md", "backend-specific guidance")
    target = put(tmp_path, "backend/file.py", "original")
    (tmp_path / "alias.py").symlink_to(target)
    with pytest.raises(RepositoryContextError, match="Unsafe"):
        RepositoryContext(tmp_path).instructions_for("alias.py")
    agent = runtime.FullStackExpertAgent(tmp_path, context(tmp_path).workspace_service)
    ctx = context(tmp_path, RepositoryContext(tmp_path))
    await agent._execute_tool_calls(
        [call("write_file", "alias", file_path="alias.py", content="bypassed")], ctx, 1
    )
    assert target.read_text() == "original"
    assert "REPOSITORY_CONTEXT_REJECTED" in agent.messages[-1]["content"]


@pytest.mark.parametrize("failed_content", ['{"error":"denied"}', "broken-json"])
def test_failed_skill_result_still_restores_runtime_ceiling(tmp_path, failed_content):
    ctx, _ = admin_context(tmp_path, '["run_command"]')
    history = []
    for ident, content, metadata in (
        (
            "load",
            '{"content":"untrusted body"}',
            {
                "skill_runtime_state": {
                    "version": 1,
                    "slug": "docs",
                    "operation": "activate",
                    "scope": [["run_command"]],
                }
            },
        ),
        (
            "fail",
            failed_content,
            {
                "skill_workflow_ceiling": {
                    "version": 1,
                    "active": {"docs": [["Bash(git status:*)"]]},
                }
            },
        ),
    ):
        history.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": ident,
                            "function": {
                                "name": "use_skill",
                                "arguments": '{"slug":"docs"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": ident,
                    "content": content,
                    "metadata": metadata,
                },
            ]
        )
    agent = runtime.FullStackExpertAgent(
        tmp_path, ctx.workspace_service, initial_messages=history
    )
    agent._restore_skill_workflows(ctx)
    assert not ctx.allows_skill_tool("run_command", {"command": "git commit -m x"})
    assert ctx.allows_skill_tool("run_command", {"command": "git status --short"})
