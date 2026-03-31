"""Issue AI 分析引擎"""

import json
import re
from datetime import datetime, timedelta
from typing import Dict, Any, List
from loguru import logger

from backend.core.config import get_settings, get_strategy_config
from backend.services.ai_reviewer.api_client import AIApiClient
from backend.services.ai_reviewer.tools import (
    FileToolHandler,
    SearchToolHandler,
    ToolHandler,
    ToolManager,
)

# 协作者缓存：{repo_full_name: {"collaborators": list, "updated_at": datetime}}
_collaborator_cache: Dict[str, Dict[str, Any]] = {}
_COLLABORATOR_CACHE_TTL = timedelta(hours=1)


class IssueAnalyzer:
    """Issue AI 分析引擎"""

    def __init__(self):
        settings = get_settings()
        self.api_client = AIApiClient(
            base_url=settings.openai_api_base, api_key=settings.openai_api_key
        )
        file_tool = FileToolHandler()
        search_tool = SearchToolHandler()
        self.tool_handler = ToolHandler(file_tool, search_tool)
        self.tool_manager = ToolManager()
        self.tools = self.tool_manager.get_all_tools_definitions()

    def _build_system_prompt(
        self, repo_full_name: str, available_labels: List[str], issue_number: int = None
    ) -> str:
        """构建系统提示词"""
        config = get_strategy_config().get_issue_analysis_config()
        base_prompt = config.get("system_prompt", "")

        labels_section = ""
        if available_labels:
            labels_section = f"\n\n## 仓库可用标签\n{', '.join(available_labels)}\n请优先从以上标签中选择。"

        repo_section = f"\n\n## 当前仓库\n{repo_full_name}"

        issue_section = ""
        if issue_number is not None:
            issue_section = (
                f"\n\n## 当前 Issue\n"
                f"你正在分析 Issue #{issue_number}。"
                f"duplicate_of 字段只能指向其他 Issue 的编号，不能设置为 {issue_number}。"
            )

        return base_prompt + labels_section + repo_section + issue_section

    def _build_user_message(
        self,
        issue_info: Dict[str, Any],
        available_labels: List[str],
        collaborators: List[str],
    ) -> str:
        """构建用户消息"""
        parts = [
            f"## Issue #{issue_info.get('issue_number', '?')}",
            f"**标题**: {issue_info.get('title', 'N/A')}",
            f"**作者**: {issue_info.get('author', 'N/A')}",
            f"**状态**: {issue_info.get('state', 'open')}",
        ]

        body = issue_info.get("body", "")
        if body:
            if len(body) > 3000:
                body = body[:3000] + "\n\n...（内容已截断）"
            parts.append(f"\n**内容**:\n{body}")

        existing_labels = issue_info.get("labels", [])
        if existing_labels:
            parts.append(f"\n**已有标签**: {', '.join(existing_labels)}")

        if collaborators:
            parts.append(f"\n**仓库协作者**: {', '.join(collaborators)}")

        return "\n".join(parts)

    def _parse_analysis_result(self, response_text: str) -> Dict[str, Any]:
        """解析 AI 返回的分析结果"""
        text = response_text.strip()

        # 移除可能的 markdown 代码块标记
        text = re.sub(r"^```json\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 尝试提取 JSON 块（支持嵌套）
            depth = 0
            start = -1
            for i, ch in enumerate(text):
                if ch == "{":
                    if depth == 0:
                        start = i
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0 and start >= 0:
                        try:
                            return json.loads(text[start : i + 1])
                        except json.JSONDecodeError:
                            break
            logger.warning(f"无法解析分析结果 JSON: {text[:200]}...")
            return {
                "category": "other",
                "priority": "medium",
                "summary": response_text[:500] if response_text else "解析失败",
                "feasibility": "无法评估",
                "suggested_labels": [],
                "suggested_assignees": [],
                "suggested_milestone": None,
                "duplicate_of": None,
            }

    async def analyze_issue(
        self,
        issue_info: Dict[str, Any],
        repo_owner: str,
        repo_name: str,
        repo: Any = None,
    ) -> Dict[str, Any]:
        """分析 Issue

        Args:
            issue_info: Issue 信息（来自 webhook）
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            repo: GitHub 仓库对象（可选，用于工具调用）

        Returns:
            分析结果字典，包含 token 和 cost 信息
        """
        settings = get_settings()

        repo_full_name = f"{repo_owner}/{repo_name}"

        # 获取仓库标签（使用 LabelService 缓存）
        from backend.services.label_service import label_service
        labels_dict = await label_service.get_repo_labels(repo_owner, repo_name)
        available_labels = list(labels_dict.keys())

        # 获取仓库协作者（带缓存）
        from backend.core.github_app import GitHubAppClient

        github_app = GitHubAppClient()
        cache_key = repo_full_name
        now = datetime.now()
        if (
            cache_key in _collaborator_cache
            and now - _collaborator_cache[cache_key]["updated_at"]
            < _COLLABORATOR_CACHE_TTL
        ):
            collaborators = _collaborator_cache[cache_key]["collaborators"]
            logger.debug(f"使用缓存的协作者列表: {cache_key}")
        else:
            collaborators = github_app.get_repo_collaborators(repo_owner, repo_name)
            _collaborator_cache[cache_key] = {
                "collaborators": collaborators,
                "updated_at": now,
            }
            logger.debug(f"从 GitHub 获取协作者列表: {cache_key}")

        # 构建提示词
        system_prompt = self._build_system_prompt(
            repo_full_name, available_labels, issue_info.get("issue_number")
        )
        user_message = self._build_user_message(
            issue_info, available_labels, collaborators
        )

        # 初始化消息列表
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        # 获取启用的工具
        enabled_tools = await self.tool_manager.get_enabled_tools(repo_full_name)

        # 多轮对话循环（带工具调用）
        max_iterations = settings.issue_max_tool_iterations
        iteration = 0
        total_prompt_tokens = 0
        total_completion_tokens = 0

        while iteration < max_iterations:
            iteration += 1

            try:
                response = await self.api_client.call_with_retry(
                    model=settings.openai_model,
                    messages=messages,
                    tools=enabled_tools,
                    tool_choice="auto",
                    temperature=settings.openai_temperature,
                )
            except Exception as e:
                logger.error(f"AI API 调用失败: {e}", exc_info=True)
                return {
                    "category": "other",
                    "priority": "medium",
                    "summary": f"AI 分析失败: {str(e)}",
                    "feasibility": "无法评估",
                    "suggested_labels": [],
                    "suggested_assignees": [],
                    "suggested_milestone": None,
                    "duplicate_of": None,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "estimated_cost": 0,
                }

            # 验证响应有效性
            if not response.choices:
                logger.error("AI API 返回空响应")
                return {
                    "category": "other",
                    "priority": "medium",
                    "summary": "AI 分析失败：API 返回空响应",
                    "feasibility": "无法评估",
                    "suggested_labels": [],
                    "suggested_assignees": [],
                    "suggested_milestone": None,
                    "duplicate_of": None,
                    "prompt_tokens": total_prompt_tokens,
                    "completion_tokens": total_completion_tokens,
                    "estimated_cost": 0,
                }

            # 累积 token 使用
            if hasattr(response, "usage") and response.usage:
                total_prompt_tokens += getattr(response.usage, "prompt_tokens", 0) or 0
                total_completion_tokens += (
                    getattr(response.usage, "completion_tokens", 0) or 0
                )

            # 检查是否有工具调用
            tool_calls = (
                response.choices[0].message.tool_calls if response.choices else None
            )

            if not tool_calls:
                # AI 完成分析，解析结果
                review_text = response.choices[0].message.content
                result = self._parse_analysis_result(review_text)

                # 计算成本
                price_prompt = settings.issue_price_per_1k_prompt
                price_completion = settings.issue_price_per_1k_completion
                estimated_cost = (total_prompt_tokens / 1000) * price_prompt + (
                    total_completion_tokens / 1000
                ) * price_completion

                result["prompt_tokens"] = total_prompt_tokens
                result["completion_tokens"] = total_completion_tokens
                result["estimated_cost"] = (
                    int(estimated_cost * 100) if estimated_cost else 0
                )

                logger.info(
                    f"Issue #{issue_info.get('issue_number')} 分析完成 "
                    f"({iteration}轮对话, tokens: {total_prompt_tokens}+{total_completion_tokens})"
                )
                return result

            # 处理工具调用
            assistant_message = response.choices[0].message
            assistant_msg_dict = {
                "role": "assistant",
                "content": assistant_message.content,
                "tool_calls": tool_calls,
            }

            # DeepSeek-R1 reasoning_content 支持
            if (
                hasattr(assistant_message, "reasoning_content")
                and assistant_message.reasoning_content
            ):
                strategy_config = get_strategy_config()
                if strategy_config.is_model_supports_reasoning_content(
                    settings.openai_model
                ):
                    assistant_msg_dict["reasoning_content"] = (
                        assistant_message.reasoning_content
                    )

            messages.append(assistant_msg_dict)

            # 执行工具调用
            for tool_call in tool_calls:
                try:
                    result = await self.tool_handler.handle_tool_call(
                        tool_call, repo, None
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                    logger.debug(f"执行工具 {tool_call.function.name} (Issue 分析)")
                except Exception as e:
                    logger.error(f"工具调用失败: {e}")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps(
                                {"error": str(e)}, ensure_ascii=False
                            ),
                        }
                    )

        # 达到最大迭代次数
        logger.warning(f"Issue 分析达到最大迭代次数 ({max_iterations})")
        last_content = response.choices[0].message.content if response.choices else ""
        result = (
            self._parse_analysis_result(last_content)
            if last_content
            else {
                "category": "other",
                "priority": "medium",
                "summary": "分析未完成（达到最大工具调用次数）",
                "feasibility": "无法评估",
                "suggested_labels": [],
                "suggested_assignees": [],
                "suggested_milestone": None,
                "duplicate_of": None,
            }
        )

        price_prompt = settings.issue_price_per_1k_prompt
        price_completion = settings.issue_price_per_1k_completion
        estimated_cost = (total_prompt_tokens / 1000) * price_prompt + (
            total_completion_tokens / 1000
        ) * price_completion

        result["prompt_tokens"] = total_prompt_tokens
        result["completion_tokens"] = total_completion_tokens
        result["estimated_cost"] = int(estimated_cost * 100) if estimated_cost else 0
        return result
