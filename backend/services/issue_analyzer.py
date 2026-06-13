"""Issue AI 分析引擎"""

import json
import re
from datetime import datetime, timedelta
from typing import Any, Callable, Coroutine, Dict, List, Optional
from loguru import logger

from backend.core.config import (
    get_settings,
    get_strategy_config,
    get_user_dynamic_config,
    get_dynamic_config,
)
from backend.models.database import AppConfig, async_session
from backend.services.ai_reviewer.api_client import AIApiClient
from backend.services.ai_reviewer.token_tracker import TokenTracker
from backend.services.ai_reviewer.message_utils import estimate_messages_tokens
from backend.core.model_context import get_model_context_manager
from backend.services.ai_reviewer.tools import (
    FileToolHandler,
    GitToolHandler,
    SakuraToolHandler,
    SearchFilesToolHandler,
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
        self._ai_client_config = None
        self._refresh_ai_client()
        file_tool = FileToolHandler()
        search_tool = SearchToolHandler()
        git_tool = GitToolHandler()
        search_files_tool = SearchFilesToolHandler()
        sakura_tool = SakuraToolHandler()
        web_search_tool = None
        if settings.web_search_enabled:
            from backend.services.ai_reviewer.tools.web_search_tool import (
                WebSearchToolHandler,
            )

            web_search_tool = WebSearchToolHandler()
        fetch_url_tool = None
        if web_search_tool is not None and settings.fetch_url_enabled:
            from backend.services.ai_reviewer.tools.fetch_url_tool import (
                FetchUrlToolHandler,
            )

            fetch_url_tool = FetchUrlToolHandler()
        self.tool_handler = ToolHandler(
            file_tool,
            search_tool,
            web_search_tool,
            git_tool,
            search_files_tool,
            sakura_tool,
            fetch_url_tool,
        )
        self.tool_manager = ToolManager()
        self.tools = self.tool_manager.get_all_tools_definitions()

    def _refresh_runtime_config(self) -> None:
        """刷新运行中可切换的工具配置。"""
        settings = get_settings()
        if settings.web_search_enabled:
            if self.tool_handler.web_search_tool is None:
                from backend.services.ai_reviewer.tools.web_search_tool import (
                    WebSearchToolHandler,
                )

                self.tool_handler.web_search_tool = WebSearchToolHandler()
        else:
            self.tool_handler.web_search_tool = None

        if settings.web_search_enabled and settings.fetch_url_enabled:
            if self.tool_handler.fetch_url_tool is None:
                from backend.services.ai_reviewer.tools.fetch_url_tool import (
                    FetchUrlToolHandler,
                )

                self.tool_handler.fetch_url_tool = FetchUrlToolHandler()
        else:
            self.tool_handler.fetch_url_tool = None

    def _refresh_ai_client(self) -> None:
        """刷新动态 AI 配置，避免长生命周期 Worker 持有旧凭据。"""
        settings = get_settings()
        config = (settings.openai_api_base, settings.openai_api_key)
        if self._ai_client_config == config:
            return
        self.api_client = AIApiClient(
            base_url=settings.openai_api_base,
            api_key=settings.openai_api_key,
        )
        self._ai_client_config = config

    def _build_system_prompt(
        self,
        repo_full_name: str,
        available_labels: List[str],
        issue_number: int = None,
        output_language: str = "",
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

        result = base_prompt + labels_section + repo_section + issue_section

        # 注入输出语言指令 / Inject output language directive
        output_lang = output_language
        if output_lang:
            language_names = {
                "zh-CN": "中文 (Simplified Chinese)",
                "en": "English",
            }
            lang_display = language_names.get(output_lang, output_lang)
            result += f"\n\n## Output Language\n**Important**: You MUST write all analysis, summaries, and comments in {lang_display}."

        return result

    def _build_user_message(
        self,
        issue_info: Dict[str, Any],
        available_labels: List[str],
        collaborators: List[str],
        comments: List[Dict[str, Any]] | None = None,
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
            parts.append(f"\n**内容**:\n{body}")

        existing_labels = issue_info.get("labels", [])
        if existing_labels:
            parts.append(f"\n**已有标签**: {', '.join(existing_labels)}")

        if collaborators:
            parts.append(f"\n**仓库协作者**: {', '.join(collaborators)}")

        if comments:
            parts.append("\n## 评论讨论")
            for comment in comments:
                author = comment.get("author", "unknown")
                body_text = comment.get("body", "")
                is_bot = comment.get("is_bot", False)
                if is_bot:
                    parts.append(f"\n### @{author} (AI 先前分析)\n{body_text}")
                else:
                    parts.append(f"\n### @{author}\n{body_text}")

        return "\n".join(parts)

    async def _fetch_issue_comments(
        self, github_app, repo_owner: str, repo_name: str, issue_number: int
    ) -> List[Dict[str, Any]] | None:
        """获取 Issue 评论，受 issue_max_comments_in_context 配置控制数量"""
        if issue_number <= 0:
            return None

        import asyncio

        settings = get_settings()
        bot_username = settings.bot_username

        try:
            comments = await asyncio.to_thread(
                github_app.get_issue_comments,
                repo_owner,
                repo_name,
                issue_number,
            )
        except Exception as e:
            logger.warning("GitHub API 获取评论失败: {}", e)
            return None

        if not comments:
            return None

        try:
            max_count = int(
                await get_dynamic_config("issue_max_comments_in_context") or 0
            )
        except (ValueError, TypeError):
            max_count = 0

        raw_comments = []
        for c in comments:
            author = getattr(c.user, "login", "unknown") if c.user else "unknown"
            raw_comments.append(
                {
                    "author": author,
                    "body": c.body or "",
                    "is_bot": bool(bot_username and author == bot_username),
                }
            )

        # 按时间正序排列（旧 → 新），便于 AI 理解对话发展
        if max_count > 0:
            raw_comments = raw_comments[-max_count:]

        return raw_comments

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

            # JSON 完整解析失败，尝试从不完整的 JSON 中提取字段
            partial_result = self._extract_partial_json_fields(text)

            # 提取到有效 summary 时使用提取结果，否则使用完整响应文本
            if not partial_result.get("summary"):
                # 移除开头的思考/过渡文本，保留有价值的内容
                cleaned_text = response_text.strip() if response_text else "解析失败"
                partial_result["summary"] = cleaned_text

            logger.warning("无法解析分析结果 JSON，已降级处理: {}", text)
            return partial_result

    @staticmethod
    def _extract_partial_json_fields(text: str) -> Dict[str, Any]:
        """从不完整的 JSON 文本中提取已知字段

        当 AI 返回的 JSON 被截断（如 token 限制）或前面带有思考文本时，
        尝试通过正则提取关键字段以减少信息丢失。

        Args:
            text: AI 返回的原始文本（可能包含不完整 JSON）

        Returns:
            提取到的字段字典，缺失字段使用默认值填充
        """
        # 找到 JSON 起始位置
        json_start = text.find("{")
        json_text = text[json_start:] if json_start >= 0 else ""

        def _unescape_json_string(value: str) -> str:
            """反转义 JSON 字符串中的转义序列。

            优先使用 ``json.loads`` 一次性正确处理所有转义，
            避免 ``replace("\\\\", "\\")`` 后新产生的 ``\\n`` 被后续替换误伤。
            截断 / 非法转义时回退到逐项替换。
            """
            try:
                return json.loads(f'"{value}"')
            except json.JSONDecodeError:
                # Best-effort fallback for truncated / malformed JSON
                return (
                    value.replace("\\\\", "\\")
                    .replace("\\n", "\n")
                    .replace("\\r", "\r")
                    .replace("\\t", "\t")
                    .replace("\\b", "\b")
                    .replace("\\f", "\f")
                    .replace("\\/", "/")
                    .replace('\\"', '"')
                )

        def _extract_string_field(name: str) -> str | None:
            """提取 JSON 字符串字段值，处理转义字符。

            正则使用 ``(?:"|$)`` 而非更严格的 ``"`` 来闭合，
            以处理 AI 响应被截断时最后一个字段缺少闭合引号的场景。
            """
            pattern = rf'"{name}"\s*:\s*"((?:[^"\\]|\\.)*)(?:"|$)'
            match = re.search(pattern, json_text)
            if match:
                return _unescape_json_string(match.group(1))
            return None

        def _extract_number_field(name: str) -> int | None:
            """提取 JSON 数值型字段（int 或 null）"""
            pattern = rf'"{name}"\s*:\s*(\d+|null)'
            match = re.search(pattern, json_text)
            if match:
                val = match.group(1)
                return None if val == "null" else int(val)
            return None

        # 提取各个字段
        category = _extract_string_field("category")
        priority = _extract_string_field("priority")
        summary = _extract_string_field("summary")
        feasibility = _extract_string_field("feasibility")
        suggested_title = _extract_string_field("suggested_title")
        duplicate_of = _extract_number_field("duplicate_of")

        # 尝试提取 suggested_labels 和 suggested_assignees 数组
        suggested_labels = []
        suggested_assignees = []

        # suggested_labels: 提取数组中的 name 字段
        labels_match = re.search(r'"suggested_labels"\s*:\s*\[', json_text)
        if labels_match:
            array_text = json_text[labels_match.end() :]
            # 逐个提取 name 和 confidence
            for m in re.finditer(r'\{\s*"name"\s*:\s*"((?:[^"\\]|\\.)*)"', array_text):
                label_name = _unescape_json_string(m.group(1))
                suggested_labels.append(
                    {
                        "name": label_name,
                        "confidence": 0.5,
                        "reason": "",
                    }
                )

        # suggested_assignees: 提取数组中的 username 字段
        assignees_match = re.search(r'"suggested_assignees"\s*:\s*\[', json_text)
        if assignees_match:
            array_text = json_text[assignees_match.end() :]
            for m in re.finditer(
                r'\{\s*"username"\s*:\s*"((?:[^"\\]|\\.)*)"', array_text
            ):
                username = _unescape_json_string(m.group(1))
                suggested_assignees.append(
                    {
                        "username": username,
                        "confidence": 0.5,
                        "reason": "",
                    }
                )

        return {
            "category": category or "other",
            "priority": priority or "medium",
            "summary": summary or "",
            "feasibility": feasibility or "无法评估",
            "suggested_labels": suggested_labels,
            "suggested_assignees": suggested_assignees,
            "suggested_milestone": None,
            "suggested_title": suggested_title,
            "duplicate_of": duplicate_of,
        }

    async def analyze_issue(
        self,
        issue_info: Dict[str, Any],
        repo_owner: str,
        repo_name: str,
        repo: Any = None,
        event_callback: Optional[Callable[[str, Dict[str, Any]], Coroutine]] = None,
    ) -> Dict[str, Any]:
        """分析 Issue

        Args:
            issue_info: Issue 信息（来自 webhook）
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            repo: GitHub 仓库对象（可选，用于工具调用）
            event_callback: 可选事件回调，用于实时推送工具调用进度到前端。

        Returns:
            分析结果字典，包含 token 和 cost 信息
        """
        self._refresh_ai_client()
        self._refresh_runtime_config()
        if self.tool_handler.fetch_url_tool:
            await self.tool_handler.fetch_url_tool.reset_session()

        settings = get_settings()
        output_language = await get_user_dynamic_config(
            "output_language", issue_info.get("user_id")
        )

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
            logger.info("使用缓存的协作者列表: {}", cache_key)
        else:
            collaborators = github_app.get_repo_collaborators(repo_owner, repo_name)
            _collaborator_cache[cache_key] = {
                "collaborators": collaborators,
                "updated_at": now,
            }
            logger.info("从 GitHub 获取协作者列表: {}", cache_key)

        # 获取评论对话（受配置控制）
        comments = None
        include_comments = await get_dynamic_config("issue_include_comments")
        if include_comments:
            try:
                comments = await self._fetch_issue_comments(
                    github_app, repo_owner, repo_name, issue_info.get("issue_number", 0)
                )
            except Exception as e:
                logger.warning("获取 Issue 评论失败（不影响分析）: {}", e)

        # 构建提示词
        system_prompt = self._build_system_prompt(
            repo_full_name,
            available_labels,
            issue_info.get("issue_number"),
            output_language=output_language or "",
        )
        user_message = self._build_user_message(
            issue_info, available_labels, collaborators, comments
        )

        # 注入 .sakura/ 记忆上下文 / Inject .sakura/ memory context
        try:
            from backend.services.sakura_memory_service import get_sakura_memory_service

            sakura_memory_service = get_sakura_memory_service()
            sakura_context = await sakura_memory_service.get_sakura_context(
                repo=repo,
                repo_full_name=repo_full_name,
            )
            if sakura_context:
                sakura_md = sakura_context.get("sakura_md", "")
                memory_md = sakura_context.get("memory_md", "")
                if sakura_md or memory_md:
                    sakura_section = (
                        "\n\n## 项目知识（来自 .sakura/ 目录，请主动参考）\n\n"
                        "以下是该项目积累的审查经验和知识，请在分析中参考：\n"
                        "- 如果项目有已知的审查规则，按照规则检查代码\n"
                        "- 如果项目记忆中记录了常见问题，重点排查类似问题是否重现\n"
                        "- 避免提出与项目记忆中已确认的做法相矛盾的建议\n"
                    )
                    if sakura_md:
                        sakura_section += f"\n### 项目概述\n{sakura_md}"
                    if memory_md:
                        sakura_section += f"\n\n### 项目记忆\n{memory_md}"
                    user_message += sakura_section
        except Exception as e:
            logger.warning(".sakura/ 记忆上下文注入失败（不影响分析）: {}", e)

        # 初始化消息列表
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        # 获取启用的工具
        enabled_tools = await self.tool_manager.get_enabled_tools(repo_full_name)

        # 多轮对话循环（带工具调用）
        max_iterations = settings.issue_max_tool_iterations
        try:
            if async_session is not None:
                async with async_session() as session:
                    from sqlalchemy import select

                    result = await session.execute(
                        select(AppConfig).where(
                            AppConfig.key_name == "issue_max_tool_iterations"
                        )
                    )
                    cfg = result.scalar_one_or_none()
                    if cfg:
                        try:
                            max_iterations = int(cfg.key_value)
                        except (ValueError, TypeError):
                            logger.warning(
                                "Invalid issue_max_tool_iterations config: {}",
                                cfg.key_value,
                            )
        except Exception as exc:
            logger.warning("读取 issue_max_tool_iterations 配置失败: {}", exc)
        iteration = 0
        tracker = TokenTracker()
        model_ctx_mgr = get_model_context_manager()
        safe_context = model_ctx_mgr.calculate_safe_context(settings.openai_model, 0.8)

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
                logger.error("AI API 调用失败: {}", e, exc_info=True)
                return {
                    "category": "other",
                    "priority": "medium",
                    "summary": f"AI 分析失败: {str(e)}",
                    "feasibility": "无法评估",
                    "suggested_labels": [],
                    "suggested_assignees": [],
                    "suggested_milestone": None,
                    "duplicate_of": None,
                    "prompt_tokens": tracker.prompt_tokens,
                    "completion_tokens": tracker.completion_tokens,
                    "tool_rounds": iteration,
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
                    "prompt_tokens": tracker.prompt_tokens,
                    "completion_tokens": tracker.completion_tokens,
                    "tool_rounds": iteration,
                    "estimated_cost": 0,
                }

            # 累积 token 使用
            tracker.accumulate(response)

            # 检查是否有工具调用
            tool_calls = (
                response.choices[0].message.tool_calls if response.choices else None
            )

            if not tool_calls:
                # AI 完成分析，解析结果
                review_text = response.choices[0].message.content
                result = self._parse_analysis_result(review_text)

                # 计算成本
                result["prompt_tokens"] = tracker.prompt_tokens
                result["completion_tokens"] = tracker.completion_tokens
                result["tool_rounds"] = iteration
                result["estimated_cost"] = tracker.calculate_cost(
                    settings.issue_price_per_1k_prompt,
                    settings.issue_price_per_1k_completion,
                )

                logger.info(
                    "Issue #{} 分析完成 ({}轮对话, tokens: {}+{})",
                    issue_info.get("issue_number"),
                    iteration,
                    tracker.prompt_tokens,
                    tracker.completion_tokens,
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

            # 通知前端：assistant 消息（包含 tool_calls → 自动创建 ToolCall 行）
            if event_callback:
                try:
                    await event_callback("message", assistant_msg_dict)
                except Exception as exc:
                    logger.warning("event_callback failed: {}", exc)

            # 执行工具调用
            for tool_call in tool_calls:
                try:
                    if event_callback:
                        try:
                            await event_callback("tool_running", tool_call.id)
                        except Exception as exc:
                            logger.warning("event_callback failed: {}", exc)
                    result = await self.tool_handler.handle_tool_call(
                        tool_call, repo, None
                    )
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                    messages.append(tool_msg)
                    logger.info("执行工具 {} (Issue 分析)", tool_call.function.name)
                    if event_callback:
                        try:
                            await event_callback("message", tool_msg)
                        except Exception as exc:
                            logger.warning("event_callback failed: {}", exc)
                except Exception as e:
                    logger.error("工具调用失败: {}", e)
                    error_tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(
                            {"error": str(e)}, ensure_ascii=False
                        ),
                    }
                    messages.append(error_tool_msg)
                    if event_callback:
                        try:
                            await event_callback("message", error_tool_msg)
                        except Exception as exc:
                            logger.warning("event_callback failed: {}", exc)

            # 每轮工具调用处理后记录上下文使用率
            current_tokens = estimate_messages_tokens(messages, model_ctx_mgr)
            tracker.log_context_usage(current_tokens, safe_context, iteration)

        # 达到最大迭代次数，做最后一次 API 调用强制 AI 返回结果
        logger.warning(
            f"Issue 分析达到最大迭代次数 ({max_iterations})，强制生成最终结果"
        )
        messages.append(
            {
                "role": "user",
                "content": "已达到最大工具调用次数，请基于已有信息立即返回最终分析结果（JSON 格式）。",
            }
        )
        try:
            final_response = await self.api_client.call_with_retry(
                model=settings.openai_model,
                messages=messages,
                temperature=0.3,
            )
            last_content = final_response.choices[0].message.content or ""
        except Exception as e:
            logger.error("最终 API 调用失败: {}", e)
            last_content = ""

        if last_content:
            result = self._parse_analysis_result(last_content)
        else:
            result = {
                "category": "other",
                "priority": "medium",
                "summary": "分析未完成（达到最大工具调用次数）",
                "feasibility": "无法评估",
                "suggested_labels": [],
                "suggested_assignees": [],
                "suggested_milestone": None,
                "duplicate_of": None,
            }

        result["prompt_tokens"] = tracker.prompt_tokens
        result["completion_tokens"] = tracker.completion_tokens
        result["tool_rounds"] = max_iterations
        result["estimated_cost"] = tracker.calculate_cost(
            settings.issue_price_per_1k_prompt,
            settings.issue_price_per_1k_completion,
        )
        return result
