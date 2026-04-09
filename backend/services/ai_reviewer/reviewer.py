"""重构后的AI审查器主类

这是重构后的主入口，通过组合各个功能模块来实现原有的功能。
保持与原 ai_reviewer.py 中 AIReviewer 类相同的公共接口。
"""

from typing import Any, Dict, List

from loguru import logger

from backend.core.config import get_settings, get_strategy_config
from backend.core.model_context import get_model_context_manager

from .api_client import AIApiClient
from .batch_processor import BatchProcessor
from .compression import ContextCompressor
from .constants import (
    MAX_FILES_PER_BATCH,
    MAX_LINES_PER_BATCH,
    MAX_TOOL_ITERATIONS,
)
from .label_recommender import LabelRecommender
from .prompt_builder import PromptBuilder
from .result_parser import ReviewResultParser
from .token_tracker import TokenTracker
from .tools import FileToolHandler, SearchToolHandler, ToolHandler, ToolManager


class AIReviewer:
    """AI审查器 - 组合各功能模块

    重构后的主类，通过组合各个专门模块来实现功能：
    - AIApiClient: AI API 调用
    - PromptBuilder: 提示词构建
    - ReviewResultParser: 结果解析
    - BatchProcessor: 批处理逻辑
    - ToolHandler/ToolManager: 工具管理
    - ContextCompressor: 上下文压缩
    - LabelRecommender: 标签推荐

    公共接口保持与原 AIReviewer 类完全兼容。
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
        self.batch_processor = BatchProcessor(
            api_client=self.api_client,
            prompt_builder=self.prompt_builder,
            result_parser=self.result_parser,
        )

        # 初始化工具相关
        file_tool = FileToolHandler()
        search_tool = SearchToolHandler()
        web_search_tool = None
        if settings.web_search_enabled:
            from backend.services.ai_reviewer.tools.web_search_tool import (
                WebSearchToolHandler,
            )

            web_search_tool = WebSearchToolHandler()
        self.tool_handler = ToolHandler(file_tool, search_tool, web_search_tool)
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
            logger.info(f"开始AI审查，策略: {strategy}")

            settings = get_settings()
            strategy_config_data = get_strategy_config().get_strategy(strategy)
            system_prompt = strategy_config_data.get("prompt", "")
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

            logger.info(f"AI审查完成，策略: {strategy}")
            return result

        except Exception as e:
            logger.error(f"AI审查时出错: {e}", exc_info=True)
            raise

    async def review_pr_with_tools(
        self, context: Dict[str, Any], strategy: str, repo: Any, pr: Any
    ) -> Dict[str, Any]:
        """使用函数工具审查PR，支持AI主动查看文件

        Args:
            context: 审查上下文
            strategy: 审查策略
            repo: GitHub仓库对象
            pr: GitHub PR对象

        Returns:
            审查结果字典
        """
        try:
            logger.info(f"开始AI审查（带工具支持），策略: {strategy}")

            settings = get_settings()
            strategy_config_data = get_strategy_config().get_strategy(strategy)
            system_prompt = self.prompt_builder.build_system_prompt(
                strategy_config_data.get("prompt", ""), context, include_tools=True
            )
            tracker = TokenTracker()

            # 构建用户消息
            user_message = self.prompt_builder.build_user_message(
                context, strategy, include_tools=True
            )

            # 初始化消息列表
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ]

            # 动态获取启用的工具列表
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

            # 多轮对话循环
            max_iterations = get_strategy_config().get_context_enhancement_config().get(
                "max_tool_iterations", MAX_TOOL_ITERATIONS
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

                # 检查是否有工具调用
                tool_calls = response.choices[0].message.tool_calls

                if not tool_calls:
                    # AI完成了审查，返回结果
                    review_text = response.choices[0].message.content
                    result = self.result_parser.parse_review_result(
                        review_text, strategy
                    )
                    result["token_usage"] = tracker.to_dict()
                    logger.info(
                        f"AI审查完成（使用了{iteration}轮对话），策略: {strategy}"
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

                # 执行每个工具调用
                import json

                for tool_call in tool_calls:
                    try:
                        result = await self.tool_handler.handle_tool_call(
                            tool_call, repo, pr
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": json.dumps(result, ensure_ascii=False),
                            }
                        )
                        logger.info(
                            f"执行工具 {tool_call.function.name}: {tool_call.function.arguments}"
                        )
                    except Exception as e:
                        logger.error(f"执行工具 {tool_call.function.name} 失败: {e}")
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": json.dumps({"error": str(e)}),
                            }
                        )

                # 检查上下文是否超限，触发压缩
                if self.enable_compression:
                    current_tokens = self.context_compressor.estimate_messages_tokens(
                        messages
                    )
                    safe_context = self.model_context_mgr.calculate_safe_context(
                        settings.openai_model, settings.context_safety_threshold
                    )
                    threshold_tokens = int(safe_context * self.compression_threshold)

                    if current_tokens > threshold_tokens:
                        current_k = current_tokens / 1000
                        threshold_k = threshold_tokens / 1000
                        logger.warning(
                            f"🚨 上下文超限: {current_k:.1f}K tokens > {threshold_k:.1f}K tokens "
                            f"(阈值 {self.compression_threshold * 100}%)，启动压缩..."
                        )

                        messages = (
                            await self.context_compressor.compress_conversation_history(
                                messages, system_prompt, threshold_tokens,
                                tracker=tracker,
                            )
                        )

                        logger.info("✅ 压缩完成，继续审查...")

            # 达到最大迭代次数，引导 AI 基于已有信息交付最终审查结果
            logger.warning(
                f"达到最大工具调用次数 ({max_iterations})，引导 AI 交付最终审查结果"
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
            review_text = last_response.choices[0].message.content
            result = self.result_parser.parse_review_result(review_text, strategy)
            result["token_usage"] = tracker.to_dict()
            return result

        except Exception as e:
            logger.error(f"AI审查（带工具）时出错: {e}", exc_info=True)
            raise

    async def review_pr_with_tools_batched(
        self,
        context: Dict[str, Any],
        strategy: str,
        repo: Any,
        pr: Any,
        max_files_per_batch: int = MAX_FILES_PER_BATCH,
        max_lines_per_batch: int = MAX_LINES_PER_BATCH,
    ) -> Dict[str, Any]:
        """使用函数工具审查PR，支持分批处理大型PR

        对于大型PR（文件数或行数超过阈值），自动启用分批模式

        Args:
            context: 审查上下文
            strategy: 审查策略
            repo: GitHub仓库对象
            pr: GitHub PR对象
            max_files_per_batch: 每批最大文件数
            max_lines_per_batch: 每批最大行数

        Returns:
            审查结果字典
        """
        try:
            files = context.get("files", [])

            # 判断是否需要分批
            if len(files) <= max_files_per_batch:
                # 小PR，直接审查（使用AI工具）
                logger.info(
                    f"PR规模较小 ({len(files)} 个文件)，使用标准审查模式（启用AI工具）"
                )
                return await self.review_pr_with_tools(context, strategy, repo, pr)

            # 大PR，启用分批模式
            strategy_config_instance = get_strategy_config()
            enable_tools_in_batch = (
                strategy_config_instance.get_context_enhancement_config().get(
                    "enable_ai_tools_in_batch", True
                )
            )

            if enable_tools_in_batch:
                logger.info(
                    f"🚨 PR规模较大 ({len(files)} 个文件)，启用分批审查模式 "
                    f"（启用AI工具，依赖自动上下文压缩）"
                )
            else:
                logger.warning(
                    f"🚨 PR规模较大 ({len(files)} 个文件)，启用分批审查模式 "
                    f"（禁用AI工具，仅基于patch审查）"
                )

            # 将文件分批
            batches = self.batch_processor.split_files_into_batches(
                files, max_files_per_batch, max_lines_per_batch
            )

            logger.info(
                f"🚀 启动MapReduce模式：{len(batches)} 个批次并行审查（并发限制: 2）"
            )

            # Map阶段：并行审查各批次
            batch_results = await self.batch_processor.review_batches_parallel(
                batches,
                context,
                strategy,
                repo,
                pr,
                enable_tools_in_batch,
                self.tool_handler,
                self.tool_manager,
            )

            # Reduce阶段：AI智能总结
            if len(batches) > 1:
                logger.info("🧠 启动Reduce阶段：AI智能总结中...")
                merged_result = await self.batch_processor.ai_reduce_results(
                    batch_results, strategy, context, pr
                )
            else:
                # 单批次直接使用结果
                merged_result = (
                    batch_results[0]
                    if not isinstance(batch_results[0], Exception)
                    else {
                        "summary": "审查失败",
                        "comments": [],
                        "inline_comments": [],
                        "overall_score": None,
                    }
                )

            # 合并所有批次的 token 消耗
            final_tracker = TokenTracker()
            for batch_result in batch_results:
                if isinstance(batch_result, dict) and "token_usage" in batch_result:
                    final_tracker.merge(
                        TokenTracker.from_dict(batch_result["token_usage"])
                    )
            # Reduce 阶段的 token（如果有的话）
            if isinstance(merged_result, dict) and "token_usage" in merged_result:
                reduce_tracker = TokenTracker.from_dict(merged_result["token_usage"])
                final_tracker.merge(reduce_tracker)
                del merged_result["token_usage"]
            merged_result["token_usage"] = final_tracker.to_dict()

            logger.info(
                f"分批审查完成: {len(batches)} 个批次, "
                f"{len(merged_result.get('comments', []))} 条整体评论, "
                f"{len(merged_result.get('inline_comments', []))} 条行内评论"
            )

            return merged_result

        except Exception as e:
            logger.error(f"分批审查失败: {str(e)}", exc_info=True)
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
            logger.error(f"审查文件 {file_path} 时出错: {e}")
            return {"file_path": file_path, "review": f"审查失败: {str(e)}"}

    async def recommend_labels(
        self,
        context: Dict[str, Any],
        available_labels: Dict[str, Dict[str, Any]],
        pr_info: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """推荐PR标签

        Args:
            context: 审查上下文
            available_labels: 可用的标签字典
            pr_info: PR信息（包含标题、描述等）

        Returns:
            推荐标签列表，格式：[{"name": str, "confidence": float, "reason": str}]
        """
        return await self.label_recommender.recommend_labels(
            context, available_labels, pr_info
        )
