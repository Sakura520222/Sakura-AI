"""list_directory 剪枝与点文件策略测试 / pruning and dotfile policy tests."""

from __future__ import annotations

import pytest

from backend.services.agent_team.tools.base import ToolContext
from backend.services.agent_team.tools.list_directory_tool import ListDirectoryTool
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


@pytest.fixture
def ctx(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".venv" / "sandbox" / "lib").mkdir(parents=True)
    (ws / ".venv" / "sandbox" / "lib" / "noise.txt").write_text("x", encoding="utf-8")
    (ws / ".git" / "objects").mkdir(parents=True)
    (ws / ".sakura" / "memory").mkdir(parents=True)
    (ws / ".github" / "workflows").mkdir(parents=True)
    (ws / "src").mkdir()
    (ws / "src" / "app.py").write_text("print()", encoding="utf-8")
    (ws / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (ws / ".gitattributes").write_text("* text=auto\n", encoding="utf-8")
    (ws / "main.py").write_text("print()", encoding="utf-8")
    return ToolContext(
        workspace=str(ws),
        workspace_service=AgentTeamWorkspaceService(base_dir=tmp_path),
    )


@pytest.mark.asyncio
async def test_recursive_walk_prunes_venv_and_shows_dotfiles(ctx):
    result = await ListDirectoryTool().execute(
        {"directory": ".", "recursive": True}, ctx
    )

    paths = [entry["path"] for entry in result.output["entries"]]
    assert not any(path.startswith(".venv/") for path in paths)
    assert not any(path.startswith(".git/") for path in paths)
    assert not any(path.startswith(".sakura/") for path in paths)
    assert ".gitignore" in paths
    assert ".gitattributes" in paths
    assert ".github/workflows" in paths
    assert "main.py" in paths
    assert "src/app.py" in paths


@pytest.mark.asyncio
async def test_non_recursive_lists_dotfiles_but_prunes_dirs(ctx):
    result = await ListDirectoryTool().execute({"directory": "."}, ctx)

    paths = [entry["path"] for entry in result.output["entries"]]
    assert ".gitignore" in paths
    assert "main.py" in paths
    assert ".venv" not in paths
    assert ".git" not in paths
    assert ".sakura" not in paths


@pytest.mark.asyncio
async def test_recursive_truncation_keeps_real_files_visible(ctx):
    for index in range(250):
        (ctx.workspace_service.resolve_inside_workspace(ctx.workspace) / f"f{index:03}.py").write_text("", encoding="utf-8")

    result = await ListDirectoryTool().execute(
        {"directory": ".", "recursive": True}, ctx
    )

    assert result.output["truncated"] is True
    paths = [entry["path"] for entry in result.output["entries"]]
    assert len(paths) <= 200
    assert "main.py" in paths or ".gitignore" in paths
