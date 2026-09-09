"""_ensure_gitignore 写入路径测试 / gitignore append write-path test."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.services.agent_team import pr_service as pr_service_module
from backend.services.agent_team.pr_service import AgentTeamPRService


class _FakeGitRunner:
    def __init__(self, workspace: Path):
        self.workspace = workspace


@pytest.mark.asyncio
async def test_ensure_gitignore_appends_missing_rules(tmp_path, monkeypatch):
    called: list[Path] = []

    def spy(path, data):
        called.append(Path(path))
        Path(path).write_bytes(data)

    monkeypatch.setattr(pr_service_module, "write_workspace_bytes", spy)
    # 只测 _ensure_gitignore，绕开重量级构造依赖 / bypass heavy __init__
    service = AgentTeamPRService.__new__(AgentTeamPRService)

    await service._ensure_gitignore(_FakeGitRunner(tmp_path))

    gitignore = tmp_path / ".gitignore"
    content = gitignore.read_text(encoding="utf-8")
    assert called == [gitignore]
    for rule in (".venv/", "__pycache__/", "*.pyc", ".pytest_cache/", ".mypy_cache/", "node_modules/"):
        assert rule in content


@pytest.mark.asyncio
async def test_ensure_gitignore_skips_existing_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pr_service_module, "write_workspace_bytes",
        lambda path, data: (_ for _ in ()).throw(AssertionError("不应重复写入已有规则")),
    )
    (tmp_path / ".gitignore").write_text(
        ".venv/\n__pycache__/\n*.pyc\n.pytest_cache/\n.mypy_cache/\nnode_modules/\n",
        encoding="utf-8",
    )
    service = AgentTeamPRService.__new__(AgentTeamPRService)

    await service._ensure_gitignore(_FakeGitRunner(tmp_path))  # 不应触发写入
