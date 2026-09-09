"""write_file 新建文件行尾策略测试 / new-file EOL policy tests."""

from __future__ import annotations

import pytest

from backend.services.agent_team.tools.base import ToolContext
from backend.services.agent_team.tools.write_tool import WriteTool
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


def _ctx(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws, ToolContext(
        workspace=str(ws),
        workspace_service=AgentTeamWorkspaceService(base_dir=tmp_path),
    )


@pytest.mark.asyncio
async def test_new_file_defaults_to_lf(tmp_path):
    ws, ctx = _ctx(tmp_path)

    result = await WriteTool().execute(
        {"file_path": "a.txt", "content": "one\ntwo\n"}, ctx
    )

    assert result.success is True
    assert (ws / "a.txt").read_bytes() == b"one\ntwo\n"


@pytest.mark.asyncio
async def test_new_file_honors_gitattributes_crlf(tmp_path):
    ws, ctx = _ctx(tmp_path)
    (ws / ".gitattributes").write_text("* text=auto eol=crlf\n", encoding="utf-8")

    result = await WriteTool().execute(
        {"file_path": "b.txt", "content": "one\ntwo\n"}, ctx
    )

    assert result.success is True
    assert (ws / "b.txt").read_bytes() == b"one\r\ntwo\r\n"
