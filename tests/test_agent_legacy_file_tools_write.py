"""旧版 AgentTeamFileTools 写入路径切换测试 / Legacy file tools write-path test."""

from __future__ import annotations

from pathlib import Path

from backend.services.agent_team import file_tools as legacy_file_tools
from backend.services.agent_team.file_tools import AgentTeamFileTools
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


def _make_tools(tmp_path: Path) -> AgentTeamFileTools:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return AgentTeamFileTools(workspace, AgentTeamWorkspaceService(base_dir=tmp_path))


def test_write_file_routes_through_workspace_writer(tmp_path, monkeypatch):
    calls: list[bytes] = []

    def spy(path, data):
        calls.append(data)
        Path(path).write_bytes(data)

    monkeypatch.setattr(legacy_file_tools, "write_workspace_bytes", spy)
    tools = _make_tools(tmp_path)

    result = tools.write_file("main.py", "content")

    assert calls == [b"content"]
    assert (tools.workspace / "main.py").read_bytes() == b"content"
    assert result.created is True


def test_edit_file_routes_through_workspace_writer(tmp_path, monkeypatch):
    called: list[str] = []

    def spy(path, data):
        called.append(str(path))
        Path(path).write_bytes(data)

    tools = _make_tools(tmp_path)
    # 准备数据：spy 尚未安装，seed 写入不会被捕获。
    # / Seed the file before installing the spy so only the edit call is captured.
    tools.write_file("main.py", "hello world")

    monkeypatch.setattr(legacy_file_tools, "write_workspace_bytes", spy)
    result = tools.edit_file("main.py", "hello", "hi")

    assert len(called) == 1
    assert result.replacements == 1
    assert (tools.workspace / "main.py").read_text(encoding="utf-8") == "hi world"
