"""工具结构化错误测试 / Structured tool error-code tests."""

from __future__ import annotations

import json
import os

import pytest

from backend.services.agent_team.tools.base import (
    ToolContext,
    ToolExecutor,
    ToolResult,
)
from backend.services.agent_team.tools.errors import (
    WORKSPACE_WRITE_PERMISSION_DENIED,
    ToolExecutionError,
)
from backend.services.agent_team.tools.file_utils import write_workspace_bytes
from backend.services.agent_team.tools.write_tool import WriteTool
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService
from backend.utils.message_utils import serialize_tool_result


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root 绕过 DAC，无法用 chmod 复现 EACCES",
)
def test_write_helper_maps_eacces_to_structured_error(tmp_path):
    target = tmp_path / "locked.py"
    target.write_text("x", encoding="utf-8")
    target.chmod(0o444)

    with pytest.raises(ToolExecutionError) as excinfo:
        write_workspace_bytes(target, b"y")

    assert excinfo.value.error_code == WORKSPACE_WRITE_PERMISSION_DENIED


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root 绕过 DAC，无法用 chmod 复现 EACCES",
)
@pytest.mark.asyncio
async def test_executor_sets_error_code_on_tool_result(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    locked = workspace / "locked.py"
    locked.write_text("x", encoding="utf-8")
    locked.chmod(0o444)
    ctx = ToolContext(
        workspace=str(workspace),
        workspace_service=AgentTeamWorkspaceService(base_dir=tmp_path),
    )

    result = await ToolExecutor([WriteTool()]).execute_raw(
        "write_file", {"file_path": "locked.py", "content": "y"}, ctx
    )

    assert result.success is False
    assert result.error_code == WORKSPACE_WRITE_PERMISSION_DENIED
    assert "文件系统拒绝写入" in result.error


def test_serialize_tool_result_includes_error_code():
    result = ToolResult(
        success=False,
        error="boom",
        error_code=WORKSPACE_WRITE_PERMISSION_DENIED,
    )

    payload = json.loads(serialize_tool_result(result))

    assert payload["error"] == "boom"
    assert payload["error_code"] == WORKSPACE_WRITE_PERMISSION_DENIED


def test_serialize_tool_result_without_error_code_unchanged():
    payload = json.loads(serialize_tool_result(ToolResult(success=False, error="x")))
    assert payload == {"error": "x"}
