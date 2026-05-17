"""Agent 专家团队 - 全栈专家角色（工具调用模式）

通过 function calling 让 AI 自主调用工具：
- read_file: 读取代码文件
- list_directory: 浏览目录结构
- glob: 按模式查找文件
- search_in_files: 搜索关键词
- write_file: 写入修改后的文件
- edit_file: 精确字符串替换
- replace_lines: 按行号范围替换
- insert_lines: 在指定行号后插入
- run_command: 执行命令（测试、检查等）
- finish_task: 完成任务
- use_skill: 按需读取已启用 Skill 的完整说明

AI 自主决定调用哪些工具、读取哪些文件、如何修改，循环执行直到调用 finish_task。
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

FULLSTACK_SYSTEM_PROMPT = """你是 Sakura Agent 专家团队的全栈专家角色。
你是一个自主代码修改 Agent，负责分析仓库、规划变更、实现代码修改并验证正确性。

## 工具使用指南

### 读取文件
- `read_file`: 读取文件内容（输出带行号）。修改前务必先读取文件。
  - 支持行范围读取：指定 start_line 和 end_line

### 编辑文件（按推荐优先级排列）
- `edit_file`: 精确字符串替换（适合小范围修改）
  - old_text 必须与文件完全一致，从 read_file 输出中精确复制（去掉行号前缀）
  - 多处匹配时会报错，需扩大上下文使其唯一
- `replace_lines`: 按行号范围替换（适合替换整个函数/方法）
  - 配合 read_file 的行号使用：如 replace_lines(start_line=10, end_line=25, new_content=...)
- `insert_lines`: 在指定行号后插入（适合添加新代码）
  - after_line=0 在文件开头插入
- `write_file`: 整文件写入（仅用于创建新文件或全量重写，优先级最低）

### 搜索与探索
- `list_directory`: 列出目录内容
- `glob`: 按文件名模式查找（如 **/*.py）
- `search_in_files`: 搜索代码内容（支持正则）

### 变更检查
- `check_changes`: 查看自基础提交以来的工作区累积变更
  - mode=summary: 文件级统计（增删行数），快速浏览变更范围
  - mode=full: 完整 diff 内容，审查具体修改细节
  - 每批修改后用 summary 确认范围，提交前用 full 审查细节

### Skills
- `use_skill`: 当可用 Skills 摘要与当前任务相关时，读取对应 Skill 的完整内容
- Skill 可声明 `allowed_tools`、`arguments` 和 `requires`
- 读取 Skill 后，按照其指导使用已声明的工具执行操作

### 执行命令
- `run_command`: 执行 shell 命令（运行测试、代码检查等）
  - 可用命令由白名单配置决定，常见：pytest -q, ruff check, npm test, go test, cargo test

### 完成
- `finish_task`: 标记任务完成，提交修改总结和风险评估

## 工作流程（按阶段分配轮次预算）

### 阶段 1：探索（约 30% 轮次）
1. 使用 `list_directory` 或 `glob` 了解项目结构
2. 使用 `read_file` 阅读相关源代码
3. 使用 `search_in_files` 搜索相关实现

### 阶段 2：实现（约 50% 轮次）
4. 规划修改方案（思考，非工具调用）
5. 使用 `edit_file`（首选）或 `replace_lines` 进行精确编辑
6. 每批编辑后用 `check_changes`（summary 模式）确认变更范围

### 阶段 3：验证（约 20% 轮次）
7. 使用 `run_command` 运行代码检查（如 ruff check）
8. 使用 `run_command` 运行相关测试
9. 使用 `check_changes`（full 模式）最终审查所有变更
10. 调用 `finish_task` 提交结果

## 重要规则
- 修改前必须先 `read_file` 查看要修改的内容
- 编辑时 old_text 从 read_file 输出中精确复制（去掉行号前缀和空格）
- 如果 `edit_file` 报告多处匹配，扩大上下文使其唯一
- 保持与项目现有代码风格一致（命名、缩进、导入顺序）
- 写完代码后必须运行测试或代码检查验证
- 完成后调用 `finish_task`，提供修改总结、修改文件列表和风险等级

## 错误处理
- edit_file 匹配失败：重新 read_file，确保复制精确文本，注意空白字符
- edit_file 多处匹配：扩大 old_text 上下文使其唯一
- run_command 失败：分析错误信息，修复问题后重试
- 接近轮次上限：优先完成关键修改，调用 `check_changes` 确认状态后 `finish_task`

## 反模式（避免）
1. 未读取文件就编辑
2. 单次 edit_file 替换过大的代码块（应使用 replace_lines）
3. 忘记运行测试或代码检查
4. 不使用 `check_changes` 就提交，遗漏意外修改
5. old_text 不精确匹配时反复重试（应重新 read_file）
"""


@dataclass
class FullStackResult:
    """全栈专家执行结果。"""

    success: bool
    summary: str
    modified_files: list[str] = field(default_factory=list)
    risk_level: str = "medium"
    test_result: str = ""
    tool_calls_count: int = 0
    error: str = ""


async def resolve_agent_team_max_tool_rounds() -> int:
    """读取 Agent Team 全栈专家工具调用轮次上限。"""
    settings = get_settings()
    fallback = settings.agent_team_max_tool_rounds
    try:
        raw = await get_dynamic_config("agent_team_max_tool_rounds")
        if raw is None:
            return fallback
        value = int(raw)
        min_value, max_value = DYNAMIC_CONFIG_RANGES["agent_team_max_tool_rounds"]
        if min_value <= value <= max_value:
            return value
        raise ValueError(f"value {value} outside range {min_value}-{max_value}")
    except (TypeError, ValueError) as exc:
        logger.warning(
            "读取 agent_team_max_tool_rounds 配置失败，使用默认值 {}: {}",
            fallback,
            exc,
        )
        return fallback
    except Exception as exc:
        logger.warning(
            "读取 agent_team_max_tool_rounds 配置异常，使用默认值 {}: {}",
            fallback,
            exc,
        )
        return fallback


class FullStackExpertAgent:
    """全栈专家 Agent - 通过工具调用自主完成代码修改。"""

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
        self.tool_executor = create_executor("fullstack")
        self.file_state = ReadFileState()
        self.checkpoint = checkpoint
        self.session_id = session_id
        self.restored_messages = initial_messages is not None
        self.messages: list[dict[str, Any]] = initial_messages or [
            {"role": "system", "content": FULLSTACK_SYSTEM_PROMPT}
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
        self, skills_context: dict[str, Any] | None = None
    ) -> ToolContext:
        extra: dict[str, Any] = {"file_state": self.file_state}
        if skills_context:
            extra.update(skills_context)
        return ToolContext(
            workspace=str(self.workspace),
            workspace_service=self.workspace_service,
            read_file_state={},
            extra=extra,
        )

    async def execute(
        self,
        task_title: str,
        task_summary: str,
        source_type: str = "",
        source_issue_number: int | None = None,
        sakura_memory: str = "",
        skills_summary: str = "",
        skills_context: dict[str, Any] | None = None,
        feedback: str = "",
        handoff_context: str = "",
        role_memory_context: str = "",
        iteration: int = 1,
        max_iterations: int = 3,
    ) -> FullStackResult:
        """执行全栈专家任务，AI 自主调用工具直到完成。"""
        client, config = await create_agent_team_client()
        ctx = self._build_context(skills_context)
        tool_schemas = get_tool_definitions("fullstack", provider=config.provider)
        max_tool_rounds = await resolve_agent_team_max_tool_rounds()

        await self._ensure_system_checkpoint()
        if not self.restored_messages and not _has_missing_tool_results(self.messages):
            await self._append_message(
                {
                    "role": "user",
                    "content": self._build_user_message(
                        task_title=task_title,
                        task_summary=task_summary,
                        source_type=source_type,
                        source_issue_number=source_issue_number,
                        sakura_memory=sakura_memory,
                        skills_summary=skills_summary,
                        feedback=feedback,
                        handoff_context=handoff_context,
                        role_memory_context=role_memory_context,
                    ),
                }
            )

        tool_calls_count = 0

        for round_num in range(1, max_tool_rounds + 1):
            logger.debug("全栈专家工具调用第 {} 轮", round_num)

            pending_tool_calls = _get_missing_tool_calls(self.messages)
            if pending_tool_calls:
                terminal_output = await self._execute_tool_calls(
                    pending_tool_calls,
                    ctx,
                    round_num,
                    max_tool_rounds=max_tool_rounds,
                    iteration=iteration,
                    max_iterations=max_iterations,
                )
                tool_calls_count += len(pending_tool_calls)
                if terminal_output is not None:
                    ai_files = terminal_output.get("modified_files", [])
                    if isinstance(ai_files, list):
                        merged = set(ai_files) | ctx.modified_files
                    else:
                        merged = ctx.modified_files
                    return FullStackResult(
                        success=True,
                        summary=terminal_output.get("summary", ""),
                        modified_files=sorted(merged),
                        risk_level=terminal_output.get("risk_level", "medium"),
                        test_result=terminal_output.get("test_result", ""),
                        tool_calls_count=tool_calls_count,
                    )
                continue

            model_messages = await AgentTeamContextCompressor(
                target_model=config.model,
                compressor_model=config.summary_model,
            ).build_model_messages(self.messages)
            response = await client.call_with_retry(
                messages=model_messages,
                model=config.model,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                timeout=config.timeout_seconds,
                tools=tool_schemas,
                tool_choice="auto",
            )

            if not response.choices:
                return FullStackResult(
                    success=False,
                    summary="AI 返回空响应",
                    modified_files=sorted(ctx.modified_files),
                    error="empty_response",
                )

            choice = response.choices[0]
            message = choice.message

            # 构建助手消息
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if message.content:
                assistant_msg["content"] = message.content
            if message.tool_calls:
                assistant_msg["tool_calls"] = [
                    _tool_call_to_dict(tc) for tc in message.tool_calls
                ]
            await self._append_message(assistant_msg)

            # 无工具调用 → AI 以纯文本完成
            if not message.tool_calls:
                tracked = sorted(ctx.modified_files)
                return FullStackResult(
                    success=True,
                    summary=message.content or "任务完成（无工具调用）",
                    modified_files=tracked,
                    tool_calls_count=tool_calls_count,
                )

            # 逐个执行工具调用
            terminal_output = await self._execute_tool_calls(
                message.tool_calls,
                ctx,
                round_num,
                max_tool_rounds=max_tool_rounds,
                iteration=iteration,
                max_iterations=max_iterations,
            )
            tool_calls_count += len(message.tool_calls)

            if terminal_output is not None:
                ai_files = terminal_output.get("modified_files", [])
                if isinstance(ai_files, list):
                    merged = set(ai_files) | ctx.modified_files
                else:
                    merged = ctx.modified_files
                return FullStackResult(
                    success=True,
                    summary=terminal_output.get("summary", ""),
                    modified_files=sorted(merged),
                    risk_level=terminal_output.get("risk_level", "medium"),
                    test_result=terminal_output.get("test_result", ""),
                    tool_calls_count=tool_calls_count,
                )

        modified_files = sorted(ctx.modified_files)
        if modified_files:
            summary = (
                f"达到最大工具调用轮次 ({max_tool_rounds})，"
                f"已修改 {len(modified_files)} 个文件但未调用 finish_task"
            )
            error = "max_rounds_reached_with_changes"
        else:
            summary = f"达到最大工具调用轮次 ({max_tool_rounds})"
            error = "max_rounds_reached"

        return FullStackResult(
            success=False,
            summary=summary,
            modified_files=modified_files,
            tool_calls_count=tool_calls_count,
            error=error,
        )

    async def _execute_tool_calls(
        self,
        tool_calls: list[Any],
        ctx: ToolContext,
        round_num: int,
        max_tool_rounds: int = 30,
        iteration: int = 1,
        max_iterations: int = 3,
    ) -> dict[str, Any] | None:
        terminal_output: dict[str, Any] | None = None
        settings = get_settings()
        max_files = getattr(settings, "agent_team_max_files_changed", 8)
        progress_suffix = (
            f"\n\n[进度: 第 {round_num}/{max_tool_rounds} 轮"
            f" | 已修改 {len(ctx.modified_files)}/{max_files} 个文件"
            f" | 迭代 {iteration}/{max_iterations}]"
        )
        for tool_call in tool_calls:
            fn_name = tool_call.function.name
            logger.info("全栈专家调用工具: {} (round={})", fn_name, round_num)

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
            clean_content = _serialize_tool_result(result)
            result_message_id = await self._append_message(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": clean_content,  # checkpoint persists valid JSON
                }
            )
            self.messages[-1]["content"] = clean_content + progress_suffix
            if self.checkpoint and self.session_id and result_message_id:
                await self.checkpoint.mark_tool_call_completed(
                    self.session_id, tool_call.id, result_message_id
                )

            if result.is_terminal:
                terminal_output = result.output
        return terminal_output

    def _build_user_message(
        self,
        task_title: str,
        task_summary: str,
        source_type: str,
        source_issue_number: int | None,
        sakura_memory: str,
        skills_summary: str,
        feedback: str,
        handoff_context: str = "",
        role_memory_context: str = "",
    ) -> str:
        parts = [f"## 任务\n标题: {task_title}\n"]
        if source_issue_number:
            parts.append(f"关联 Issue: #{source_issue_number}\n")
        if source_type:
            parts.append(f"来源类型: {source_type}\n")
        parts.append(f"\n## 任务描述\n{task_summary}\n")
        if sakura_memory:
            parts.append(f"\n## 项目记忆\n{sakura_memory}\n")
        if skills_summary:
            parts.append(f"\n{skills_summary}\n")
        if role_memory_context:
            parts.append(f"\n## 全栈专家历史记忆\n{role_memory_context}\n")
        if handoff_context:
            parts.append(f"\n## 专家对话交接\n{handoff_context}\n")
        if feedback:
            parts.append(f"\n## 审查反馈（请针对以下问题修改）\n{feedback}\n")
        return "".join(parts)


# ── 辅助函数 ──────────────────────────────────────────


def _tool_call_to_dict(tc: Any) -> dict[str, Any]:
    return {
        "id": tc.id,
        "type": "function",
        "function": {
            "name": tc.function.name,
            "arguments": tc.function.arguments,
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


def _serialize_tool_result(result: ToolResult) -> str:
    if result.success:
        return json.dumps(result.output, ensure_ascii=False, default=str)
    return json.dumps({"error": result.error}, ensure_ascii=False)
