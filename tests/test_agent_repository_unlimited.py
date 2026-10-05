"""Quota removal and review regression cases."""

from pathlib import PurePosixPath
from unittest.mock import AsyncMock

import pytest

from backend.services.agent_team import fullstack_expert as runtime
from backend.services.agent_team import repository_context as repository_module
from backend.services.agent_team.repository_context import (
    RepositoryContext,
    RepositoryContextError,
)
from backend.utils.message_utils import tool_call_to_dict
from tests.test_agent_repository_context import call, context, put

pytest_plugins = ["tests.test_agent_harness_checkpoint"]


def repository(root):
    return RepositoryContext(root)


def test_repository_quota_interfaces_are_removed():
    assert not hasattr(repository_module, "RepositoryLimits")
    assert not hasattr(repository_module, "get_repository_limits")


def test_large_repository_data_has_no_file_render_metadata_or_skill_count_cap(tmp_path):
    body = "repository convention\n" * 20000
    put(tmp_path, "AGENTS.md", body)
    repo = repository(tmp_path)
    docs = repo.instructions_for()
    assert len(docs) == 1 and docs[0].content == body
    assert len(repo.render(docs).encode()) > 262144
    description = "metadata" * 2000
    for i in range(65):
        put(
            tmp_path,
            f".agents/skills/skill-{i}/SKILL.md",
            f"---\nname: skill-{i}\ndescription: {description}\n---\nBODY",
        )
    index = repo.discover_skills()
    assert len(index) == 65 and all(
        entry["description"] == description for entry in index.values()
    )


def test_scanning_does_not_stop_at_previous_entry_count(tmp_path):
    for i in range(8200):
        (tmp_path / f"file-{i:04}.txt").touch()
    assert len(repository(tmp_path).list_files(".")) == 8200


def test_no_frontmatter_marker_does_not_read_plaintext_body_line(tmp_path, monkeypatch):
    put(tmp_path, ".agents/skills/plain/SKILL.md", "ordinary body" * 100000)
    repo = repository(tmp_path)
    original = repository_module.os.fdopen
    reads = []

    class Probe:
        def __init__(self, descriptor, mode):
            self.stream = original(descriptor, mode)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def read(self, amount=-1):
            reads.append(amount)
            assert amount >= 0
            return self.stream.read(amount)

        def readline(self, *args):
            pytest.fail("no-header probe read the body line")

    monkeypatch.setattr(repository_module.os, "fdopen", Probe)
    with pytest.raises(RepositoryContextError, match="frontmatter"):
        repo._metadata(PurePosixPath(".agents/skills/plain/SKILL.md"))
    assert reads == [3]


@pytest.mark.parametrize("whole", [False, True])
def test_sakura_global_instruction_is_read_once_before_dedup(
    tmp_path, monkeypatch, whole
):
    put(tmp_path, ".sakura/AGENTS.md", "GLOBAL")
    repo = repository(tmp_path)
    original = repo._open_file
    paths = []

    def read(path, **kwargs):
        paths.append(str(path))
        return original(path, **kwargs)

    monkeypatch.setattr(repo, "_open_file", read)
    docs = repo.snapshot((".sakura/x.py",), whole=whole)
    assert paths.count(".sakura/AGENTS.md") == 1
    assert [doc.path for doc in docs] == [".sakura/AGENTS.md"]
    assert docs[0].scope == "."


@pytest.mark.asyncio
async def test_successful_listing_persists_narrowed_ceiling_on_resume(
    tmp_path, persistence, monkeypatch
):
    monkeypatch.setattr(
        repository_module, "get_dynamic_config", AsyncMock(return_value=True)
    )
    service, _, _ = persistence
    session = await service.create_session(1, "agent")
    path = put(
        tmp_path,
        ".agents/skills/docs/SKILL.md",
        "---\nname: docs\nallowed_tools: [run_command]\n---\nBODY",
    )
    ctx = context(tmp_path, repository(tmp_path))
    ctx.extra["skills_index"] = ctx.repository_context.discover_skills()
    agent = runtime.FullStackExpertAgent(
        tmp_path, ctx.workspace_service, checkpoint=service, session_id=session.id
    )
    load = call("use_skill", "load", slug="docs")
    await agent._append_message(
        {"role": "assistant", "tool_calls": [tool_call_to_dict(load)]}
    )
    await agent._execute_tool_calls([load], ctx, 1)
    path.write_text(path.read_text().replace("[run_command]", '["Bash(git status:*)"]'))
    ctx.extra["admin_skills_index"] = {}
    await agent._refresh_skills(ctx)
    listing = call("use_skill", "list", slug="docs", list_files=True)
    await agent._append_message(
        {"role": "assistant", "tool_calls": [tool_call_to_dict(listing)]}
    )
    await agent._execute_tool_calls([listing], ctx, 2)
    history = await service.load_messages(session.id)
    assert "files" in history[-1]["content"]
    path.write_text(path.read_text().replace('["Bash(git status:*)"]', "[run_command]"))
    resume = context(tmp_path, repository(tmp_path))
    resume.extra["skills_index"] = resume.repository_context.discover_skills()
    restored = runtime.FullStackExpertAgent(
        tmp_path, ctx.workspace_service, initial_messages=history
    )
    restored._restore_skill_workflows(resume)
    assert not resume.allows_skill_tool("run_command", {"command": "git commit -m x"})
    assert resume.allows_skill_tool("run_command", {"command": "git status --short"})
