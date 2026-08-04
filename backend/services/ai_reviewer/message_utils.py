"""AI 审查消息辅助函数。"""

from typing import Any

from backend.core.model_context import get_model_context_manager


def estimate_messages_tokens(
    messages: list[dict[str, Any]],
    model_context_mgr: Any | None = None,
) -> int:
    """估算模型消息列表的 token 用量。

    消息本身应为字典；其中 tool_calls 兼容 OpenAI SDK 对象和字典两种结构。
    """
    model_context_mgr = model_context_mgr or get_model_context_manager()
    total_tokens = 0

    for message in messages:
        content = message.get("content", "")
        if content:
            total_tokens += model_context_mgr.estimate_tokens(content)

        tool_calls = message.get("tool_calls")
        if tool_calls:
            for tool_call in tool_calls:
                function = getattr(tool_call, "function", None)
                if function is None and isinstance(tool_call, dict):
                    function = tool_call.get("function")

                if isinstance(function, dict):
                    function_name = function.get("name", "")
                    function_arguments = function.get("arguments", "")
                else:
                    function_name = getattr(function, "name", "")
                    function_arguments = getattr(function, "arguments", "")

                total_tokens += model_context_mgr.estimate_tokens(
                    function_name + str(function_arguments)
                )

    return total_tokens
