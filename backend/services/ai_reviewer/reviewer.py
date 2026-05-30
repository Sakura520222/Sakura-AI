"""重构后的AI审查器主类

这是重构后的主入口，通过组合各个功能模块来实现原有的功能。
保持与原 ai_reviewer.py 中 AIReviewer 类相同的公共接口。
"""

import json
from typing import Any, Callable, Coroutine, Dict, List, Optional

from loguru import logger

from backend.core.config import (
    get_settings,
    get_strategy_config,
    get_user_dynamic_config,
)
from backend.core.model_context import get_model_context_manager

from .api_client import AIApiClient, AIEmptyResponseError, PromptTooLongError
from .compact_diff import build_tool_handler_with_diff
from .compression import ContextCompressor
from .constants import MAX_TOOL_ITERATIONS
from .label_recommender import LabelRecommender
from .prompt_builder import PromptBuilder
from .result_parser import ReviewResultParser
from .token_tracker import TokenTracker
from .tools import (
    DiffToolHandler,
    FileToolHandler,
    GitToolHandler,
    SakuraToolHandler,
    SearchFilesToolHandler,
    SearchToolHandler,
    ToolHandler,
    ToolManager,
)


class AIReviewer:
    """AI审查器 - 组合各功能模块

    重构后的主类，通过组合各个专门模块来实现功能：
    - AIApiClient: AI API 调用
    - PromptBuilder: 提示词构建
    - ReviewResultParser: 结果解析
    - ToolHandler/ToolManager: 工具管理
    - ContextCompressor: 上下文压缩
    - LabelRecommender: 标签推荐

    所有 PR 审查统一为单次审查 + AI 自主使用工具查看文件变更。
    """

    def __init__(self):
        """初始化AI审查器"""
        settings = get_settings()

        # 初始化各组件
        self.api_client = AIApiClient(
            base_url=settings.openai_api_base, api_key=settings.openai_api_key
        )

        # 初始化辅助模型（摘要、压缩等轻量任务）
        self.summary_model = settings.summary_model or settings.openai_model
        if not settings.summary_api_base and not settings.summary_api_key:
            self.summary_api_client = self.api_client
        else:
            summary_api_base = settings.summary_api_base or settings.openai_api_base
            summary_api_key = settings.summary_api_key or settings.openai_api_key
            self.summary_api_client = AIApiClient(
                base_url=summary_api_base, api_key=summary_api_key
            )
        self.prompt_builder = PromptBuilder()
        self.result_parser = ReviewResultParser()

        # 初始化工具相关
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

        # PR diff 工具（按需查看文件 diff，用于 prompt 精简模式）
        # 注意：每次精简模式会创建临时 DiffToolHandler 实例，避免并发安全问题
        self.tool_handler = ToolHandler(
            file_tool,
            search_tool,
            web_search_tool,
            git_tool,
            search_files_tool,
            sakura_tool,
            fetch_url_tool,
            diff_tool=None,
        )
        self.tool_manager = ToolManager()

        # 初始化上下文压缩
        self.enable_compression = settings.enable_context_compression
        self.compression_threshold = settings.context_compression_threshold
        self.keep_rounds = settings.context_compression_keep_rounds
        self.context_compressor = ContextCompressor(
            api_client=self.summary_api_client,
            model=self.summary_model,
            keep_rounds=self.keep_rounds,
        )
        self.model_context_mgr = get_model_context_manager()

        # 初始化标签推荐
        self.label_recommender = LabelRecommender(
            api_client=self.summary_api_client,
            prompt_builder=self.prompt_builder,
            result_parser=self.result_parser,
            model=self.summary_model,
        )

        # 存储工具定义（用于向后兼容）
        self.tools = self.tool_manager.get_all_tools_definitions()

    async def review_pr(self, context: Dict[str, Any], strategy: str) -> Dict[str, Any]:
        """审查PR（标准模式，不使用工具）

        Args:
            context: 审查上下文
            strategy: 审查策略

        Returns:
            审查结果字典
        """
        try:
            logger.info("开始AI审查，策略: {}", strategy)

            settings = get_settings()
            strategy_config_data = get_strategy_config().get_strategy(strategy)
            output_lang = await get_user_dynamic_config(
                "output_language", context.get("user_id")
            )
            system_prompt = self.prompt_builder.build_system_prompt(
                strategy_config_data.get("prompt", ""),
                context,
                include_tools=False,
                output_language=output_lang or "",
            )
            tracker = TokenTracker()

            # 构建用户消息
            user_message = self.prompt_builder.build_user_message(
                context, strategy, include_tools=False
            )

            # 调用AI API
            response = await self.api_client.call_with_retry(
                model=settings.openai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=settings.openai_temperature,
            )
            tracker.accumulate(response)

            # 解析结果
            review_text = response.choices[0].message.content
            result = self.result_parser.parse_review_result(review_text, strategy)
            result["token_usage"] = tracker.to_dict()

            logger.info("AI审查完成，策略: {}", strategy)
            return result

        except Exception as e:
            logger.error("AI审查时出错: {}", str(e), exc_info=True)
            raise

    async def _run_tool_loop(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: str,
        strategy: str,
        enabled_tools: List[Any],
        repo: Any,
        pr: Any,
        tracker: TokenTracker,
        context: Dict[str, Any],
        tool_handler: ToolHandler | None = None,
        event_callback: Optional[Callable[[str, Dict[str, Any]], Coroutine]] = None,
    ) -> Dict[str, Any]:
        """执行多轮工具调用循环

        Args:
            messages: 初始消息列表 [system, user, ...]
            system_prompt: 系统提示词
            strategy: 审查策略
            enabled_tools: 启用的工具列表
            repo: GitHub仓库对象
            pr: GitHub PR对象
            tracker: TokenTracker
            context: 审查上下文
            event_callback: 可选事件回调，签名为 async (event_type, data) -> None，
                            用于实时推送工具调用进度到前端。

        Returns:
            审查结果字典
        """
        settings = get_settings()
        active_tool_handler = tool_handler or self.tool_handler
        max_iterations = (
            get_strategy_config()
            .get_context_enhancement_config()
            .get("max_tool_iterations", MAX_TOOL_ITERATIONS)
        )
        safe_context = self.model_context_mgr.calculate_safe_context(
            settings.openai_model, settings.context_safety_threshold
        )
        iteration = 0

        while iteration < max_iterations:
            iteration += 1

            # 调用AI API
            response = await self.api_client.call_with_retry(
                model=settings.openai_model,
                messages=messages,
                tools=enabled_tools,
                tool_choice="auto",
                temperature=settings.openai_temperature,
            )
            tracker.accumulate(response)

            # 防御性检查：确保响应有效
            if not response.choices:
                logger.error("AI 返回空 choices")
                raise AIEmptyResponseError("AI 返回空响应")

            # 检查是否有工具调用
            tool_calls = response.choices[0].message.tool_calls or []

            if not tool_calls:
                # AI完成了审查，返回结果
                review_text = response.choices[0].message.content or ""
                # 通知前端：最终 assistant 消息（无工具调用）
                if event_callback:
                    try:
                        await event_callback("message", {
                            "role": "assistant",
                            "content": review_text,
                        })
                    except Exception as exc:
                        logger.warning("event_callback failed: {}", exc)
                result = self.result_parser.parse_review_result(review_text, strategy)
                result["token_usage"] = tracker.to_dict()
                logger.info(
                    "AI审查完成（使用了{}轮对话），策略: {}",
                    iteration,
                    strategy,
                )
                return result

            # 处理工具调用
            assistant_message = response.choices[0].message
            assistant_msg_dict = {
                "role": "assistant",
                "content": assistant_message.content,
                "tool_calls": tool_calls,
            }

            # DeepSeek-R1 特有：必须包含 reasoning_content
            strategy_config = get_strategy_config()
            if (
                hasattr(assistant_message, "reasoning_content")
                and assistant_message.reasoning_content
                and strategy_config.is_model_supports_reasoning_content(
                    settings.openai_model
                )
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

            # 执行每个工具调用
            for tool_call in tool_calls:
                tool_name = tool_call.function.name
                try:
                    # 通知前端：工具开始运行
                    if event_callback:
                        try:
                            await event_callback("tool_running", tool_call.id)
                        except Exception as exc:
                            logger.warning("event_callback failed: {}", exc)
                    result = await active_tool_handler.handle_tool_call(
                        tool_call, repo, pr
                    )
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                    messages.append(tool_msg)
                    logger.info(
                        "执行工具 {}: {}",
                        tool_call.function.name,
                        tool_call.function.arguments,
                    )
                    # 通知前端：tool 消息 → 自动更新 ToolCall 行
                    if event_callback:
                        try:
                            await event_callback("message", tool_msg)
                        except Exception as exc:
                            logger.warning("event_callback failed: {}", exc)
                except Exception as e:
                    error_tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps({"error": str(e)}),
                    }
                    messages.append(error_tool_msg)
                    logger.error(
                        "执行工具 {} 失败: {}", tool_call.function.name, str(e)
                    )
                    # 通知前端：tool 错误消息
                    if event_callback:
                        try:
                            await event_callback("message", error_tool_msg)
                        except Exception as exc:
                            logger.warning("event_callback failed: {}", exc)

            # 记录上下文使用率
            current_tokens = self.context_compressor.estimate_messages_tokens(
                messages
            )
            tracker.log_context_usage(current_tokens, safe_context, iteration)

            # 检查上下文是否超限，触发压缩
            if self.enable_compression:
                threshold_tokens = int(safe_context * self.compression_threshold)

                if current_tokens > threshold_tokens:
                    current_k = current_tokens / 1000
                    threshold_k = threshold_tokens / 1000
                    logger.warning(
                        "🚨 上下文超限: {:.1f}K tokens > {:.1f}K tokens "
                        "(阈值 {}%)，启动压缩...",
                        current_k,
                        threshold_k,
                        self.compression_threshold * 100,
                    )

                    messages = (
                        await self.context_compressor.compress_conversation_history(
                            messages,
                            system_prompt,
                            threshold_tokens,
                            tracker=tracker,
                        )
                    )

                    # 压缩后再次记录上下文使用率
                    post_compress_tokens = self.context_compressor.estimate_messages_tokens(
                        messages
                    )
                    tracker.log_context_usage(
                        post_compress_tokens, safe_context, iteration
                    )

        # 达到最大迭代次数，引导 AI 基于已有信息交付最终审查结果
        logger.warning(
            "达到最大工具调用次数 ({})，引导 AI 交付最终审查结果",
            max_iterations,
        )
        messages.append(
            {
                "role": "user",
                "content": (
                    "已达到最大工具调用次数，请基于你当前已掌握的所有信息，"
                    "立即返回最终的代码审查结果。"
                ),
            }
        )
        last_response = await self.api_client.call_with_retry(
            model=settings.openai_model,
            messages=messages,
            temperature=settings.openai_temperature,
        )
        tracker.accumulate(last_response)
        review_text = last_response.choices[0].message.content or ""
        result = self.result_parser.parse_review_result(review_text, strategy)
        result["token_usage"] = tracker.to_dict()
        return result

    async def review_pr_with_tools(
        self,
        context: Dict[str, Any],
        strategy: str,
        repo: Any,
        pr: Any,
        event_callback: Optional[Callable[[str, Dict[str, Any]], Coroutine]] = None,
    ) -> Dict[str, Any]:
        """使用函数工具审查 PR（唯一审查入口）

        所有 PR 统一走此方法：初始 prompt 不包含完整 diff，
        AI 通过 get_file_diff / list_changed_files / read_file 工具自主查看变更。

        Args:
            context: 审查上下文
            strategy: 审查策略
            repo: GitHub仓库对象
            pr: GitHub PR对象
            event_callback: 可选事件回调，用于实时推送工具调用进度到前端。

        Returns:
            审查结果字典
        """
        if self.tool_handler.fetch_url_tool:
            await self.tool_handler.fetch_url_tool.reset_session()
        try:
            file_count = len(context.get("files", []))
            logger.info(
                "开始AI审查（工具驱动模式），策略: {}，文件数: {}",
                strategy,
                file_count,
            )

            strategy_config_data = get_strategy_config().get_strategy(strategy)
            output_lang = await get_user_dynamic_config(
                "output_language", context.get("user_id")
            )
            system_prompt = self.prompt_builder.build_system_prompt(
                strategy_config_data.get("prompt", ""),
                context,
                include_tools=True,
                output_language=output_lang or "",
            )
            tracker = TokenTracker()

            # 始终使用精简模式：只列文件清单，不嵌入 diff
            user_message = self.prompt_builder.build_user_message(
                context, strategy, include_tools=True, compact=True
            )

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ]

            # 动态获取启用的工具列表（已包含 diff 工具）
            if (
                not repo
                or not hasattr(repo, "owner")
                or not repo.owner
                or not repo.name
            ):
                logger.warning("无效的 repo 对象，使用默认工具")
                enabled_tools = await self.tool_manager.get_enabled_tools(None)
            else:
                repo_full_name = f"{repo.owner.login}/{repo.name}"
                enabled_tools = await self.tool_manager.get_enabled_tools(
                    repo_full_name
                )

            # 初始化 DiffToolHandler，加载文件 diff 数据
            diff_tool = DiffToolHandler()
            diff_tool.set_files_data(context.get("files", []))
            if diff_tool.has_data:
                # 注册 diff 工具到工具处理器
                active_tool_handler = build_tool_handler_with_diff(
                    self.tool_handler, diff_tool
                )
                logger.info(
                    "已加载 {} 个文件的 diff 数据，AI 将通过工具按需查看",
                    file_count,
                )
            else:
                active_tool_handler = self.tool_handler
                logger.warning("没有文件 diff 数据可用")

            try:
                return await self._run_tool_loop(
                    messages=messages,
                    system_prompt=system_prompt,
                    strategy=strategy,
                    enabled_tools=enabled_tools,
                    repo=repo,
                    pr=pr,
                    tracker=tracker,
                    context=context,
                    tool_handler=active_tool_handler,
                    event_callback=event_callback,
                )

            except PromptTooLongError as e:
                logger.warning(
                    "🚨 Prompt 超出模型上下文限制 (估算 ~{} tokens, 模型: {})",
                    e.estimated_tokens,
                    e.model,
                )
                # 尝试压缩后重试
                if self.enable_compression:
                    settings = get_settings()
                    safe_context = self.model_context_mgr.calculate_safe_context(
                        settings.openai_model, settings.context_safety_threshold
                    )
                    threshold_tokens = int(
                        safe_context * self.compression_threshold
                    )
                    compressed_messages = (
                        await self.context_compressor.compress_conversation_history(
                            messages,
                            system_prompt,
                            threshold_tokens,
                            tracker=tracker,
                        )
                    )
                    # 重新加载 diff 数据（前一次 clear 可能已清空）
                    diff_tool.set_files_data(context.get("files", []))
                    return await self._run_tool_loop(
                        messages=compressed_messages,
                        system_prompt=system_prompt,
                        strategy=strategy,
                        enabled_tools=enabled_tools,
                        repo=repo,
                        pr=pr,
                        tracker=tracker,
                        context=context,
                        tool_handler=active_tool_handler,
                        event_callback=event_callback,
                    )
                logger.error(
                    "🚨 上下文超限但压缩未启用 (估算 ~{} tokens)",
                    e.estimated_tokens,
                )
                raise

            finally:
                diff_tool.clear()

        except Exception as e:
            logger.error("AI审查（带工具）时出错: {}", str(e), exc_info=True)
            raise

    async def review_file(
        self, file_path: str, patch: str, strategy: str
    ) -> Dict[str, Any]:
        """审查单个文件

        Args:
            file_path: 文件路径
            patch: 文件patch
            strategy: 审查策略

        Returns:
            审查结果字典
        """
        try:
            settings = get_settings()
            strategy_config_data = get_strategy_config().get_strategy(strategy)
            system_prompt = strategy_config_data.get("prompt", "")

            # 构建文件审查消息
            user_message = f"""请审查以下文件的代码变更：

文件: {file_path}

```diff
{patch}
```

请指出潜在的问题和改进建议。"""

            # 调用AI API
            response = await self.api_client.call_with_retry(
                model=settings.openai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=settings.openai_temperature,
            )

            review_text = response.choices[0].message.content

            return {"file_path": file_path, "review": review_text}

        except Exception as e:
            logger.error("审查文件 {} 时出错: {}", file_path, str(e))
            return {"file_path": file_path, "review": f"审查失败: {str(e)}"}

    async def recommend_labels(
        self,
        context: Dict[str, Any],
        available_labels: Dict[str, Dict[str, Any]],
        pr_info: Dict[str, Any],
        existing_labels: List[str] | None = None,
    ) -> List[Dict[str, Any]]:
        """推荐PR标签

        Args:
            context: 审查上下文
            available_labels: 可用的标签字典
            pr_info: PR信息（包含标题、描述等）
            existing_labels: PR 已有的标签名称列表（用于增量审查时避免冲突）

        Returns:
            推荐标签列表，格式：[{"name": str, "confidence": float, "reason": str}]
        """
        return await self.label_recommender.recommend_labels(
            context, available_labels, pr_info, existing_labels=existing_labels
        )
