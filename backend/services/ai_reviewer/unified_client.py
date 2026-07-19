"""统一 AI 客户端 / Unified AI client.

按角色（main / summary / agent_team）解析候选链，执行：
- 重试循环（沿用现有退避策略）
- 上下文超限恢复（压缩后同模型重试）
- 跨协议回退（重试耗尽后切下一候选）
- 归一化响应与链路追踪

Resolves a candidate chain per role (main / summary / agent_team) and
orchestrates retry, context-overflow recovery, cross-protocol fallback,
normalized responses, and attempt tracing.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

import httpx
from loguru import logger

from backend.core.ai_protocol.errors import (
    AIError,
    AllCandidatesFailedError,
    ContextOverflowError,
    ReviewCancelledError,
)
from backend.core.ai_protocol.models import (
    AIErrorCategory,
    ModelMetadata,
    ResolvedModel,
    UnifiedMessage,
    UnifiedRequest,
    UnifiedResponse,
    UnifiedTool,
)
from backend.core.ai_protocol import registry as _protocol_registry


def _get_adapter(family):
    """通过 registry 模块属性访问，便于测试 monkeypatch / Module-attribute access for testability."""
    return _protocol_registry.get_adapter(family)

# 压缩器延迟导入，避免循环依赖 / lazy import to avoid circular dependency
if TYPE_CHECKING:
    from backend.services.ai_reviewer.compression.unified_compressor import (
        UnifiedContextCompressor,
    )


@dataclass
class FallbackConfig:
    """故障转移配置 / Fallback configuration."""

    enabled: bool = True
    max_candidates: int = 3
    max_retries: int = 3
    total_timeout: float = 600.0
    initial_retry_delay: float = 1.0
    # 是否记忆并优先使用上次成功的候选（按 role）/ prefer last-winning candidate per role
    sticky_candidate: bool = True


@dataclass
class AttemptRecord:
    """单次尝试记录 / A single attempt record."""

    provider: str
    model: str
    category: str
    elapsed: float
    retry: int
    fallback_reason: str = ""


def _filter_params_by_capability(
    metadata: ModelMetadata,
    *,
    temperature: Optional[float],
    top_p: Optional[float],
    top_k: Optional[int],
    thinking: Optional[dict[str, Any]],
    effort: Optional[str],
) -> dict[str, Any]:
    """按模型能力过滤推理参数 / Filter reasoning params by model capability."""
    caps = metadata.capabilities
    params = metadata.reasoning_params
    result: dict[str, Any] = {}

    def _pick(passed: Any, configured: Any, allowed: bool) -> Any:
        if not allowed:
            return None
        return passed if passed is not None else configured

    result["temperature"] = _pick(temperature, params.temperature, caps.temperature)
    result["top_p"] = _pick(top_p, params.top_p, caps.top_p)
    result["top_k"] = _pick(top_k, params.top_k, caps.top_k)
    result["thinking"] = thinking if (thinking is not None and caps.thinking) else (
        params.thinking if caps.thinking else None
    )
    result["effort"] = effort if (effort is not None and caps.effort) else (
        params.effort if caps.effort else None
    )
    return result


def _messages_from_legacy(
    messages: list[dict[str, Any]],
) -> list[UnifiedMessage]:
    """旧版 dict 消息 → UnifiedMessage（向后兼容门面调用）/ Legacy dict → Unified."""
    result: list[UnifiedMessage] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")
        if content is None and role != "assistant":
            content = ""
        tool_calls_raw = msg.get("tool_calls")
        tool_calls = None
        if tool_calls_raw:
            from backend.core.ai_protocol.models import UnifiedToolCall

            converted = []
            for tc in tool_calls_raw:
                function = (
                    tc.get("function") if isinstance(tc, dict) else getattr(tc, "function", None)
                )
                if function is None:
                    continue
                fname = function.get("name") if isinstance(function, dict) else getattr(function, "name", "")
                fargs = (
                    function.get("arguments")
                    if isinstance(function, dict)
                    else getattr(function, "arguments", "")
                )
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "")
                converted.append(
                    UnifiedToolCall(id=tc_id or "", name=fname or "", arguments=fargs or "")
                )
            if converted:
                tool_calls = converted
        result.append(
            UnifiedMessage(
                role=role,
                content=content if isinstance(content, str) else None,
                tool_calls=tool_calls,
                tool_call_id=msg.get("tool_call_id"),
                reasoning_content=msg.get("reasoning_content"),
                name=msg.get("name"),
            )
        )
    return result


def _tools_from_legacy(tools: Optional[list[dict[str, Any]]]) -> Optional[list[UnifiedTool]]:
    """旧版工具 dict → UnifiedTool / Legacy tool dict → UnifiedTool."""
    if not tools:
        return None
    result: list[UnifiedTool] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function":
            function = tool.get("function") or {}
            result.append(
                UnifiedTool(
                    name=function.get("name", ""),
                    description=function.get("description", ""),
                    parameters=function.get("parameters") or {"type": "object", "properties": {}},
                    strict=bool(function.get("strict", False)),
                )
            )
        else:
            result.append(
                UnifiedTool(
                    name=tool.get("name", ""),
                    description=tool.get("description", ""),
                    parameters=tool.get("parameters") or {"type": "object", "properties": {}},
                )
            )
    return result


class UnifiedAIClient:
    """统一 AI 客户端 / Unified AI client.

    用法：
        client = UnifiedAIClient()
        chain = client.resolve_chain(role="main")
        response = await client.call_with_retry(chain, messages, model=..., tools=...)
    """

    def __init__(
        self,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        fallback_config: Optional[FallbackConfig] = None,
        compressor: Optional["UnifiedContextCompressor"] = None,
    ):
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self.fallback_config = fallback_config or FallbackConfig()
        self._compressor = compressor
        # role -> (provider_id, model_id)，记忆上次成功候选
        # remember last-winning candidate per role so subsequent calls in the
        # same review skip the failed-first fallback chain.
        self._last_successful: dict[str, tuple[str, str]] = {}

    async def __aenter__(self) -> "UnifiedAIClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    @property
    def http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(600.0, connect=10.0),
                follow_redirects=False,
            )
        return self._http_client

    def set_compressor(self, compressor: "UnifiedContextCompressor") -> None:
        self._compressor = compressor

    # ------------------------------------------------------------------
    # 核心：带故障转移的调用 / Core: fallback-aware call
    # ------------------------------------------------------------------
    async def call_with_retry(
        self,
        chain_or_candidates: Any,
        messages: list[dict[str, Any]] | list[UnifiedMessage],
        *,
        model: str,
        tools: Optional[list[dict[str, Any]]] | Optional[list[UnifiedTool]] = None,
        tool_choice: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        max_tokens: Optional[int] = None,
        thinking: Optional[dict[str, Any]] = None,
        effort: Optional[str] = None,
        timeout: Optional[float] = None,
        role: str = "main",
        cancel_event: Optional[asyncio.Event] = None,
    ) -> UnifiedResponse:
        """统一调用入口（对外契约与旧 AIApiClient.call_with_retry 对齐）.

        chain_or_candidates: ResolvedChain 或 list[ResolvedModel]。
        messages: 统一消息或旧版 dict（门面兼容）。
        返回 UnifiedResponse（向后兼容 response.choices[0].message.xxx 访问）。
        """
        candidates = self._extract_candidates(chain_or_candidates)
        if not candidates:
            raise AllCandidatesFailedError(
                f"角色 {role} 无可用 AI 候选模型，请检查配置。"
            )

        unified_messages = (
            messages
            if (messages and isinstance(messages[0], UnifiedMessage))
            else _messages_from_legacy(messages)  # type: ignore[arg-type]
        )
        unified_tools = (
            tools
            if (tools and tools and isinstance(tools[0], UnifiedTool))
            else _tools_from_legacy(tools)  # type: ignore[arg-type]
        )

        max_candidates = (
            min(self.fallback_config.max_candidates, len(candidates))
            if self.fallback_config.enabled
            else 1
        )
        selected = candidates[:max_candidates]

        # Sticky candidate: 若该 role 上次有成功候选，提升到首位，避免每轮工具循环
        # 都从首选重新故障转移（首选若已挂会浪费 N×retries 次失败重试）。
        if self.fallback_config.sticky_candidate and role in self._last_successful:
            sticky_key = self._last_successful[role]
            for i, c in enumerate(selected):
                if (c.provider.id, c.model.model_id) == sticky_key:
                    if i > 0:
                        selected = [selected[i], *selected[:i], *selected[i + 1 :]]
                    break

        attempt_chain: list[AttemptRecord] = []
        last_error: Optional[AIError] = None
        compressed_once = False

        for idx, candidate in enumerate(selected):
            # 取消信号：立即中止整条故障转移链 / abort fast on external cancel
            if cancel_event is not None and cancel_event.is_set():
                raise ReviewCancelledError()
            served_by = f"{candidate.provider.id}/{candidate.model.model_id}"
            params = _filter_params_by_capability(
                candidate.model,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                thinking=thinking,
                effort=effort,
            )
            effective_max_tokens = max_tokens or candidate.model.reasoning_params.max_output_tokens

            request = UnifiedRequest(
                model=candidate.model.model_id,
                messages=list(unified_messages),
                max_tokens=effective_max_tokens,
                tools=unified_tools,
                tool_choice=tool_choice,
                temperature=params["temperature"],
                top_p=params["top_p"],
                top_k=params["top_k"],
                thinking=params["thinking"],
                effort=params["effort"],
                stream=False,
            )

            start = time.monotonic()
            try:
                response = await self._retry_candidate(
                    candidate,
                    request,
                    timeout=timeout,
                    role=role,
                    idx=idx,
                    cancel_event=cancel_event,
                )
                response.meta.served_by = served_by
                response.meta.attempt_chain = [a.__dict__ for a in attempt_chain] + [
                    {
                        "provider": candidate.provider.id,
                        "model": candidate.model.model_id,
                        "category": "success",
                        "elapsed": time.monotonic() - start,
                        "retry": 0,
                    }
                ]
                response.meta.compressed = compressed_once
                # 记录该 role 的成功候选，供后续调用 sticky 提升
                self._last_successful[role] = (
                    candidate.provider.id,
                    candidate.model.model_id,
                )
                return response
            except AIError as exc:
                last_error = exc
                attempt_chain.append(
                    AttemptRecord(
                        provider=candidate.provider.id,
                        model=candidate.model.model_id,
                        category=exc.category.value,
                        elapsed=time.monotonic() - start,
                        retry=0,
                        fallback_reason=str(exc)[:200],
                    )
                )
                logger.warning(
                    "AI 候选失败 [{}/{}]: {} | category={} err={}",
                    idx + 1,
                    len(selected),
                    served_by,
                    exc.category.value,
                    exc,
                )

                # 终端错误：直接报出 / terminal errors surface immediately
                if exc.is_terminal:
                    raise

                # 上下文超限：尝试压缩恢复 / context overflow: compress & retry
                if exc.category == AIErrorCategory.CONTEXT_OVERFLOW:
                    recovered = await self._attempt_compress_recovery(
                        candidate=candidate,
                        messages=unified_messages,
                        request=request,
                        remaining=selected[idx + 1 :],
                        attempt_chain=attempt_chain,
                        timeout=timeout,
                        role=role,
                        cancel_event=cancel_event,
                    )
                    if recovered is not None:
                        recovered.meta.compressed = True
                        recovered.meta.served_by = served_by
                        # 压缩重试成功仍属于该候选，记录 sticky
                        self._last_successful[role] = (
                            candidate.provider.id,
                            candidate.model.model_id,
                        )
                        return recovered
                    # 压缩无法恢复，继续回退到下一候选 / continue to next candidate
                    continue

                # 其他可恢复错误：重试耗尽 → 回退下一候选 / exhausted → next candidate
                continue

        # 全部候选失败 / all candidates failed
        if last_error and last_error.category == AIErrorCategory.CONTEXT_OVERFLOW:
            raise ContextOverflowError(
                f"所有候选模型均无法承载当前上下文（尝试 {len(attempt_chain)} 次）",
                attempted_candidates=[a.model for a in attempt_chain],
            )
        raise AllCandidatesFailedError(
            f"角色 {role} 所有候选模型均失败",
            attempts=[a.__dict__ for a in attempt_chain],
        )

    # ------------------------------------------------------------------
    # 单候选重试 / Per-candidate retry loop
    # ------------------------------------------------------------------
    async def _retry_candidate(
        self,
        candidate: ResolvedModel,
        request: UnifiedRequest,
        *,
        timeout: Optional[float],
        role: str,
        idx: int,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> UnifiedResponse:
        adapter = _get_adapter(candidate.provider.family)
        cfg = self.fallback_config
        total_timeout = timeout or cfg.total_timeout
        start = time.monotonic()

        last_exc: Optional[AIError] = None
        for attempt in range(cfg.max_retries):
            # 取消信号：退避中途被取消时立即抛出 / honor cancel between retries
            if cancel_event is not None and cancel_event.is_set():
                raise ReviewCancelledError()
            elapsed = time.monotonic() - start
            if elapsed > total_timeout:
                raise AIError(
                    AIErrorCategory.UNKNOWN,
                    f"AI 调用总超时（{total_timeout}s）",
                    provider=candidate.provider.id,
                    model=candidate.model.model_id,
                )
            try:
                response = await adapter.chat(
                    self.http_client,
                    candidate.endpoint,
                    candidate.credential,
                    request,
                    timeout=timeout,
                )
                return response
            except AIError as exc:
                last_exc = exc
                if exc.is_terminal or not exc.is_retryable:
                    raise
                if attempt < cfg.max_retries - 1:
                    delay = self._calculate_delay(attempt)
                    logger.warning(
                        "AI 调用失败 [{}]: {}，{:.1f}s 后重试 ({}/{}) role={} model={}",
                        exc.category.value,
                        str(exc)[:160],
                        delay,
                        attempt + 1,
                        cfg.max_retries,
                        role,
                        candidate.model.model_id,
                    )
                    await self._abortable_sleep(delay, cancel_event)
                else:
                    raise
        # 不可达 / unreachable
        raise last_exc  # type: ignore[misc]

    @staticmethod
    async def _abortable_sleep(
        delay: float, cancel_event: Optional[asyncio.Event]
    ) -> None:
        """退避等待，cancel_event 被 set 时立即抛出 ReviewCancelledError。

        用 asyncio.wait(FIRST_COMPLETED) 让 sleep 与 event 竞速；先完成的胜出，
        另一个 task 主动 cancel 以避免悬挂。无 cancel_event 时退化为普通 sleep。
        """
        if cancel_event is None:
            await asyncio.sleep(delay)
            return
        sleep_task = asyncio.ensure_future(asyncio.sleep(delay))
        event_task = asyncio.ensure_future(cancel_event.wait())
        try:
            await asyncio.wait(
                {sleep_task, event_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (sleep_task, event_task):
                if not task.done():
                    task.cancel()
        if cancel_event.is_set():
            raise ReviewCancelledError()

    def _calculate_delay(self, attempt: int) -> float:
        """混合退避 + 抖动（沿用旧策略）/ Hybrid backoff with jitter (legacy)."""
        cfg = self.fallback_config
        initial = cfg.initial_retry_delay
        if attempt < 3:
            delay = initial * (2**attempt)
        else:
            delay = initial * 8 * (2 ** (attempt - 3))
        jitter = random.uniform(0.8, 1.2)
        return delay * jitter

    # ------------------------------------------------------------------
    # 上下文超限压缩恢复 / Context-overflow compression recovery
    # ------------------------------------------------------------------
    async def _attempt_compress_recovery(
        self,
        *,
        candidate: ResolvedModel,
        messages: list[UnifiedMessage],
        request: UnifiedRequest,
        remaining: list[ResolvedModel],
        attempt_chain: list[AttemptRecord],
        timeout: Optional[float],
        role: str,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> Optional[UnifiedResponse]:
        """压缩后同候选重试；仍超限则返回 None 交由上层回退.

        Compress then retry the same candidate; if still overflowing, return
        None so the caller falls back to the next candidate.
        """
        if self._compressor is None:
            return None
        try:
            compressed = await self._compressor.compress_for_candidate(
                candidate=candidate,
                messages=messages,
                system=request.system,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("压缩恢复失败: {}", exc)
            return None
        if compressed is None:
            return None

        compressed_request = UnifiedRequest(
            model=request.model,
            messages=compressed,
            max_tokens=request.max_tokens,
            system=request.system,
            tools=request.tools,
            tool_choice=request.tool_choice,
            temperature=request.temperature,
            top_p=request.top_p,
            top_k=request.top_k,
            thinking=request.thinking,
            effort=request.effort,
            stream=False,
        )
        start = time.monotonic()
        try:
            response = await self._retry_candidate(
                candidate,
                compressed_request,
                timeout=timeout,
                role=role,
                idx=0,
                cancel_event=cancel_event,
            )
            response.meta.fallback_reason = "compressed-retry"
            attempt_chain.append(
                AttemptRecord(
                    provider=candidate.provider.id,
                    model=candidate.model.model_id,
                    category="context_overflow_recovered",
                    elapsed=time.monotonic() - start,
                    retry=0,
                    fallback_reason="compressed",
                )
            )
            return response
        except AIError as exc:
            if exc.category == AIErrorCategory.CONTEXT_OVERFLOW:
                logger.info(
                    "压缩后仍超限，回退到容量足够的候选 / still overflowing after compress"
                )
                return None
            raise

    # ------------------------------------------------------------------
    # 辅助 / Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_candidates(chain_or_candidates: Any) -> list[ResolvedModel]:
        """从 ResolvedChain 或 list 提取候选列表 / Extract candidates."""
        from backend.core.ai_protocol.resolver import ResolvedChain

        if isinstance(chain_or_candidates, ResolvedChain):
            return list(chain_or_candidates.candidates)
        if isinstance(chain_or_candidates, list):
            return [c for c in chain_or_candidates if isinstance(c, ResolvedModel)]
        return []

    # ------------------------------------------------------------------
    # 模型发现代理 / Model discovery proxy
    # ------------------------------------------------------------------
    async def discover_models(
        self,
        candidate: ResolvedModel,
    ) -> list[Any]:
        """通过适配器列出模型（供 Setup/config 调用）/ List models via adapter."""
        adapter = _get_adapter(candidate.provider.family)
        return await adapter.list_models(
            self.http_client, candidate.endpoint, candidate.credential
        )

    async def fetch_model_metadata(
        self,
        candidate: ResolvedModel,
        model_id: str,
    ) -> Optional[Any]:
        """通过适配器获取单个模型元数据 / Fetch one model via adapter."""
        adapter = _get_adapter(candidate.provider.family)
        return await adapter.fetch_model_metadata(
            self.http_client, candidate.endpoint, candidate.credential, model_id
        )


__all__ = [
    "UnifiedAIClient",
    "FallbackConfig",
    "AttemptRecord",
]
