"""重构后的AI审查器主类

这是重构后的主入口，通过组合各个功能模块来实现原有的功能。
保持与原 ai_reviewer.py 中 AIReviewer 类相同的公共接口。
"""

import json
import re
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
from .review_protocol import (
    REPAIR_INSTRUCTION,
    ReviewProtocolError,
    safe_protocol_failure,
)
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


PendingUserMessageCallback = Callable[
    [], Coroutine[Any, Any, Dict[str, Any] | None]
]


def _dump_protocol_failure(strategy: str, review_text: str) -> None:
    """Persist the full malformed protocol payload for offline diagnosis.

    The warning log only keeps an 80-char prefix/suffix, which has repeatedly
    been too little to root-cause why a model output broke the tagged protocol.
    Writing the whole payload lets the exact failure mode be determined after
    the fact instead of guessing from truncated snippets.
    """
    try:
        from datetime import datetime
        from pathlib import Path

        dump_dir = Path("logs") / "protocol_failures"
        dump_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = dump_dir / f"{timestamp}_{strategy}.txt"
        path.write_text(review_text, encoding="utf-8")
        logger.warning(
            "已保存完整协议失败载荷（{} 字符）到 {}", len(review_text), path
        )
    except Exception as exc:
        logger.warning("保存协议失败载荷失败: {}", exc)


def _coerce_tool_call_to_dict(tc: Any) -> Dict[str, Any]:
    """把 tool_call 统一为 OpenAI 标准 dict 形态。

    tool_calls 可能是 OpenAI SDK 对象（内存新响应）、dict（规范化形态）或字符串
    （checkpoint 经 json.dumps(default=str) 持久化后恢复的损坏形态）。发送给 AI
    API 与持久化前都必须是标准 dict，否则上游反序列化失败。
    """
    if isinstance(tc, dict):
        function = tc.get("function") or {}
        if isinstance(function, dict):
            return {
                "id": tc.get("id", ""),
                "type": tc.get("type") or "function",
                "function": {
                    "name": function.get("name", ""),
                    "arguments": function.get("arguments", ""),
                },
            }
        return {
            "id": tc.get("id", ""),
            "type": tc.get("type") or "function",
            "function": {"name": str(function), "arguments": ""},
        }
    if isinstance(tc, str):
        # Checkpoint 经 json.dumps(default=str) 持久化后，SDK tool_call 对象会变成
        # 其 repr 字符串（如 "ChatCompletionMessageToolCall(id='call_x', ...)"）。
        # 尽力从中恢复 id 与 function.name，避免与后续 tool 消息的 tool_call_id
        # 失配导致增量审查请求被上游拒绝（400）。无法解析时回退空 id。
        recovered_id = ""
        recovered_name = ""
        id_match = re.search(r"\bid\s*=\s*['\"]([^'\"]*)['\"]", tc)
        if id_match:
            recovered_id = id_match.group(1)
        name_match = re.search(r"\bname\s*=\s*['\"]([^'\"]*)['\"]", tc)
        if name_match:
            recovered_name = name_match.group(1)
        return {
            "id": recovered_id,
            "type": "function",
            "function": {"name": recovered_name, "arguments": ""},
        }
    function = getattr(tc, "function", None)
    return {
        "id": getattr(tc, "id", "") or "",
        "type": getattr(tc, "type", None) or "function",
        "function": {
            "name": getattr(function, "name", "") if function is not None else "",
            "arguments": getattr(function, "arguments", "")
            if function is not None
            else "",
        },
    }


def _normalize_tool_calls_inplace(messages: List[Dict[str, Any]]) -> None:
    """把 messages 中所有 tool_calls 原地规范化为标准 OpenAI dict。"""
    for message in messages:
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            message["tool_calls"] = [
                _coerce_tool_call_to_dict(tc) for tc in tool_calls
            ]


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
        self._ai_client_config = None
        self._summary_client_config = None
        self._refresh_ai_clients()
        self.prompt_builder = PromptBuilder()
        self.result_parser = ReviewResultParser()

        # 初始化工具相关
        file_tool = FileToolHandler()
        search_tool = SearchToolHandler()
        git_tool = GitToolHandler()
        search_files_tool = SearchFilesToolHandler()
        sakura_tool = SakuraToolHandler()
        # PR diff 工具（按需查看文件 diff，用于 prompt 精简模式）
        # 注意：每次精简模式会创建临时 DiffToolHandler 实例，避免并发安全问题
        self.tool_handler = ToolHandler(
            file_tool,
            search_tool,
            None,
            git_tool,
            search_files_tool,
            sakura_tool,
            None,
            diff_tool=None,
        )
        # web_search / fetch_url 按配置动态填充，与 _refresh_runtime_config 复用同一逻辑
        self.tool_handler.apply_web_tool_settings(settings)
        self.tool_manager = ToolManager()

        # 初始化上下文压缩
        # 压缩使用主审查 model（settings.openai_model）而非 summary model：
        # _compress_early_history 需要忠实压缩含 tool_call 的多轮对话历史
        # （见 context_compressor._extract_tool_call_fields），summary model 通常
        # 更弱、难以可靠处理 tool_call 结构，故与主审查共用同一 model/客户端。
        self.enable_compression = settings.enable_context_compression
        self.compression_threshold = settings.context_compression_threshold
        self.keep_rounds = settings.context_compression_keep_rounds
        self.context_compressor = ContextCompressor(
            api_client=self.api_client,
            model=settings.openai_model,
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

    def _refresh_runtime_config(self) -> None:
        """刷新不应被长生命周期审查器固化的运行时配置。"""
        settings = get_settings()
        self.enable_compression = settings.enable_context_compression
        self.compression_threshold = settings.context_compression_threshold
        self.keep_rounds = settings.context_compression_keep_rounds
        self.context_compressor.keep_rounds = self.keep_rounds

        self.tool_handler.apply_web_tool_settings(settings)

    def _refresh_ai_clients(self) -> None:
        """刷新动态 AI 配置，避免长生命周期 Worker 持有旧凭据。"""
        settings = get_settings()
        main_config = (settings.openai_api_base, settings.openai_api_key)
        if self._ai_client_config != main_config:
            self.api_client = AIApiClient(
                base_url=settings.openai_api_base,
                api_key=settings.openai_api_key,
            )
            self._ai_client_config = main_config

        # summary_model 不纳入 summary_config 元组：model 是每次调用的入参
        # （call_with_retry(model=...)），与客户端凭据无关，仅在此刷新属性即可，
        # 避免 model 变化时重建客户端；凭据变化仍由下方元组比较触发重建。
        self.summary_model = settings.summary_model or settings.openai_model
        summary_uses_main = (
            not settings.summary_api_base and not settings.summary_api_key
        )
        summary_config = (
            "main",
            main_config,
        ) if summary_uses_main else (
            "custom",
            settings.summary_api_base or settings.openai_api_base,
            settings.summary_api_key or settings.openai_api_key,
        )
        if self._summary_client_config != summary_config:
            if summary_uses_main:
                self.summary_api_client = self.api_client
            else:
                self.summary_api_client = AIApiClient(
                    base_url=settings.summary_api_base or settings.openai_api_base,
                    api_key=settings.summary_api_key or settings.openai_api_key,
                )
            self._summary_client_config = summary_config

        if hasattr(self, "context_compressor"):
            self.context_compressor.api_client = self.api_client
            self.context_compressor.model = settings.openai_model
        if hasattr(self, "label_recommender"):
            self.label_recommender.api_client = self.summary_api_client
            self.label_recommender.model = self.summary_model

    async def _parse_or_repair_review(
        self,
        review_text: str,
        messages: List[Dict[str, Any]],
        strategy: str,
        tracker: TokenTracker,
        event_callback: Optional[Callable[[str, Dict[str, Any]], Coroutine]] = None,
    ) -> Dict[str, Any]:
        """Parse a final review and make one format-only repair attempt if needed."""
        try:
            return self.result_parser.parse_review_result(review_text, strategy)
        except ReviewProtocolError as first_error:
            stripped = review_text.strip()
            _dump_protocol_failure(strategy, review_text)
            logger.warning(
                "审查协议解析失败，尝试修复一次: {} | length={} prefix={!r} suffix={!r}",
                first_error,
                len(review_text),
                stripped[:80],
                stripped[-80:],
            )

        system_message = next(
            (message for message in messages if message.get("role") == "system"),
            None,
        )
        repair_messages = [
            *([system_message] if system_message else []),
            {"role": "assistant", "content": review_text},
            {"role": "user", "content": REPAIR_INSTRUCTION},
        ]
        try:
            settings = get_settings()
            response = await self.api_client.call_with_retry(
                model=settings.openai_model,
                messages=repair_messages,
                temperature=0,
            )
            tracker.accumulate(response)
            repaired_text = response.choices[0].message.content or ""
            if event_callback:
                try:
                    await event_callback(
                        "message",
                        {"role": "assistant", "content": repaired_text},
                    )
                except Exception as exc:
                    logger.warning("event_callback failed: {}", exc)
            result = self.result_parser.parse_review_result(repaired_text, strategy)
            original_finding_count = sum(
                line.strip() == "<FINDING>" for line in review_text.splitlines()
            )
            repaired_finding_count = len(result["comments"]) + len(
                result["inline_comments"]
            )
            if repaired_finding_count != original_finding_count:
                logger.warning(
                    "审查协议修复后的 finding 数量发生变化: original_tags={} repaired_valid={}",
                    original_finding_count,
                    repaired_finding_count,
                )
            return result
        except Exception as repair_error:
            logger.error("审查协议修复失败，降级为人工复审: {}", repair_error)
            return safe_protocol_failure(repair_error)

    async def review_pr(self, context: Dict[str, Any], strategy: str) -> Dict[str, Any]:
        """审查PR（标准模式，不使用工具）

        Args:
            context: 审查上下文
            strategy: 审查策略

        Returns:
            审查结果字典
        """
        try:
            self._refresh_ai_clients()
            self._refresh_runtime_config()
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

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ]

            # 调用AI API
            response = await self.api_client.call_with_retry(
                model=settings.openai_model,
                messages=messages,
                temperature=settings.openai_temperature,
            )
            tracker.accumulate(response)

            # 解析结果
            review_text = response.choices[0].message.content
            result = await self._parse_or_repair_review(
                review_text,
                messages,
                strategy,
                tracker,
            )
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
        pending_user_message_callback: Optional[PendingUserMessageCallback] = None,
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
        # 增量审查恢复的历史 tool_calls 可能是字符串（checkpoint 持久化损坏），
        # 发送给 AI 前统一规范化为标准 dict，避免上游反序列化失败（400）
        _normalize_tool_calls_inplace(messages)
        iteration = 0

        while iteration < max_iterations:
            iteration += 1

            await self._append_pending_user_message_if_any(
                messages,
                pending_user_message_callback,
                event_callback,
            )

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
                result = await self._parse_or_repair_review(
                    review_text,
                    messages,
                    strategy,
                    tracker,
                    event_callback,
                )
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
                "tool_calls": [_coerce_tool_call_to_dict(tc) for tc in tool_calls],
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
                    "Finalize the review now using the system output contract and the "
                    "evidence already gathered. Do not call more tools."
                ),
            }
        )
        await self._append_pending_user_message_if_any(
            messages,
            pending_user_message_callback,
            event_callback,
        )
        last_response = await self.api_client.call_with_retry(
            model=settings.openai_model,
            messages=messages,
            temperature=settings.openai_temperature,
        )
        tracker.accumulate(last_response)
        review_text = last_response.choices[0].message.content or ""
        result = await self._parse_or_repair_review(
            review_text,
            messages,
            strategy,
            tracker,
            event_callback,
        )
        result["token_usage"] = tracker.to_dict()
        return result

    async def _append_pending_user_message_if_any(
        self,
        messages: List[Dict[str, Any]],
        pending_user_message_callback: Optional[PendingUserMessageCallback],
        event_callback: Optional[Callable[[str, Dict[str, Any]], Coroutine]] = None,
    ) -> None:
        """Append a queued incremental user message before an AI request."""
        if not pending_user_message_callback:
            return

        try:
            message = await pending_user_message_callback()
        except Exception as exc:
            logger.warning("pending_user_message_callback failed: {}", exc)
            return

        if not message:
            return
        if message.get("role") != "user":
            logger.warning(
                "pending_user_message_callback returned non-user message: {}",
                message.get("role"),
            )
            return

        messages.append(message)
        if event_callback:
            try:
                await event_callback("message", message)
            except Exception as exc:
                logger.warning("event_callback failed: {}", exc)

    async def _emit_initial_messages(
        self,
        messages: List[Dict[str, Any]],
        event_callback: Optional[Callable[[str, Dict[str, Any]], Coroutine]] = None,
    ) -> None:
        if not event_callback:
            return
        for message in messages:
            try:
                await event_callback("message", message)
            except Exception as exc:
                logger.warning("event_callback failed: {}", exc)

    async def review_pr_with_tools(
        self,
        context: Dict[str, Any],
        strategy: str,
        repo: Any,
        pr: Any,
        event_callback: Optional[Callable[[str, Dict[str, Any]], Coroutine]] = None,
        pending_user_message_callback: Optional[PendingUserMessageCallback] = None,
        initial_messages: Optional[List[Dict[str, Any]]] = None,
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
        self._refresh_ai_clients()
        self._refresh_runtime_config()
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

            current_system_message = {"role": "system", "content": system_prompt}
            current_user_message = {"role": "user", "content": user_message}
            if initial_messages:
                messages = [dict(message) for message in initial_messages]
                # 用当前 system prompt 替换恢复的历史 system 消息，
                # 确保 output_language、strategy 等配置变更在增量审查中生效
                for i, msg in enumerate(messages):
                    if msg.get("role") == "system":
                        messages[i] = current_system_message
                        break
                else:
                    messages.insert(0, current_system_message)
                messages_to_persist = [current_user_message]
                messages.append(current_user_message)
            else:
                messages = [current_system_message, current_user_message]
                messages_to_persist = messages

            await self._emit_initial_messages(messages_to_persist, event_callback)

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
                    pending_user_message_callback=pending_user_message_callback,
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
                        pending_user_message_callback=pending_user_message_callback,
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
            self._refresh_ai_clients()
            self._refresh_runtime_config()
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
        self._refresh_ai_clients()
        self._refresh_runtime_config()
        return await self.label_recommender.recommend_labels(
            context, available_labels, pr_info, existing_labels=existing_labels
        )
