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

AI 自主决定调用哪些工具、读取哪些文件、如何修改，循环执行直到调用 finish_task。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from backend.services.agent_team.ai_client import create_agent_team_client
from backend.services.agent_team.tools.base import ToolContext, ToolResult
from backend.services.agent_team.tools.file_state import ReadFileState
from backend.services.agent_team.tools.registry import create_executor, get_tool_definitions
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService

FULLSTACK_SYSTEM_PROMPT = """你是 Sakura Agent 专家团队的全栈专家角色。

## 你的职责
根据任务描述，自主分析仓库代码并完成代码修改。

## 工具使用指南

### 读取文件
- `read_file`: 读取文件内容（输出带行号）。修改前务必先读取文件。
  - 支持行范围读取：指定 start_line 和 end_line

### 编辑文件（三种方式）
- `edit_file`: 精确字符串替换（适合小范围修改）
  - old_text 必须与文件完全一致，从 read_file 输出中精确复制（去掉行号前缀）
  - 多处匹配时会报错，需扩大上下文
- `replace_lines`: 按行号范围替换（适合替换整个函数/方法）
  - 配合 read_file 的行号使用：如 replace_lines(start_line=10, end_line=25, new_content=...)
- `insert_lines`: 在指定行号后插入（适合添加新代码）
  - after_line=0 在文件开头插入
- `write_file`: 整文件写入（适合创建新文件或全量重写）

### 搜索与探索
- `list_directory`: 列出目录内容
- `glob`: 按文件名模式查找（如 **/*.py）
- `search_in_files`: 搜索代码内容（支持正则）

### 执行命令
- `run_command`: 执行 shell 命令（运行测试、语法检查等）

### 完成
- `finish_task`: 标记任务完成

## 工作流程
1. 先使用 `list_directory` 或 `glob` 了解项目结构
2. 使用 `read_file` 阅读相关源代码
3. 使用 `search_in_files` 搜索相关实现
4. 使用编辑工具修改代码
5. 使用 `run_command` 运行测试或检查
6. 确认所有修改正确后，调用 `finish_task` 提交结果

## 重要规则
- 修改前必须先 `read_file` 查看要修改的内容
- 优先使用 `edit_file` 或 `replace_lines` 而非 `write_file`
- 保持与项目现有代码风格一致
- 写完代码后尽量运行测试验证
- 完成后调用 `finish_task`
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


class FullStackExpertAgent:
    """全栈专家 Agent - 通过工具调用自主完成代码修改。"""

    MAX_TOOL_ROUNDS = 30

    def __init__(
        self,
        workspace: str | Any,
        workspace_service: AgentTeamWorkspaceService | None = None,
    ):
        self.workspace_service = workspace_service or AgentTeamWorkspaceService()
        self.workspace = self.workspace_service.resolve_inside_workspace(workspace)
        self.tool_executor = create_executor("fullstack")
        self.file_state = ReadFileState()

    def _build_context(self) -> ToolContext:
        return ToolContext(
            workspace=str(self.workspace),
            workspace_service=self.workspace_service,
            read_file_state={},
            extra={"file_state": self.file_state},
        )

    async def execute(
        self,
        task_title: str,
        task_summary: str,
        source_type: str = "",
        source_issue_number: int | None = None,
        sakura_memory: str = "",
        feedback: str = "",
    ) -> FullStackResult:
        """执行全栈专家任务，AI 自主调用工具直到完成。"""
        client, config = await create_agent_team_client()
        ctx = self._build_context()
        tool_schemas = get_tool_definitions("fullstack")

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": FULLSTACK_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": self._build_user_message(
                    task_title=task_title,
                    task_summary=task_summary,
                    source_type=source_type,
                    source_issue_number=source_issue_number,
                    sakura_memory=sakura_memory,
                    feedback=feedback,
                ),
            },
        ]

        tool_calls_count = 0

        for round_num in range(1, self.MAX_TOOL_ROUNDS + 1):
            logger.debug("全栈专家工具调用第 {} 轮", round_num)

            response = await client.call_with_retry(
                messages=messages,
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
            messages.append(assistant_msg)

            # 无工具调用 → AI 以纯文本完成
            if not message.tool_calls:
                return FullStackResult(
                    success=True,
                    summary=message.content or "任务完成（无工具调用）",
                    tool_calls_count=tool_calls_count,
                )

            # 逐个执行工具调用
            for tool_call in message.tool_calls:
                tool_calls_count += 1
                fn_name = tool_call.function.name
                logger.info("全栈专家调用工具: {} (round={})", fn_name, round_num)

                result = await self.tool_executor.execute_tool_call(tool_call, ctx)

                # 终止工具 → 直接返回
                if result.is_terminal:
                    output = result.output
                    return FullStackResult(
                        success=True,
                        summary=output.get("summary", ""),
                        modified_files=output.get("modified_files", []),
                        risk_level=output.get("risk_level", "medium"),
                        test_result=output.get("test_result", ""),
                        tool_calls_count=tool_calls_count,
                    )

                # 工具结果加入历史
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": _serialize_tool_result(result),
                    }
                )

        return FullStackResult(
            success=False,
            summary=f"达到最大工具调用轮次 ({self.MAX_TOOL_ROUNDS})",
            tool_calls_count=tool_calls_count,
            error="max_rounds_reached",
        )

    def _build_user_message(
        self,
        task_title: str,
        task_summary: str,
        source_type: str,
        source_issue_number: int | None,
        sakura_memory: str,
        feedback: str,
    ) -> str:
        parts = [f"## 任务\n标题: {task_title}\n"]
        if source_issue_number:
            parts.append(f"关联 Issue: #{source_issue_number}\n")
        if source_type:
            parts.append(f"来源类型: {source_type}\n")
        parts.append(f"\n## 任务描述\n{task_summary}\n")
        if sakura_memory:
            parts.append(f"\n## 项目记忆\n{sakura_memory}\n")
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


def _serialize_tool_result(result: ToolResult) -> str:
    if result.success:
        return json.dumps(result.output, ensure_ascii=False, default=str)
    return json.dumps({"error": result.error}, ensure_ascii=False)
