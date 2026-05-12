"""PR diff 精简审查辅助函数。"""

from typing import Any, Dict, List, Optional

from backend.core.config import get_settings
from backend.core.model_context import get_model_context_manager
from backend.services.ai_reviewer.constants import (
    COMPACT_TOOLS,
    TOOL_NAME_TO_DEFINITION,
)
from backend.services.ai_reviewer.message_utils import estimate_messages_tokens
from backend.services.ai_reviewer.tools import DiffToolHandler, ToolHandler


def extend_with_compact_tools(
    enabled_tools: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """为精简模式追加 PR diff 工具定义"""
    compact_enabled_tools = list(enabled_tools)
    enabled_names = {
        tool.get("function", {}).get("name")
        for tool in compact_enabled_tools
        if isinstance(tool, dict)
    }

    for tool_name in COMPACT_TOOLS:
        if tool_name in enabled_names:
            continue
        tool_def = TOOL_NAME_TO_DEFINITION.get(tool_name)
        if tool_def:
            compact_enabled_tools.append(tool_def)
            enabled_names.add(tool_name)

    return compact_enabled_tools


def should_use_compact_prompt(
    messages: List[Dict[str, Any]],
    context: Dict[str, Any],
    compression_threshold: Optional[float] = None,
    model_context_mgr: Optional[Any] = None,
) -> tuple[bool, int, int]:
    """判断初始 prompt 是否应主动切换到 PR diff 工具精简模式"""
    settings = get_settings()
    model_context_mgr = model_context_mgr or get_model_context_manager()
    threshold_ratio = (
        compression_threshold
        if compression_threshold is not None
        else settings.context_compression_threshold
    )
    current_tokens = estimate_messages_tokens(messages, model_context_mgr)
    safe_context = model_context_mgr.calculate_safe_context(
        settings.openai_model, settings.context_safety_threshold
    )
    threshold_tokens = int(safe_context * threshold_ratio)
    should_compact = (
        bool(context.get("files"))
        and threshold_tokens > 0
        and current_tokens > threshold_tokens
    )
    return should_compact, current_tokens, threshold_tokens


def build_tool_handler_with_diff(
    tool_handler: ToolHandler,
    diff_tool: DiffToolHandler,
) -> ToolHandler:
    """基于现有工具处理器创建启用 PR diff 工具的临时处理器"""
    return tool_handler.with_diff_tool(diff_tool)
