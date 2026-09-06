"""detect_project 能力探测测试 / detect_project capability-probe tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.services.agent_team.tools.base import ToolContext
from backend.services.agent_team.tools.project_detect_tool import DetectProjectTool
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


def _ctx(tmp_path: Path, chdir_to: Path) -> ToolContext:
    """构造最小工具上下文，工作区根指向 chdir_to。"""
    (chdir_to / "pyproject.toml").write_text(
        "[project]\nname = 'x'\nversion = '0'\n", encoding="utf-8"
    )
    return ToolContext(
        workspace=str(chdir_to),
        workspace_service=AgentTeamWorkspaceService(base_dir=tmp_path),
    )


@pytest.mark.asyncio
async def test_no_lint_command_when_ruff_missing(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = _ctx(tmp_path, ws)

    result = await DetectProjectTool().execute({}, ctx)

    assert result.success is True
    assert "lint_command" not in result.output


@pytest.mark.asyncio
async def test_lint_command_when_ruff_in_dependency_venv(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = _ctx(tmp_path, ws)
    ruff = ws / ".venv" / "sandbox" / "bin" / "ruff"
    ruff.parent.mkdir(parents=True)
    ruff.write_text("#!/bin/sh\n", encoding="utf-8")

    result = await DetectProjectTool().execute({}, ctx)

    assert result.output.get("lint_command") == "ruff check"


@pytest.mark.asyncio
async def test_local_venv_ruff_also_detected(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = _ctx(tmp_path, ws)
    ruff = ws / ".venv" / "local" / "bin" / "ruff"
    ruff.parent.mkdir(parents=True)
    ruff.write_text("#!/bin/sh\n", encoding="utf-8")

    result = await DetectProjectTool().execute({}, ctx)

    assert result.output.get("lint_command") == "ruff check"
