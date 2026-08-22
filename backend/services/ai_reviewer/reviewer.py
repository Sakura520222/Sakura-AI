"""重构后的AI审查器主类

这是重构后的主入口，通过组合各个功能模块来实现原有的功能。
保持与原 ai_reviewer.py 中 AIReviewer 类相同的公共接口。
"""

import asyncio
import json
import re
from collections.abc import Callable, Coroutine
from typing import Any

from loguru import logger

from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.core.config import (
    get_dynamic_config,
    get_settings,
    get_strategy_config,
    get_user_dynamic_config,
)
from backend.core.model_context import get_model_context_manager
from backend.core.time_service import filename_timestamp
from backend.services.activity_observability.publication_service import (
    coordinate_publication,
)
from backend.services.ai_task_deadline import AITaskDeadline
from backend.services.protocol_repair import run_protocol_repair_loop

from .api_client import AIApiClient, AIEmptyResponseError, PromptTooLongError
from .compact_diff import build_tool_handler_with_diff
from .compression import ContextCompressor
from .label_recommender import LabelRecommender
from .prompt_builder import PromptBuilder
from .result_parser import ReviewResultParser
from .review_protocol import (
    REPAIR_INSTRUCTION,
    ReviewProtocolError,
    safe_protocol_failure,
)
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

PendingUserMessageCallback = Callable[[], Coroutine[Any, Any, dict[str, Any] | None]]


def _dump_protocol_failure(strategy: str, review_text: str) -> None:
    """Persist the full malformed protocol payload for offline diagnosis.

    The warning log only keeps an 80-char prefix/suffix, which has repeatedly
    been too little to root-cause why a model output broke the tagged protocol.
    Writing the whole payload lets the exact failure mode be determined after
    the fact instead of guessing from truncated snippets.
    """
    try:
        from pathlib import Path

        dump_dir = Path("logs") / "protocol_failures"
        dump_dir.mkdir(parents=True, exist_ok=True)
        timestamp = filename_timestamp()
        path = dump_dir / f"{timestamp}_{strategy}.txt"
        path.write_text(review_text, encoding="utf-8")
        logger.warning("已保存完整协议失败载荷（{} 字符）到 {}", len(review_text), path)
    except Exception as exc:
        logger.warning("保存协议失败载荷失败: {}", exc)


def _coerce_tool_call_to_dict(tc: Any) -> dict[str, Any]:
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


def _normalize_tool_calls_inplace(messages: list[dict[str, Any]]) -> None:
    """把 messages 中所有 tool_calls 原地规范化为标准 OpenAI dict。"""
    for message in messages:
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            message["tool_calls"] = [_coerce_tool_call_to_dict(tc) for tc in tool_calls]


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

        # 初始化各组件。端点、凭据和模型均由角色门面按请求解析。
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

        # 初始化上下文压缩。实际模型由 main 角色绑定解析。
        self.enable_compression = settings.enable_context_compression
        self.compression_threshold = settings.context_compression_threshold
        self.keep_rounds = settings.context_compression_keep_rounds
        self.context_compressor = ContextCompressor(
            api_client=self.api_client,
            model="",
            keep_rounds=self.keep_rounds,
        )
        self.model_context_mgr = get_model_context_manager()

        # 初始化标签推荐
        self.label_recommender = LabelRecommender(
            api_client=self.summary_api_client,
            prompt_builder=self.prompt_builder,
            result_parser=self.result_parser,
            model="",
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
        """刷新角色驱动的 AI 门面，不读取旧的扁平供应商配置。"""
        if not hasattr(self, "api_client"):
            self.api_client = AIApiClient()
        self.summary_api_client = self.api_client

        if hasattr(self, "context_compressor"):
            self.context_compressor.api_client = self.api_client
            self.context_compressor.model = ""
        if hasattr(self, "label_recommender"):
            self.label_recommender.api_client = self.summary_api_client
            self.label_recommender.model = ""

    async def _parse_or_repair_review(
        self,
        review_text: str,
        messages: list[dict[str, Any]],
        strategy: str,
        tracker: TokenTracker,
        event_callback: Callable[[str, dict[str, Any]], Coroutine] | None = None,
        invocation_context: Any = None,
        observer: Any = None,
        cancel_event: asyncio.Event | None = None,
        deadline: AITaskDeadline | None = None,
    ) -> dict[str, Any]:
        """Parse a final review; delegate to the shared repair loop on failure."""
        # 注意：不在此推送 final assistant turn——_run_tool_loop 两个退出分支
        # （无工具分支 / finalize 分支）已推送，此处补推会造成同一条 assistant
        # 消息被推 2 次（Canonical Transcript 重复）。
        try:
            max_attempts = int(
                await get_dynamic_config("protocol_repair_max_attempts") or 3
            )
        except ValueError, TypeError:
            max_attempts = 3

        # helper 只把 first_error 传给 on_parse_failure 回调，review_text/strategy
        # 不可见，故用闭包捕获两者，复用模块级落盘函数（完整载荷诊断）。
        async def _dump_failure(_error: BaseException) -> None:
            _dump_protocol_failure(strategy, review_text)

        return await run_protocol_repair_loop(
            parse_fn=lambda text: self.result_parser.parse_review_result(
                text, strategy
            ),
            error_type=ReviewProtocolError,
            base_messages=messages,
            final_text=review_text,
            repair_instruction=REPAIR_INSTRUCTION,
            api_client=self.api_client,
            tracker=tracker,
            max_attempts=max_attempts,
            fallback_result_fn=safe_protocol_failure,
            log_label="审查",
            sse_channel="review:protocol_repair",
            invocation_context=invocation_context,
            observer=observer,
            event_callback=event_callback,
            on_repaired=self._check_finding_consistency,
            on_parse_failure=_dump_failure,
            cancel_event=cancel_event,
            deadline=deadline,
        )

    @staticmethod
    async def _check_finding_consistency(
        original_text: str, repaired_text: str, result: dict[str, Any]
    ) -> None:
        """修复后的 finding 数量与原始输出的标签数对比——仅警告，不阻断。"""
        # 语义与旧实现一致：原始 review_text 的 <FINDING> 标签数 vs 修复后解析的
        # comment 数。修复丢弃 finding（或凭空增加）时告警。
        original_tag_count = sum(
            line.strip() == "<FINDING>" for line in original_text.splitlines()
        )
        comment_count = len(result["comments"]) + len(result["inline_comments"])
        if comment_count != original_tag_count:
            logger.warning(
                "审查协议修复后的 finding 数量与原始标签数不一致: original_tags={} repaired={}",
                original_tag_count,
                comment_count,
            )

    async def review_pr(
        self,
        context: dict[str, Any],
        strategy: str,
        cancel_event: asyncio.Event | None = None,
        publication_coordinator: Any = None,
        invocation_context: Any = None,
        observer: Any = None,
        deadline: AITaskDeadline | None = None,
    ) -> dict[str, Any]:
        """审查PR（标准模式，不使用工具）

        Args:
            context: 审查上下文
            strategy: 审查策略

        Returns:
            审查结果字典
        """
        task_deadline = deadline or AITaskDeadline.from_timeout(
            get_settings().review_timeout_seconds
        )
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
            call_kwargs = task_deadline.prepare_call(messages)
            response = await self.api_client.call_with_retry(
                model="",
                messages=messages,
                temperature=settings.ai_temperature,
                role="main",
                cancel_event=cancel_event,
                context=invocation_context,
                observer=observer,
                **call_kwargs,
            )
            tracker.accumulate(response)

            # 解析结果
            review_text = response.choices[0].message.content
            result = await self._parse_or_repair_review(
                review_text,
                messages,
                strategy,
                tracker,
                invocation_context=invocation_context,
                observer=observer,
                cancel_event=cancel_event,
                deadline=task_deadline,
            )
            result["token_usage"] = tracker.to_dict()
            if publication_coordinator is not None and invocation_context is not None:
                result = await coordinate_publication(
                    publication_coordinator,
                    kind="review",
                    result=result,
                    context=invocation_context,
                )
            logger.info("AI审查完成，策略: {}", strategy)
            return result

        except Exception as e:
            logger.error("AI审查时出错: {}", str(e), exc_info=True)
            raise

    async def _run_tool_loop(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
        strategy: str,
        enabled_tools: list[Any],
        repo: Any,
        pr: Any,
        tracker: TokenTracker,
        context: dict[str, Any],
        tool_handler: ToolHandler | None = None,
        event_callback: Callable[[str, dict[str, Any]], Coroutine] | None = None,
        pending_user_message_callback: PendingUserMessageCallback | None = None,
        cancel_event: asyncio.Event | None = None,
        publication_coordinator: Any = None,
        invocation_context: Any = None,
        observer: Any = None,
        deadline: AITaskDeadline | None = None,
    ) -> dict[str, Any]:
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
        task_deadline = deadline or AITaskDeadline.from_timeout(
            get_settings().review_timeout_seconds
        )
        settings = get_settings()
        active_tool_handler = tool_handler or self.tool_handler
        # 工具循环不再设置轮次上限：依赖模型自然停止（无工具调用即交付），
        # 整体时长由共享 soft deadline 控制；到期只切换下一次调用为最终回答。
        # 优先用新版 unified config 解析的上下文窗口（来自角色绑定模型的内置元数据，
        # 如 deepseek-v4-flash 内置 1M tokens），避免 model_context 在未提供模型名时
        # 回退 128K 兜底（曾导致日志误报 102K 上限、过早触发压缩）。
        (
            _ctx_model_id,
            context_window_tokens,
        ) = await self.api_client.resolve_role_model_context("main")
        if context_window_tokens and context_window_tokens > 0:
            safe_context = int(
                context_window_tokens * settings.context_safety_threshold
            )
        else:
            safe_context = self.model_context_mgr.calculate_safe_context(
                None, settings.context_safety_threshold
            )
        # 增量审查恢复的历史 tool_calls 可能是字符串（checkpoint 持久化损坏），
        # 发送给 AI 前统一规范化为标准 dict，避免上游反序列化失败（400）
        _normalize_tool_calls_inplace(messages)
        iteration = 0

        async def _append_assistant_tool_turn(tool_calls: list[Any]) -> None:
            """Persist an assistant tool-call turn without executing its tools."""
            assistant_message = response.choices[0].message
            assistant_msg_dict = {
                "role": "assistant",
                "content": assistant_message.content,
                "tool_calls": [_coerce_tool_call_to_dict(tc) for tc in tool_calls],
            }
            strategy_config = get_strategy_config()
            if (
                hasattr(assistant_message, "reasoning_content")
                and assistant_message.reasoning_content
                and strategy_config.is_model_supports_reasoning_content("")
            ):
                assistant_msg_dict["reasoning_content"] = (
                    assistant_message.reasoning_content
                )
            messages.append(assistant_msg_dict)
            if event_callback:
                try:
                    await event_callback("message", assistant_msg_dict)
                except Exception as exc:
                    logger.warning("event_callback failed: {}", exc)

        while True:
            iteration += 1

            # 取消信号：PR 关闭等外部信号已触发时，立即中止工具循环
            if cancel_event is not None and cancel_event.is_set():
                raise ReviewCancelledError()

            await self._append_pending_user_message_if_any(
                messages,
                pending_user_message_callback,
                event_callback,
            )

            # 调用AI API。软 deadline 只在下一次调用前切换成最终回答模式，
            # 不取消已经在 provider 中执行的请求。
            prompt_was_sent = task_deadline.timeout_prompt_sent
            call_kwargs = {
                "model": "",
                "messages": messages,
                "tools": enabled_tools,
                "tool_choice": "auto",
                "temperature": settings.ai_temperature,
                "role": "main",
                "cancel_event": cancel_event,
                "context": invocation_context,
                "observer": observer,
            }
            call_kwargs.update(task_deadline.prepare_call(messages))
            if (
                not prompt_was_sent
                and task_deadline.timeout_prompt_sent
                and event_callback
            ):
                try:
                    await event_callback("message", messages[-1])
                except Exception as exc:
                    logger.warning("event_callback failed: {}", exc)
            response = await self.api_client.call_with_retry(**call_kwargs)
            tracker.accumulate(response)
            reported_context_tokens = tracker.log_context_usage(
                response,
                context_window_tokens,
                iteration,
            )
            response_meta = getattr(response, "meta", None)
            reported_context_window = (
                getattr(
                    response_meta,
                    "context_window_tokens",
                    None,
                )
                or context_window_tokens
            )

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
                        await event_callback(
                            "message",
                            {
                                "role": "assistant",
                                "content": review_text,
                            },
                        )
                    except Exception as exc:
                        logger.warning("event_callback failed: {}", exc)
                result = await self._parse_or_repair_review(
                    review_text,
                    messages,
                    strategy,
                    tracker,
                    event_callback,
                    invocation_context=invocation_context,
                    observer=observer,
                    cancel_event=cancel_event,
                    deadline=task_deadline,
                )
                result["token_usage"] = tracker.to_dict()
                logger.info(
                    "AI审查完成（使用了{}轮对话），策略: {}",
                    iteration,
                    strategy,
                )
                return result

            # 即使 provider 在 deadline 前开始、在 deadline 后返回了 tool call，
            # 也不能执行该工具。把这轮 assistant 请求保留到累计上下文，
            # 下一轮会追加一次 timeout prompt 并以 tools=[] 收尾。
            if task_deadline.tools_disabled:
                await _append_assistant_tool_turn(tool_calls)
                review_text = response.choices[0].message.content or ""
                result = await self._parse_or_repair_review(
                    review_text,
                    messages,
                    strategy,
                    tracker,
                    event_callback,
                    invocation_context=invocation_context,
                    observer=observer,
                    cancel_event=cancel_event,
                    deadline=task_deadline,
                )
                result["token_usage"] = tracker.to_dict()
                logger.info(
                    "AI审查在软超时后完成最终回答（使用了{}轮对话），策略: {}",
                    iteration,
                    strategy,
                )
                return result

            if task_deadline.is_expired():
                await _append_assistant_tool_turn(tool_calls)
                continue

            # 处理工具调用
            await _append_assistant_tool_turn(tool_calls)

            # 执行每个工具调用
            for tool_call in tool_calls:
                if task_deadline.is_expired():
                    skipped_tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(
                            {
                                "error": (
                                    "Task deadline reached; this tool call was "
                                    "not executed."
                                )
                            }
                        ),
                    }
                    messages.append(skipped_tool_msg)
                    if event_callback:
                        try:
                            await event_callback("message", skipped_tool_msg)
                        except Exception as exc:
                            logger.warning("event_callback failed: {}", exc)
                    continue
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

            # 本地估算仅用于预测下一次发送前是否应压缩；不得展示为
            # Provider 精确上下文使用量。
            estimated_message_tokens = self.context_compressor.estimate_messages_tokens(
                messages
            )

            # 通知 Check Run：本轮进度快照（轮次/工具调用/Token/上下文/模型）。
            # worker 侧 _review_event_callback 识别 "progress" 事件桥接到 Analysis Check。
            # 异常仅记日志，不中断审查（与现有 "message"/"tool_running" 回调一致）。
            if event_callback:
                try:
                    await event_callback(
                        "progress",
                        {
                            "iteration": iteration,
                            "token_usage": tracker.to_dict(),
                            "current_tokens": reported_context_tokens,
                            "safe_context": reported_context_window,
                            "estimated_message_tokens": estimated_message_tokens,
                            "context_source": "provider",
                            "model": "",
                        },
                    )
                except Exception as exc:
                    logger.warning("event_callback progress failed: {}", exc)

            # 检查上下文是否超限，触发压缩
            if self.enable_compression:
                threshold_tokens = int(safe_context * self.compression_threshold)

                if estimated_message_tokens > threshold_tokens:
                    current_k = estimated_message_tokens / 1000
                    threshold_k = threshold_tokens / 1000
                    logger.warning(
                        "🚨 本地上下文估算超限: {:.1f}K tokens > {:.1f}K tokens "
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

                    # 压缩成功后写入可观测性：创建 context_operation + 替换消息行，
                    # 使实时监控显示"上下文操作"、对话流显示压缩后的摘要上下文。
                    # Persist the replacement so the observability timeline and
                    # conversation stream reflect this explicit compression.
                    if observer is not None:
                        record_replacement = getattr(
                            observer, "record_context_replacement", None
                        )
                        if record_replacement is not None:
                            try:
                                await record_replacement(
                                    messages,
                                    trigger_reason="threshold",
                                )
                            except Exception as exc:
                                logger.warning(
                                    "PR 审查压缩可观测性记录失败（不影响审查）: {}",
                                    exc,
                                )

                    # 压缩发生在下一次 Provider 请求前，此时只能本地估算；
                    # 精确值会在下一次响应 usage 中记录。
                    post_compress_tokens = (
                        self.context_compressor.estimate_messages_tokens(messages)
                    )
                    logger.info(
                        "上下文压缩后本地估算: {:,} tokens；精确值等待下一次 "
                        "Provider usage",
                        post_compress_tokens,
                    )

    async def _append_pending_user_message_if_any(
        self,
        messages: list[dict[str, Any]],
        pending_user_message_callback: PendingUserMessageCallback | None,
        event_callback: Callable[[str, dict[str, Any]], Coroutine] | None = None,
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
        messages: list[dict[str, Any]],
        event_callback: Callable[[str, dict[str, Any]], Coroutine] | None = None,
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
        context: dict[str, Any],
        strategy: str,
        repo: Any,
        pr: Any,
        event_callback: Callable[[str, dict[str, Any]], Coroutine] | None = None,
        pending_user_message_callback: PendingUserMessageCallback | None = None,
        initial_messages: list[dict[str, Any]] | None = None,
        cancel_event: asyncio.Event | None = None,
        publication_coordinator: Any = None,
        invocation_context: Any = None,
        observer: Any = None,
        deadline: AITaskDeadline | None = None,
    ) -> dict[str, Any]:
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
        task_deadline = deadline or AITaskDeadline.from_timeout(
            get_settings().review_timeout_seconds
        )
        self._refresh_ai_clients()
        self._refresh_runtime_config()
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
                    cancel_event=cancel_event,
                    publication_coordinator=publication_coordinator,
                    invocation_context=invocation_context,
                    observer=observer,
                    deadline=task_deadline,
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
                    (
                        _ctx_model_id,
                        ctx_tokens,
                    ) = await self.api_client.resolve_role_model_context("main")
                    if ctx_tokens and ctx_tokens > 0:
                        safe_context = int(
                            ctx_tokens * settings.context_safety_threshold
                        )
                    else:
                        safe_context = self.model_context_mgr.calculate_safe_context(
                            None, settings.context_safety_threshold
                        )
                    threshold_tokens = int(safe_context * self.compression_threshold)
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
                        cancel_event=cancel_event,
                        publication_coordinator=publication_coordinator,
                        invocation_context=invocation_context,
                        observer=observer,
                        deadline=task_deadline,
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
    ) -> dict[str, Any]:
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
                model="",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=settings.ai_temperature,
                role="main",
            )

            review_text = response.choices[0].message.content

            return {"file_path": file_path, "review": review_text}

        except Exception as e:
            logger.error("审查文件 {} 时出错: {}", file_path, str(e))
            return {"file_path": file_path, "review": f"审查失败: {e!s}"}

    async def recommend_labels(
        self,
        context: dict[str, Any],
        available_labels: dict[str, dict[str, Any]],
        pr_info: dict[str, Any],
        existing_labels: list[str] | None = None,
        *,
        invocation_context: Any = None,
        observer: Any = None,
        propagate_errors: bool = False,
        event_callback: Any = None,
        deadline: AITaskDeadline | None = None,
    ) -> list[dict[str, Any]]:
        """推荐PR标签

        Args:
            context: 审查上下文
            available_labels: 可用的标签字典
            pr_info: PR信息（包含标题、描述等）
            existing_labels: PR 已有的标签名称列表（用于增量审查时避免冲突）
            invocation_context: 可观测调用上下文
            observer: 可观测模型发送器
            propagate_errors: 是否向上抛出 provider 失败
            event_callback: 标签推荐请求/响应可观测事件回调
            deadline: 主审查任务的共享软 deadline；到期后跳过该辅助输出契约

        Returns:
            推荐标签列表，格式：[{"name": str, "confidence": float, "reason": str}]
        """
        self._refresh_ai_clients()
        self._refresh_runtime_config()
        return await self.label_recommender.recommend_labels(
            context,
            available_labels,
            pr_info,
            existing_labels=existing_labels,
            invocation_context=invocation_context,
            observer=observer,
            propagate_errors=propagate_errors,
            event_callback=event_callback,
            deadline=deadline,
        )
