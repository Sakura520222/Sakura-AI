"""FinishTask 工具 - 全栈专家标记任务完成

终止工具，调用后 Agent 循环结束。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from backend.services.agent_team.tools.base import (
    BaseTool,
    ToolContext,
    ToolMetadata,
    ToolResult,
)


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

    def runtime_metadata(self) -> ToolMetadata:
        return ToolMetadata(read_only=True, terminal=True)

    def validate_input(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        if not isinstance(args.get("summary"), str) or not args["summary"].strip():
            return "summary must be a non-empty string"
        risk_level = args.get("risk_level", "medium")
        if not isinstance(risk_level, str) or risk_level not in {
            "low",
            "medium",
            "high",
        }:
            return "risk_level must be low, medium or high"
        files = args.get("modified_files", [])
        if not isinstance(files, list) or any(
            not isinstance(path, str) for path in files
        ):
            return "modified_files must be a list of paths"
        if not isinstance(args.get("test_result", ""), str):
            return "test_result must be a string"
        return None

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
