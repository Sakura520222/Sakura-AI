"""Runtime E2E: real files, SQLite, child sessions, HTTP MCP and command hooks.

Only model responses and event publication are substitutes. No paid provider,
GitHub publication, production database or Docker daemon is involved.
"""

import asyncio
import json
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core import config
from backend.models import agent_team_models as models
from backend.models.database import AppConfig, Base
from backend.services.agent_team import fullstack_expert as runtime
from backend.services.agent_team.execution import LocalExecutionRunner
from backend.services.agent_team.iteration_loop import IterationLoopService
from backend.services.agent_team.mcp_runtime import tool_name
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService
from tests import test_agent_mcp_http as protocol_fixtures
from tests import test_agent_subagents as subagent_fixtures
from tests.test_agent_subagents import call, response

persistence = subagent_fixtures.persistence
# Reuse the protocol fixture's logger isolation for the server in this process.
capture_client_debug_logs = protocol_fixtures.capture_client_debug_logs


@pytest.mark.asyncio
async def test_repository_skill_child_mcp_hook_repair_and_durable_finish(
    persistence, tmp_path, monkeypatch
):
    checkpoint, engine = persistence
    Base.metadata.create_all(
        engine,
        tables=[
            models.AgentTeamUserPrompt.__table__,
            models.AgentTeamConversationContext.__table__,
        ],
    )
    monkeypatch.setattr(config, "_dynamic_config_cache", OrderedDict())
    monkeypatch.setattr(runtime, "_publish_ai_request", AsyncMock())
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("o", "r")
    (workspace / "AGENTS.md").write_text("REPOSITORY_RULE: review all evidence.\n")
    (workspace / "evidence.txt").write_text("observed evidence")
    skill = workspace / ".agents/skills/review/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: Review\ndescription: Review relevant evidence\n---\n"
        "ON_DEMAND_WORKFLOW_BODY\n"
    )
    calls = []
    child_id = None
    root_round, child_round = 0, 0
    remote = tool_name("fixture", "echo")

    async def model(messages, tools, **kwargs):
        nonlocal root_round, child_round, child_id
        calls.append(messages)
        names = {tool["function"]["name"] for tool in tools}
        assert remote in names
        assert "REPOSITORY_RULE" not in messages[0]["content"]
        results = {
            m["tool_call_id"]: json.loads(m["content"])
            for m in messages
            if m["role"] == "tool"
        }
        child = messages[0]["content"].startswith("You are Sakura's read-only subagent")
        if child:
            assert not names & {"write_file", "run_command", "spawn_agent"}
            child_round += 1
            if child_round == 1:
                reply = response(
                    call("write_file", "denied", file_path="forbidden", content="x")
                )
            elif child_round == 2:
                assert results["denied"]["error_code"] == "SUBAGENT_TOOL_RESTRICTED"
                reply = response(
                    call("read_file", "child-read", file_path="evidence.txt")
                )
            elif child_round == 3:
                assert "observed evidence" in str(results["child-read"])
                reply = response(call(remote, "child-mcp", text="child evidence"))
            else:
                assert "[REDACTED]" in str(results["child-mcp"])
                reply = response(
                    call("finish_task", "child-finish", summary="child inspected")
                )
        else:
            root_round += 1
            if root_round == 1:
                assert "REPOSITORY_RULE" in str(messages)
                assert "ON_DEMAND_WORKFLOW_BODY" not in str(messages)
                reply = response(text="I will inspect and implement the task.")
            elif root_round == 2:
                reply = response(call("use_skill", "skill", slug="review"))
            elif root_round == 3:
                assert "ON_DEMAND_WORKFLOW_BODY" in str(messages)
                reply = response(
                    call("spawn_agent", "spawn", task="inspect evidence independently")
                )
            elif root_round == 4:
                child_id = results["spawn"]["agent_id"]
                reply = response(call("wait_agent", "wait", agent_id=child_id))
            elif root_round == 5:
                assert results["wait"]["status"] == "completed"
                assert results["wait"]["result"]["summary"] == "child inspected"
                reply = response(call(remote, "main-mcp", text="main evidence"))
            elif root_round == 6:
                reply = response(
                    call("finish_task", "premature", summary="not verified yet")
                )
            elif root_round == 7:
                assert results["premature"]["error_code"] == "HOOK_FAILED"
                reply = response(
                    call("write_file", "repair", file_path="verified.txt", content="ok")
                )
            else:
                assert root_round == 8
                reply = response(
                    call(
                        "finish_task",
                        "finish",
                        summary="verified",
                        modified_files=["verified.txt"],
                    )
                )
        reply.usage = SimpleNamespace(prompt_tokens=7, completion_tokens=3)
        return reply

    async def create_client(**kwargs):
        return (
            SimpleNamespace(
                resolve_role_primary_candidate=AsyncMock(return_value=None),
                call_with_retry=model,
            ),
            SimpleNamespace(agent_role="agent_team"),
        )

    monkeypatch.setattr(runtime, "create_agent_team_client", create_client)
    async with protocol_fixtures.endpoint() as server:
        plugins = {
            "version": 1,
            "mcp": {
                "servers": [
                    {
                        "id": "fixture",
                        "url": server["url"],
                        "timeout_seconds": 3,
                        "headers": {"Authorization": "Bearer socket-secret"},
                        "tools": {"echo": {"read_only": True}},
                    }
                ]
            },
            "mcp_repository_scopes": {"fixture": ["o/r"]},
            "hooks": [
                {
                    "id": "verify",
                    "event": "before_finish",
                    "argv": [
                        "python3",
                        "-c",
                        "from pathlib import Path; assert Path('verified.txt').read_text() == 'ok'",
                    ],
                }
            ],
        }
        with Session(engine) as db:
            for key, value in {
                "agent_team_harness_plugins": json.dumps(plugins),
                "agent_team_permission_profile": "autonomous",
                "agent_team_network_policy": "full_access",
                "agent_team_skills_enabled": "true",
            }.items():
                db.add(AppConfig(key_name=key, key_value=value))
            db.commit()
        loop = IterationLoopService(
            workspace,
            service,
            task_id=1,
            checkpoint=checkpoint,
            execution_runner=LocalExecutionRunner(workspace, service),
        )
        result = await asyncio.wait_for(
            loop.run("Implement task", "Inspect, implement and verify."), 30
        )
        assert result.success, result
        assert (workspace / "verified.txt").read_text() == "ok"
        assert not (workspace / "forbidden").exists()
        assert sorted(server["calls"]) == ["child evidence", "main evidence"]
        with Session(engine) as db:
            sessions = list(db.scalars(select(models.AgentTeamSession)))
            assert len(sessions) == 2
            assert all(s.status == "completed" for s in sessions)
            assert child_id in {s.id for s in sessions}
            main_id = next(s.id for s in sessions if s.role_name == "agent")
            receipts = list(db.scalars(select(models.AgentTeamUsage)))
            assert len(receipts) == len(calls) == 12
            task = db.get(models.AgentTeamTask, 1)
            assert (task.prompt_tokens, task.completion_tokens) == (84, 36)
        history = await checkpoint.load_messages(main_id)
        assert "socket-secret" not in json.dumps(history)
        events = [m.get("metadata", {}).get("harness_event", {}) for m in history]
        assert any(e.get("kind") == "hook" and e["status"] == "failed" for e in events)
        assert any(e.get("event") == "after_finish" for e in events)
        # Reconciliation of an already durable finish needs no provider or hook
        # side effect; receipts and child identity remain unchanged.
        restored = runtime.FullStackExpertAgent(
            workspace,
            service,
            checkpoint,
            main_id,
            initial_messages=history,
            execution_runner=LocalExecutionRunner(workspace, service),
        )
        resumed = await restored.execute(
            "Implement task", "Inspect, implement and verify."
        )
        assert resumed.success and resumed.summary == "verified"
        assert len(calls) == 12
