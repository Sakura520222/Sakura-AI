"""SubmitReview 工具 - 审查角色提交审查结果

终止工具，调用后审查 Agent 循环结束。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult


class SubmitReviewTool(BaseTool):
    """审查角色提交审查结果。"""

    name = "submit_review"

    _schema = {
        "type": "function",
        "function": {
            "name": "submit_review",
            "description": "提交审查结果。在完成所有文件审查后调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "verdict": {
                        "type": "string",
                        "enum": ["pass", "needs_improvement", "reject"],
                        "description": "审查结论",
                    },
                    "score": {
                        "type": "integer",
                        "description": "评分 1-10，>=7 为通过",
                    },
                    "summary": {
                        "type": "string",
                        "description": "审查总结",
                    },
                    "findings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "severity": {
                                    "type": "string",
                                    "enum": ["critical", "major", "minor", "suggestion"],
                                },
                                "file": {"type": "string"},
                                "message": {"type": "string"},
                                "suggestion": {"type": "string"},
                            },
                            "required": ["severity", "file", "message"],
                        },
                        "description": "审查发现列表",
                    },
                    "improvement_suggestions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "改进建议",
                    },
                },
                "required": ["verdict", "score", "summary"],
            },
        },
    }

    def is_read_only(self) -> bool:
        return True

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        verdict = args.get("verdict", "reject")
        score = int(args.get("score", 0))
        summary = args.get("summary", "")
        logger.info("SubmitReviewTool: verdict={}, score={}", verdict, score)

        # 防御性类型检查：AI 提供商可能忽略 schema 类型约束
        raw_findings = args.get("findings", [])
        findings = raw_findings if isinstance(raw_findings, list) else []

        raw_suggestions = args.get("improvement_suggestions", [])
        improvement_suggestions = (
            raw_suggestions if isinstance(raw_suggestions, list) else []
        )

        return ToolResult(
            success=True,
            output={
                "_terminal": True,
                "verdict": verdict,
                "score": score,
                "summary": summary,
                "findings": findings,
                "improvement_suggestions": improvement_suggestions,
            },
        )
