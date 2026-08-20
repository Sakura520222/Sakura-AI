"""Legacy professional-review Agent implementation.

The runtime no longer instantiates this class. Sakura PR Review is the
external review boundary; this module remains import-compatible for historical
result/checkpoint readers only.

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

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from backend.services.agent_team.ai_client import create_agent_team_client
from backend.services.agent_team.context_compressor import compress_agent_team_messages
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
from backend.services.ai_reviewer.token_tracker import TokenTracker
from backend.utils.message_utils import (
    get_missing_tool_calls,
    has_missing_tool_results,
    serialize_tool_result,
    tool_call_to_dict,
)

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
- `search_web`：搜索互联网获取文档和最佳实践
- `fetch_url`：抓取网页内容（用于深入阅读搜索结果中的链接）
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
    prompt_tokens: int = 0
    completion_tokens: int = 0


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
        user_guidance: str = "",
        cancel_check: Callable[[], bool] | None = None,
        guidance_callback: Callable[[], Any] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> ReviewResult:
        """执行审查，AI 自主调用工具直到提交审查。"""
        client, config = await create_agent_team_client()
        candidate = await client.resolve_role_primary_candidate(config.agent_role)
        context_window_tokens = (
            candidate.model.context_window_tokens if candidate else None
        )
        ctx = self._build_context(
            skills_context,
            github_repo=github_repo,
            sakura_ref=sakura_ref,
        )
        tool_schemas = get_tool_definitions("reviewer")
        # 工具循环不设轮次与时长上限：依赖模型自然停止（submit_review / 纯文本
        # 完成）与手动取消（cancel_check / cancel_event）。agent_team_timeout_seconds
        # 仅约束单次 AI 请求的 HTTP 超时，不约束整体轮数与时长。

        await self._ensure_system_checkpoint()
        if not self.restored_messages and not has_missing_tool_results(self.messages):
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
                        user_guidance=user_guidance,
                    ),
                }
            )

        tool_calls_count = 0
        token_tracker = TokenTracker()
        round_num = 0

        while True:
            round_num += 1
            if cancel_check and cancel_check():
                return ReviewResult(
                    passed=False,
                    verdict="cancelled",
                    score=0,
                    summary="任务已取消",
                    findings=[],
                    tool_calls_count=tool_calls_count,
                    prompt_tokens=token_tracker.prompt_tokens,
                    completion_tokens=token_tracker.completion_tokens,
                )
            logger.debug("专业审查工具调用第 {} 轮", round_num)

            pending_tool_calls = get_missing_tool_calls(self.messages)
            if pending_tool_calls:
                terminal_output = await self._execute_tool_calls(
                    pending_tool_calls,
                    ctx,
                    round_num,
                )
                tool_calls_count += len(pending_tool_calls)
                if terminal_output is not None:
                    return _review_result_from_terminal(
                        terminal_output,
                        tool_calls_count,
                        token_tracker,
                    )
                continue

            # 消费新的管理员指导
            if guidance_callback:
                try:
                    guidance = await guidance_callback()
                    if guidance:
                        await self._append_message(
                            {"role": "user", "content": guidance}
                        )
                        await self._append_message(
                            {
                                "role": "assistant",
                                "content": "收到管理员指导，我将按照要求调整审查方向。",
                            }
                        )
                except Exception:
                    pass

            model_messages = await compress_agent_team_messages(
                self.messages, candidate=candidate, token_tracker=token_tracker
            )
            await _publish_review_ai_request(
                round_num,
                task_id=self.checkpoint.task_id if self.checkpoint else None,
                session_id=self.session_id,
            )
            response = await client.call_with_retry(
                messages=model_messages,
                model="",
                tools=tool_schemas,
                tool_choice="auto",
                role="agent_team",
                cancel_event=cancel_event,
            )
            token_tracker.accumulate(response)
            token_tracker.log_context_usage(
                response,
                context_window_tokens,
                round_num,
            )

            if not response.choices:
                return ReviewResult(
                    verdict="reject",
                    score=0,
                    summary="AI 返回空响应",
                    tool_calls_count=tool_calls_count,
                    prompt_tokens=token_tracker.prompt_tokens,
                    completion_tokens=token_tracker.completion_tokens,
                )

            choice = response.choices[0]
            message = choice.message

            # 构建助手消息
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if message.content:
                assistant_msg["content"] = message.content
            if message.tool_calls:
                assistant_msg["tool_calls"] = [
                    tool_call_to_dict(tc) for tc in message.tool_calls
                ]
            await self._append_message(assistant_msg)

            if not message.tool_calls:
                return ReviewResult(
                    verdict="reject",
                    score=0,
                    summary=message.content or "审查未提交结果",
                    tool_calls_count=tool_calls_count,
                    prompt_tokens=token_tracker.prompt_tokens,
                    completion_tokens=token_tracker.completion_tokens,
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
                    prompt_tokens=token_tracker.prompt_tokens,
                    completion_tokens=token_tracker.completion_tokens,
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
                await self.checkpoint.mark_tool_call_running(
                    self.session_id, tool_call.id
                )
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
                    "content": serialize_tool_result(result),
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
        user_guidance: str = "",
    ) -> str:
        parts = [f"## 任务\n标题: {task_title}\n描述: {task_summary}\n"]
        if fullstack_summary:
            parts.append(f"\n## 全栈专家修改总结\n{fullstack_summary}\n")
        if diff_summary:
            parts.append(
                f"\n## Diff 摘要（所有修改的累积）\n```\n{diff_summary}\n```\n"
            )
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
        if user_guidance:
            parts.append(f"\n{user_guidance}\n")
        return "\n".join(parts)


def _review_result_from_terminal(
    terminal_output: dict[str, Any],
    tool_calls_count: int,
    token_tracker: TokenTracker | None = None,
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
        prompt_tokens=token_tracker.prompt_tokens if token_tracker else 0,
        completion_tokens=token_tracker.completion_tokens if token_tracker else 0,
    )


async def _publish_review_ai_request(
    round_num: int,
    task_id: int | None = None,
    session_id: int | None = None,
) -> None:
    """发布审查 AI 请求 SSE 事件（延迟导入避免循环依赖）。"""
    try:
        from backend.webui.sse import publish_event

        payload: dict[str, Any] = {
            "role": "reviewer",
            "round_num": round_num,
        }
        if task_id is not None:
            payload["task_id"] = task_id
        if session_id is not None:
            payload["session_id"] = session_id
        await publish_event("agent:ai_request", payload)
    except Exception:
        pass
