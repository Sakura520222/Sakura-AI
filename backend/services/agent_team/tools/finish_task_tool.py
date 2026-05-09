"""FinishTask 工具 - 全栈专家标记任务完成

终止工具，调用后 Agent 循环结束。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult


class FinishTaskTool(BaseTool):
    """全栈专家标记任务完成。"""

    name = "finish_task"

    _schema = {
        "type": "function",
        "function": {
            "name": "finish_task",
            "description": (
                "标记任务完成。当你认为所有必要的代码修改已完成且测试通过时调用此工具。"
                "提供修改总结和风险评估。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "本次修改的简要总结",
                    },
                    "modified_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "已修改的文件路径列表",
                    },
                    "risk_level": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "修改的风险评估",
                    },
                    "test_result": {
                        "type": "string",
                        "description": "测试执行结果摘要",
                    },
                },
                "required": ["summary"],
            },
        },
    }

    def is_read_only(self) -> bool:
        return True

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        summary = args.get("summary", "")
        logger.info("FinishTaskTool: {}", summary[:100])

        return ToolResult(
            success=True,
            output={
                "_terminal": True,
                "summary": summary,
                "modified_files": args.get("modified_files", []),
                "risk_level": args.get("risk_level", "medium"),
                "test_result": args.get("test_result", ""),
            },
        )
