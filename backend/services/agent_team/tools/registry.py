"""工具注册表 - Agent 的唯一实现工具集。

历史版本把工具拆成 ``fullstack`` 和 ``reviewer`` 两套集合。Agent Team
现在只有一个实现 Agent；这里保留少量旧函数名作为导入兼容，但它们都指向
同一套实现工具，绝不会重新注册 ``submit_review`` 或创建 reviewer 工具集。
"""

from __future__ import annotations

from typing import Any

from backend.services.agent_team.tools.base import BaseTool, ToolExecutor
from backend.services.agent_team.tools.edit_tool import EditTool
from backend.services.agent_team.tools.fetch_url_tool import FetchUrlTool
from backend.services.agent_team.tools.finish_task_tool import FinishTaskTool
from backend.services.agent_team.tools.git_diff_tool import GitDiffTool
from backend.services.agent_team.tools.glob_tool import GlobTool
from backend.services.agent_team.tools.grep_tool import GrepTool
from backend.services.agent_team.tools.insert_lines_tool import InsertLinesTool
from backend.services.agent_team.tools.list_directory_tool import ListDirectoryTool
from backend.services.agent_team.tools.project_detect_tool import DetectProjectTool
from backend.services.agent_team.tools.read_tool import ReadTool
from backend.services.agent_team.tools.replace_lines_tool import ReplaceLinesTool
from backend.services.agent_team.tools.revert_file_tool import RevertFileTool
from backend.services.agent_team.tools.shell_tool import ShellTool
from backend.services.agent_team.tools.use_skill_tool import UseSkillTool
from backend.services.agent_team.tools.web_search_tool import WebSearchTool
from backend.services.agent_team.tools.write_tool import WriteTool

# ── 工具实例 ──────────────────────────────────────────

# Agent 实现角色唯一可用工具。
AGENT_TOOL_INSTANCES: list[BaseTool] = [
    ReadTool(),
    ListDirectoryTool(),
    GlobTool(),
    GrepTool(),
    UseSkillTool(),
    WriteTool(),
    EditTool(),
    ReplaceLinesTool(),
    InsertLinesTool(),
    ShellTool(),
    GitDiffTool(),
    DetectProjectTool(),
    RevertFileTool(),
    FinishTaskTool(),
    WebSearchTool(),
    FetchUrlTool(),
]

# Legacy name retained for callers that still import the old implementation list.
FULLSTACK_TOOL_INSTANCES = AGENT_TOOL_INSTANCES

# 按名称索引的工具注册表。Reviewer-specific submit_review is intentionally absent.
tool_registry: dict[str, BaseTool] = {tool.name: tool for tool in AGENT_TOOL_INSTANCES}


def get_agent_tools() -> list[BaseTool]:
    """获取 Agent 实现角色可用工具列表。"""
    return list(AGENT_TOOL_INSTANCES)


def get_fullstack_tools() -> list[BaseTool]:
    """兼容旧调用方，返回 Agent 实现工具列表。"""
    return get_agent_tools()


def get_reviewer_tools() -> list[BaseTool]:
    """兼容旧导入；Agent Team 不再有 reviewer 专属工具集。"""
    return get_agent_tools()


def _sanitize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """清理工具 schema，确保兼容各 AI 厂商。

    某些厂商（如智谱 GLM）对 function calling schema 有更严格的要求：
    - 不接受空 required 数组
    - 不接受 properties 中的 default 值
    """
    import copy

    schema = copy.deepcopy(schema)
    fn = schema.get("function", {})
    params = fn.get("parameters", {})

    # 移除空 required 数组
    if "required" in params and not params["required"]:
        del params["required"]

    # 移除 properties 中的 default 值
    for prop in params.get("properties", {}).values():
        prop.pop("default", None)

    return schema


def _glm_compatible_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """转换为 GLM OpenAI 兼容接口更稳定接受的工具 schema。

    GLM 的 OpenAI 兼容接口对 function calling JSON Schema 较严格，
    可选参数容易触发 1210 参数错误。这里将所有 properties 都放入
    required，并禁止额外属性；工具实现本身已有默认值兜底。
    """
    schema = _sanitize_schema(schema)
    params = schema.get("function", {}).get("parameters", {})
    properties = params.get("properties", {})
    if properties:
        params["required"] = list(properties.keys())
    params["additionalProperties"] = False
    for prop in properties.values():
        if prop.get("type") == "object":
            prop.setdefault("additionalProperties", False)
    return schema


def get_tool_definitions(
    role: str = "agent",
    provider: str | None = None,
) -> list[dict[str, Any]]:
    """获取 Agent 实现工具 schema 列表（用于 function calling）。

    Args:
        role: legacy role label, ignored for compatibility
        provider: AI 厂商 ID，用于应用厂商兼容转换
    """
    del role
    tools = AGENT_TOOL_INSTANCES
    if (provider or "").lower() in ("glm", "zai"):
        return [_glm_compatible_schema(t.get_schema()) for t in tools]
    return [_sanitize_schema(t.get_schema()) for t in tools]


def create_executor(role: str = "agent") -> ToolExecutor:
    """创建 Agent 实现工具执行器。

    ``role`` 仅为旧调用方保留，不再改变工具白名单。
    """
    del role
    return ToolExecutor(list(AGENT_TOOL_INSTANCES))
