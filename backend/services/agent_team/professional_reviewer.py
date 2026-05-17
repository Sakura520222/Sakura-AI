"""Agent 专家团队 - 专业审查角色（工具调用模式）

通过 function calling 让 AI 自主调用工具审查代码：
- read_file: 读取修改后的文件
- list_directory: 浏览目录结构
- glob: 按模式查找文件
- search_in_files: 搜索关联代码
- run_command: 运行测试或检查
- use_skill: 按需读取已启用 Skill 的完整说明
- submit_review: 提交审查结果

AI 自主决定审查哪些文件、运行什么检查，完成后调用 submit_review。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from loguru import logger

from backend.core.config import DYNAMIC_CONFIG_RANGES, get_dynamic_config, get_settings
from backend.services.agent_team.ai_client import create_agent_team_client
from backend.services.agent_team.context_compressor import AgentTeamContextCompressor
from backend.services.agent_team.conversation_checkpoint import (
    ConversationCheckpointService,
)
from backend.services.agent_team.tools.base import ToolContext, ToolResult
from backend.services.agent_team.tools.file_state import ReadFileState
from backend.services.agent_team.tools.registry import (
    create_executor,
    get_tool_definitions,
)
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService

REVIEWER_SYSTEM_PROMPT = """你是 Sakura Agent 专家团队的专业代码审查员。
你审查全栈专家 Agent 所做的代码修改，确保质量、正确性和安全性。
你可以使用工具检查代码，但不能修改代码。你的审查结果决定代码是否可以提交。

## 审查流程（按步骤执行）

### 步骤 1：理解变更范围
- 阅读任务描述和全栈专家的修改总结
- 使用 `check_changes`（summary 模式）查看所有修改的文件和变更统计
- 理解修改的目的和背景

### 步骤 2：逐文件审查
- 对每个修改的文件使用 `read_file` 阅读完整内容
- 不仅看修改的行，也要检查周围的上下文代码
- 使用 `search_in_files` 检查修改的代码在其他地方是否也被引用
- 检查是否遗漏了必要的修改（导入、调用点、配置、测试）

### 步骤 3：运行验证
- 使用 `run_command` 运行代码检查（如 ruff check）
- 使用 `run_command` 运行测试（如 pytest -q）
- 验证项目约定是否被遵守

### 步骤 4：提交审查
- 调用 `submit_review` 提交结构化审查结果

## 审查维度（按优先级）

1. **正确性** (权重 30%)：代码逻辑是否正确？是否解决了目标问题？是否有边界情况？
2. **安全性** (权重 25%)：SQL 注入、命令注入、路径遍历、敏感数据泄露、权限问题
3. **完整性** (权重 20%)：所有必要的导入是否存在？调用点是否已更新？配置/测试是否已处理？
4. **代码质量** (权重 15%)：命名、结构、可读性、错误处理、与项目风格一致
5. **风险** (权重 10%)：修改范围是否合理？是否会引入回归？性能影响？

## 严重性定义
- `critical`：安全漏洞、数据丢失风险、功能中断。必须修复。
- `major`：逻辑错误、缺失的错误处理、API 契约违反。应当修复。
- `minor`：代码风格、命名、可读性问题。可选修复。
- `suggestion`：改进建议、替代方案。仅供参考。

## 评分标准
- 9-10：优秀。仅有微小的风格建议。
- 7-8：良好。有一些小问题，但可以安全合并。
- 5-6：可接受。存在问题需要修复，但方案整体可行。
- 3-4：差。存在严重问题或缺失关键部分。
- 1-2：不可接受。根本性缺陷，需要完全重做。

## 判定标准
- `pass`：分数 >= 7，无 critical 发现，major 发现极少
- `needs_improvement`：分数 4-6，或存在 major 发现但方案整体可行
- `reject`：分数 < 4，或存在 critical 发现，或方案根本性问题

## Finding 格式要求
每个 finding 的 `suggestion` 必须具体可操作：
- 好："将第 42 行的 `except Exception:` 改为 `except (ValueError, KeyError) as e:`"
- 好："在 routes.py 第 15 行后添加 `from backend.models import User` 导入"
- 差："改进错误处理"
- 差："考虑边界情况"

## 工具使用
- `read_file`：读取文件内容（支持行范围）
- `list_directory`：浏览目录结构
- `glob`：按文件名模式查找
- `search_in_files`：搜索代码内容
- `check_changes`：查看工作区累积变更（summary=统计，full=完整 diff）
- `run_command`：运行测试或代码检查
- `use_skill`：读取相关 Skill 指导
- `submit_review`：提交审查结果

## 重要规则
- 审查所有修改的文件，不要只看第一个
- 先用 `check_changes` 查看完整变更范围，再逐文件深入
- style 问题使用 suggestion 严重性，不要阻塞通过
- finding 中要具体：引用文件路径和行号，给出精确修复建议
- 如果修改很小且正确，不要人为压低分数
- 如果代码检查和测试通过，在评分中应予以认可
"""


@dataclass
class ReviewFinding:
    """审查发现。"""

    severity: str
    file: str
    message: str
    suggestion: str = ""


@dataclass
class ReviewResult:
    """专业审查结果。"""

    verdict: str  # pass / needs_improvement / reject
    score: int
    summary: str
    findings: list[ReviewFinding] = field(default_factory=list)
    improvement_suggestions: list[str] = field(default_factory=list)
    passed: bool = False
    tool_calls_count: int = 0


async def resolve_reviewer_max_tool_rounds() -> int:
    """读取专业审查工具调用轮次上限。"""
    settings = get_settings()
    fallback = settings.agent_team_reviewer_max_tool_rounds
    try:
        raw = await get_dynamic_config("agent_team_reviewer_max_tool_rounds")
        if raw is None:
            return fallback
        value = int(raw)
        min_value, max_value = DYNAMIC_CONFIG_RANGES["agent_team_reviewer_max_tool_rounds"]
        if min_value <= value <= max_value:
            return value
        raise ValueError(f"value {value} outside range {min_value}-{max_value}")
    except (TypeError, ValueError) as exc:
        logger.warning(
            "读取 agent_team_reviewer_max_tool_rounds 配置失败，使用默认值 {}: {}",
            fallback,
            exc,
        )
        return fallback
    except Exception as exc:
        logger.warning(
            "读取 agent_team_reviewer_max_tool_rounds 配置异常，使用默认值 {}: {}",
            fallback,
            exc,
        )
        return fallback


class ProfessionalReviewAgent:
    """专业审查 Agent - 通过工具调用自主审查代码。"""

    def __init__(
        self,
        workspace: str | Any,
        workspace_service: AgentTeamWorkspaceService | None = None,
        checkpoint: ConversationCheckpointService | None = None,
        session_id: int | None = None,
        initial_messages: list[dict[str, Any]] | None = None,
    ):
        self.workspace_service = workspace_service or AgentTeamWorkspaceService()
        self.workspace = self.workspace_service.resolve_inside_workspace(workspace)
        self.tool_executor = create_executor("reviewer")
        self.file_state = ReadFileState()
        self.checkpoint = checkpoint
        self.session_id = session_id
        self.restored_messages = initial_messages is not None
        self.messages: list[dict[str, Any]] = initial_messages or [
            {"role": "system", "content": REVIEWER_SYSTEM_PROMPT}
        ]

    async def _append_message(self, message: dict[str, Any]) -> int | None:
        self.messages.append(message)
        if self.checkpoint and self.session_id:
            return await self.checkpoint.append_message(self.session_id, message)
        return None

    async def _ensure_system_checkpoint(self) -> None:
        if not self.checkpoint or not self.session_id or not self.messages:
            return
        if len(self.messages) == 1 and self.messages[0].get("role") == "system":
            await self.checkpoint.append_message(self.session_id, self.messages[0])

    def _build_context(
        self,
        skills_context: dict[str, Any] | None = None,
        github_repo: Any | None = None,
        sakura_ref: str | None = None,
    ) -> ToolContext:
        extra: dict[str, Any] = {"file_state": self.file_state}
        if github_repo is not None:
            extra["github_repo"] = github_repo
        if sakura_ref is not None:
            extra["sakura_ref"] = sakura_ref
        if skills_context:
            extra.update(skills_context)
        return ToolContext(
            workspace=str(self.workspace),
            workspace_service=self.workspace_service,
            read_file_state={},
            extra=extra,
        )

    async def review(
        self,
        task_title: str,
        task_summary: str,
        modified_files: list[str],
        fullstack_summary: str = "",
        feedback_context: str = "",
        diff_summary: str = "",
        handoff_context: str = "",
        role_memory_context: str = "",
        skills_summary: str = "",
        skills_context: dict[str, Any] | None = None,
        github_repo: Any | None = None,
        sakura_ref: str | None = None,
    ) -> ReviewResult:
        """执行审查，AI 自主调用工具直到提交审查。"""
        client, config = await create_agent_team_client()
        ctx = self._build_context(
            skills_context,
            github_repo=github_repo,
            sakura_ref=sakura_ref,
        )
        tool_schemas = get_tool_definitions("reviewer", provider=config.provider)
        max_tool_rounds = await resolve_reviewer_max_tool_rounds()

        await self._ensure_system_checkpoint()
        if not self.restored_messages and not _has_missing_tool_results(self.messages):
            await self._append_message(
                {
                    "role": "user",
                    "content": self._build_review_message(
                        task_title=task_title,
                        task_summary=task_summary,
                        modified_files=modified_files,
                        fullstack_summary=fullstack_summary,
                        feedback_context=feedback_context,
                        diff_summary=diff_summary,
                        handoff_context=handoff_context,
                        role_memory_context=role_memory_context,
                        skills_summary=skills_summary,
                    ),
                }
            )

        tool_calls_count = 0

        for round_num in range(1, max_tool_rounds + 1):
            logger.debug("专业审查工具调用第 {} 轮", round_num)

            pending_tool_calls = _get_missing_tool_calls(self.messages)
            if pending_tool_calls:
                terminal_output = await self._execute_tool_calls(
                    pending_tool_calls,
                    ctx,
                    round_num,
                )
                tool_calls_count += len(pending_tool_calls)
                if terminal_output is not None:
                    return _review_result_from_terminal(terminal_output, tool_calls_count)
                continue

            model_messages = await AgentTeamContextCompressor(
                target_model=config.review_model,
                compressor_model=config.summary_model,
            ).build_model_messages(self.messages)
            response = await client.call_with_retry(
                messages=model_messages,
                model=config.review_model,
                temperature=max(config.temperature - 0.1, 0.0),
                max_tokens=config.max_tokens,
                timeout=config.timeout_seconds,
                tools=tool_schemas,
                tool_choice="auto",
            )

            if not response.choices:
                return ReviewResult(
                    verdict="reject",
                    score=0,
                    summary="AI 返回空响应",
                    tool_calls_count=tool_calls_count,
                )

            choice = response.choices[0]
            message = choice.message

            # 构建助手消息
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if message.content:
                assistant_msg["content"] = message.content
            if message.tool_calls:
                assistant_msg["tool_calls"] = [
                    _tc_to_dict(tc) for tc in message.tool_calls
                ]
            await self._append_message(assistant_msg)

            if not message.tool_calls:
                return ReviewResult(
                    verdict="reject",
                    score=0,
                    summary=message.content or "审查未提交结果",
                    tool_calls_count=tool_calls_count,
                )

            terminal_output = await self._execute_tool_calls(
                message.tool_calls,
                ctx,
                round_num,
            )
            tool_calls_count += len(message.tool_calls)

            if terminal_output is not None:
                verdict = terminal_output.get("verdict", "reject")
                score = int(terminal_output.get("score", 0))
                findings = []
                raw_findings = terminal_output.get("findings", [])
                if isinstance(raw_findings, list):
                    for f in raw_findings:
                        if not isinstance(f, dict):
                            continue
                        findings.append(
                            ReviewFinding(
                                severity=f.get("severity", "minor"),
                                file=f.get("file", ""),
                                message=f.get("message", ""),
                                suggestion=f.get("suggestion", ""),
                            )
                        )
                return ReviewResult(
                    verdict=verdict,
                    score=score,
                    summary=terminal_output.get("summary", ""),
                    findings=findings,
                    improvement_suggestions=terminal_output.get(
                        "improvement_suggestions", []
                    ),
                    passed=verdict == "pass" and score >= 7,
                    tool_calls_count=tool_calls_count,
                )

        return ReviewResult(
            verdict="reject",
            score=0,
            summary=f"达到最大审查轮次 ({max_tool_rounds})",
            tool_calls_count=tool_calls_count,
        )

    async def _execute_tool_calls(
        self,
        tool_calls: list[Any],
        ctx: ToolContext,
        round_num: int,
    ) -> dict[str, Any] | None:
        terminal_output: dict[str, Any] | None = None
        for tool_call in tool_calls:
            fn_name = tool_call.function.name
            logger.info("专业审查调用工具: {} (round={})", fn_name, round_num)

            if self.checkpoint and self.session_id:
                await self.checkpoint.mark_tool_call_running(self.session_id, tool_call.id)
            try:
                if terminal_output is None:
                    result = await self.tool_executor.execute_tool_call(tool_call, ctx)
                else:
                    result = ToolResult(
                        success=True,
                        output={
                            "skipped": True,
                            "reason": "terminal_tool_already_called",
                        },
                    )
            except Exception as exc:
                if self.checkpoint and self.session_id:
                    await self.checkpoint.mark_tool_call_failed(
                        self.session_id, tool_call.id, str(exc)
                    )
                raise
            result_message_id = await self._append_message(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": _serialize_tool_result(result),
                }
            )
            if self.checkpoint and self.session_id and result_message_id:
                await self.checkpoint.mark_tool_call_completed(
                    self.session_id, tool_call.id, result_message_id
                )

            if result.is_terminal:
                terminal_output = result.output
        return terminal_output

    def _build_review_message(
        self,
        task_title: str,
        task_summary: str,
        modified_files: list[str],
        fullstack_summary: str,
        feedback_context: str,
        diff_summary: str = "",
        handoff_context: str = "",
        role_memory_context: str = "",
        skills_summary: str = "",
    ) -> str:
        parts = [f"## 任务\n标题: {task_title}\n描述: {task_summary}\n"]
        if fullstack_summary:
            parts.append(f"\n## 全栈专家修改总结\n{fullstack_summary}\n")
        if diff_summary:
            parts.append(f"\n## Diff 摘要（所有修改的累积）\n```\n{diff_summary}\n```\n")
        if modified_files:
            files_list = "\n".join(f"- `{f}`" for f in modified_files)
            parts.append(f"\n## 已修改的文件\n{files_list}\n")
            parts.append("\n请逐一审查以上修改的文件，确认代码质量。\n")
        if feedback_context:
            parts.append(f"\n## 上下文\n{feedback_context}\n")
        if role_memory_context:
            parts.append(f"\n## 专业审查历史记忆\n{role_memory_context}\n")
        if handoff_context:
            parts.append(f"\n## 专家对话交接\n{handoff_context}\n")
        if skills_summary:
            parts.append(f"\n{skills_summary}\n")
        return "\n".join(parts)


def _tc_to_dict(tool_call: Any) -> dict:
    return {
        "id": tool_call.id,
        "type": "function",
        "function": {
            "name": tool_call.function.name,
            "arguments": tool_call.function.arguments,
        },
    }


def _tool_call_from_dict(data: dict[str, Any]) -> Any:
    function = data.get("function") or {}
    return SimpleNamespace(
        id=data.get("id", ""),
        function=SimpleNamespace(
            name=function.get("name", ""),
            arguments=function.get("arguments", ""),
        ),
    )


def _get_missing_tool_calls(messages: list[dict[str, Any]]) -> list[Any]:
    completed = {
        item.get("tool_call_id")
        for item in messages
        if item.get("role") == "tool" and item.get("tool_call_id")
    }
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls") or []
        missing = [
            _tool_call_from_dict(item)
            for item in tool_calls
            if item.get("id") not in completed
        ]
        if missing:
            return missing
    return []


def _has_missing_tool_results(messages: list[dict[str, Any]]) -> bool:
    return bool(_get_missing_tool_calls(messages))


def _review_result_from_terminal(
    terminal_output: dict[str, Any], tool_calls_count: int
) -> ReviewResult:
    verdict = terminal_output.get("verdict", "reject")
    score = int(terminal_output.get("score", 0))
    findings = []
    raw_findings = terminal_output.get("findings", [])
    if isinstance(raw_findings, list):
        for item in raw_findings:
            if not isinstance(item, dict):
                continue
            findings.append(
                ReviewFinding(
                    severity=item.get("severity", "minor"),
                    file=item.get("file", ""),
                    message=item.get("message", ""),
                    suggestion=item.get("suggestion", ""),
                )
            )
    return ReviewResult(
        verdict=verdict,
        score=score,
        summary=terminal_output.get("summary", ""),
        findings=findings,
        improvement_suggestions=terminal_output.get("improvement_suggestions", []),
        passed=verdict == "pass" and score >= 7,
        tool_calls_count=tool_calls_count,
    )


def _serialize_tool_result(result: ToolResult) -> str:
    if result.success:
        return json.dumps(result.output, ensure_ascii=False, default=str)
    return json.dumps({"error": result.error}, ensure_ascii=False)
