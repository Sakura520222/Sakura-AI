"""Adapters' real-send observation boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from loguru import logger

from backend.core.config import get_settings
from backend.services.activity_observability.artifact_projection import (
    projection_json,
)
from backend.services.activity_observability.attempt_service import AttemptService
from backend.services.activity_observability.context_service import (
    AVAILABILITY_ESTIMATED,
    AVAILABILITY_REPORTED,
    AVAILABILITY_UNAVAILABLE,
    SOURCE_CONFIGURATION,
    SOURCE_HEURISTIC,
    SOURCE_MODEL_CATALOG,
    SOURCE_PROVIDER,
    ContextService,
    ContextSnapshotFields,
    MeasuredValue,
    ThreadLeaseToken,
)
from backend.services.activity_observability.contracts import (
    EffectiveReasoningSnapshot,
    InvocationContext,
)
from backend.services.activity_observability.reasoning import (
    REASONING_OMITTED,
    REASONING_PROVIDER_EXPOSED,
    REASONING_SUMMARIZED,
    REASONING_UNAVAILABLE,
    ReasoningCapturePolicy,
    configured_reasoning_capture_policy,
)


class ObservedModelSender:
    """Wrap adapter.chat/stream so each invocation maps to one Attempt row."""

    def __init__(
        self,
        attempt_service: AttemptService,
        *,
        context: InvocationContext | None = None,
        revision_resolver: Callable[[], Awaitable[int | None] | int | None]
        | None = None,
        context_service: ContextService | None = None,
        tool_service: Any = None,
        lease: ThreadLeaseToken | None = None,
    ) -> None:
        self.attempt_service = attempt_service
        self.context = context
        self.revision_resolver = revision_resolver
        self.context_service = context_service
        self.tool_service = tool_service
        self.lease = lease
        self.last_attempt_id: int | None = None

    async def _best_effort(
        self,
        stage: str,
        operation: Callable[[], Awaitable[Any]],
        *,
        default: Any = None,
    ) -> Any:
        """Keep observability failures off the provider request's critical path."""
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.bind(
                observability_stage=stage,
                error_type=type(exc).__name__,
            ).warning(
                "AI 可观测性写入失败，业务调用继续: stage={} error_type={}",
                stage,
                type(exc).__name__,
            )
            return default

    def _log_attempt(
        self,
        event: str,
        *,
        logical_call_id: str,
        attempt: Any,
        attempt_kind: str,
        purpose: str,
        candidate: Any,
        reasoning_snapshot: EffectiveReasoningSnapshot | None,
        elapsed: float | None = None,
        error: BaseException | None = None,
    ) -> None:
        provider = getattr(getattr(candidate, "provider", None), "id", "unknown")
        model = getattr(getattr(candidate, "model", None), "model_id", "unknown")
        role = getattr(
            getattr(self.context, "role_snapshot", None),
            "role",
            purpose,
        )
        attempt_id = getattr(attempt, "id", None)
        attempt_index = getattr(attempt, "attempt_index", None)
        thinking = (
            reasoning_snapshot.effective_thinking_mode
            if reasoning_snapshot is not None
            else getattr(attempt, "effective_thinking_mode", "unavailable")
        )
        effort = (
            reasoning_snapshot.effective_effort
            if reasoning_snapshot is not None
            else getattr(attempt, "effective_effort", "unavailable")
        )
        protocol = (
            reasoning_snapshot.protocol_family
            if reasoning_snapshot is not None
            else getattr(attempt, "protocol_family", "unknown")
        )
        bound = logger.bind(
            logical_call_id=logical_call_id,
            attempt_id=attempt_id,
            attempt_index=attempt_index,
            attempt_kind=attempt_kind,
            role=role,
            purpose=purpose,
            provider=provider,
            model=model,
            protocol=protocol,
            effective_thinking_mode=thinking,
            effective_effort=effort,
        )
        if event == "started":
            bound.info(
                "AI 调用开始: call={} attempt={} index={} kind={} role={} purpose={} "
                "provider={} model={} protocol={} thinking={} effort={}",
                logical_call_id,
                attempt_id,
                attempt_index,
                attempt_kind,
                role,
                purpose,
                provider,
                model,
                protocol,
                thinking,
                effort,
            )
            return
        if event == "completed":
            reasoning_availability = getattr(
                attempt, "reasoning_availability", "unavailable"
            )
            reasoning_tokens = getattr(attempt, "reasoning_tokens", None)
            input_tokens = getattr(attempt, "input_tokens", None)
            output_tokens = getattr(attempt, "output_tokens", None)
            cached_input_tokens = getattr(attempt, "cached_input_tokens", None)
            stop_reason = getattr(attempt, "stop_reason", None)
            bound.bind(
                reasoning_availability=reasoning_availability,
                reasoning_tokens=reasoning_tokens,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached_input_tokens,
                stop_reason=stop_reason,
                elapsed_seconds=elapsed,
            ).info(
                "AI 调用成功: call={} attempt={} provider={} model={} "
                "thinking={} effort={} reasoning={} reasoning_tokens={} "
                "input_tokens={} output_tokens={} cached_input_tokens={} "
                "stop_reason={} elapsed={:.3f}s",
                logical_call_id,
                attempt_id,
                provider,
                model,
                thinking,
                effort,
                reasoning_availability,
                reasoning_tokens,
                input_tokens,
                output_tokens,
                cached_input_tokens,
                stop_reason,
                elapsed or 0.0,
            )
            return
        category = getattr(getattr(error, "category", None), "value", "unknown")
        bound.bind(
            error_category=category,
            retryable=getattr(error, "is_retryable", None),
            elapsed_seconds=elapsed,
            retry_of_attempt_id=getattr(attempt, "retry_of_attempt_id", None),
            fallback_from_attempt_id=getattr(attempt, "fallback_from_attempt_id", None),
        ).warning(
            "AI 调用失败: call={} attempt={} provider={} model={} "
            "thinking={} effort={} category={} retryable={} elapsed={:.3f}s",
            logical_call_id,
            attempt_id,
            provider,
            model,
            thinking,
            effort,
            category,
            getattr(error, "is_retryable", None),
            elapsed or 0.0,
        )

    @staticmethod
    def _artifact_identity(
        candidate: Any,
        reasoning_snapshot: EffectiveReasoningSnapshot | None,
    ) -> dict[str, str]:
        provider = str(getattr(getattr(candidate, "provider", None), "id", "unknown"))
        model = str(getattr(getattr(candidate, "model", None), "model_id", "unknown"))
        protocol = (
            reasoning_snapshot.protocol_family
            if reasoning_snapshot is not None
            else str(
                getattr(
                    getattr(
                        getattr(candidate, "provider", None),
                        "family",
                        "unknown",
                    ),
                    "value",
                    getattr(
                        getattr(candidate, "provider", None),
                        "family",
                        "unknown",
                    ),
                )
            )
        )
        endpoint = getattr(
            getattr(candidate, "endpoint", None),
            "base_url",
            "",
        )
        endpoint_scope = hashlib.sha256(str(endpoint).encode("utf-8")).hexdigest()
        return {
            "provider_family": provider,
            "protocol_family": protocol,
            "model_family": model,
            "endpoint_scope": endpoint_scope,
        }

    async def _capture_projection(
        self,
        *,
        artifact_kind: str,
        attempt: Any,
        candidate: Any,
        reasoning_snapshot: EffectiveReasoningSnapshot | None,
        payload: Any,
    ) -> None:
        settings = get_settings()
        if (
            attempt is None
            or self.tool_service is None
            or not settings.activity_request_response_capture_enabled
        ):
            return
        identity = self._artifact_identity(candidate, reasoning_snapshot)
        await self.tool_service.capture_sensitive_artifact(
            artifact_kind=artifact_kind,
            payload=projection_json(payload),
            attempt_id=int(attempt.id),
            retention_days=settings.activity_artifact_retention_days,
            **identity,
        )

    async def _capture_reasoning(
        self,
        *,
        attempt: Any,
        candidate: Any,
        reasoning_snapshot: EffectiveReasoningSnapshot | None,
        payload: str | None,
        availability: str,
        policy: ReasoningCapturePolicy,
    ) -> None:
        if attempt is None:
            return
        await self.attempt_service.record_reasoning_event(
            int(attempt.id),
            event_type="reasoning_observed",
            availability=availability,
        )
        if self.tool_service is None or payload is None:
            return
        identity = self._artifact_identity(candidate, reasoning_snapshot)
        await self.tool_service.capture_reasoning_artifact(
            attempt_id=int(attempt.id),
            availability=availability,
            payload=payload,
            policy=policy,
            **identity,
        )

    async def _revision(self, explicit: int | None) -> int | None:
        if explicit is not None:
            return explicit
        if self.revision_resolver is not None:
            result = self.revision_resolver()
            return await result if hasattr(result, "__await__") else result
        return None

    async def begin(
        self,
        *,
        logical_call_id: str,
        attempt_kind: str,
        purpose: str,
        requested: Any,
        effective: Any,
        context_revision_id: int | None = None,
        retry_of: int | None = None,
        fallback_from: int | None = None,
        reasoning_snapshot: EffectiveReasoningSnapshot | None = None,
    ):
        if self.context is None:
            return None
        return await self.attempt_service.begin_attempt(
            self.context,
            logical_call_id,
            attempt_kind,
            purpose,
            requested,
            effective,
            await self._revision(context_revision_id),
            retry_of=retry_of,
            fallback_from=fallback_from,
            reasoning_snapshot=reasoning_snapshot,
        )

    async def record_context_replacement(
        self,
        messages: list[Any],
        *,
        trigger_reason: str,
    ) -> int | None:
        """Persist the exact replacement context before a compressed retry."""
        if (
            self.context is None
            or self.context.thread_id is None
            or self.tool_service is None
            or self.lease is None
        ):
            return None
        payloads: list[dict[str, Any]] = []
        for message in messages:
            # 兼容 UnifiedMessage 对象与旧版 dict 消息（reviewer/issue 显式压缩
            # 均以 dict 形态传入）。Support both UnifiedMessage objects and
            # legacy dict messages (reviewer/issue explicit compression).
            if isinstance(message, dict):
                payload = dict(message)
                payload.pop("reasoning_content", None)
                payloads.append(payload)
                continue
            payload: dict[str, Any] = {
                "role": str(getattr(message, "role", "") or ""),
                "content": getattr(message, "content", None),
            }
            tool_calls = getattr(message, "tool_calls", None)
            if tool_calls:
                payload["tool_calls"] = [
                    {
                        "id": str(getattr(call, "id", "") or ""),
                        "type": "function",
                        "function": {
                            "name": str(getattr(call, "name", "") or ""),
                            "arguments": str(getattr(call, "arguments", "") or ""),
                        },
                    }
                    for call in tool_calls
                ]
            tool_call_id = getattr(message, "tool_call_id", None)
            if tool_call_id:
                payload["tool_call_id"] = str(tool_call_id)
            name = getattr(message, "name", None)
            if name:
                payload["name"] = str(name)
            payloads.append(payload)
        revision = await self.tool_service.replace_context_messages(
            thread_id=self.context.thread_id,
            work_unit_id=self.context.work_unit_id,
            messages=payloads,
            lease=self.lease,
            trigger_reason=trigger_reason,
        )
        return int(revision.id)

    @staticmethod
    def _context_limits(
        candidate: Any,
        request: Any,
    ) -> tuple[int | None, int | None, int | None]:
        model = getattr(candidate, "model", None)
        context_window = getattr(model, "context_window_tokens", None)
        if not isinstance(context_window, int) or context_window <= 0:
            context_window = None
        reserved_output = getattr(request, "max_tokens", None)
        if not isinstance(reserved_output, int) or reserved_output < 0:
            reserved_output = None
        available = (
            context_window - reserved_output
            if context_window is not None
            and reserved_output is not None
            and context_window >= reserved_output
            else None
        )
        return context_window, reserved_output, available

    @staticmethod
    def _attempt_usage_measurement(attempt: Any, field_name: str) -> MeasuredValue:
        value = getattr(attempt, field_name, None)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return MeasuredValue(
                None,
                AVAILABILITY_UNAVAILABLE,
                SOURCE_PROVIDER,
            )
        return MeasuredValue(
            value,
            getattr(
                attempt,
                f"{field_name}_availability",
                AVAILABILITY_REPORTED,
            ),
            getattr(attempt, f"{field_name}_source", SOURCE_PROVIDER),
        )

    async def _record_before_request_context(
        self,
        *,
        attempt: Any,
        adapter: Any,
        candidate: Any,
        request: Any,
    ) -> None:
        """Persist field-level context provenance for the exact serialized request.

        The adapter's final request body is the closest stable representation of
        what is sent over HTTP.  Until a provider/tokenizer counting API is
        available, its size is explicitly marked heuristic/estimated rather than
        being presented as a provider-reported token count.
        """
        if (
            attempt is None
            or self.context_service is None
            or getattr(attempt, "context_revision_id", None) is None
        ):
            return
        try:
            serialized = adapter.serialize_request(request)
            encoded = json.dumps(
                serialized,
                ensure_ascii=False,
                default=str,
                sort_keys=True,
                separators=(",", ":"),
            )
        except Exception:
            encoded = repr(request)
        estimated_tokens = max(1, (len(encoded) + 3) // 4)

        context_window, reserved_output, available = self._context_limits(
            candidate,
            request,
        )
        fields = ContextSnapshotFields(
            context_tokens=MeasuredValue(
                estimated_tokens,
                AVAILABILITY_ESTIMATED,
                SOURCE_HEURISTIC,
            ),
            context_window_tokens=MeasuredValue(
                context_window,
                AVAILABILITY_REPORTED
                if context_window is not None
                else AVAILABILITY_UNAVAILABLE,
                SOURCE_MODEL_CATALOG,
            ),
            reserved_output_tokens=MeasuredValue(
                reserved_output,
                AVAILABILITY_REPORTED
                if reserved_output is not None
                else AVAILABILITY_UNAVAILABLE,
                SOURCE_CONFIGURATION,
            ),
            available_context_tokens=MeasuredValue(
                available,
                AVAILABILITY_ESTIMATED
                if available is not None
                else AVAILABILITY_UNAVAILABLE,
                SOURCE_HEURISTIC,
            ),
        )
        await self.context_service.record_snapshot(
            attempt_id=int(attempt.id),
            operation_id=None,
            revision_id=int(attempt.context_revision_id),
            snapshot_kind="before_request",
            fields=fields,
        )

    async def _record_after_request_context(
        self,
        *,
        attempt: Any,
        candidate: Any,
        request: Any,
    ) -> None:
        """Replace the in-flight heuristic with provider-reported usage."""
        if (
            attempt is None
            or self.context_service is None
            or getattr(attempt, "context_revision_id", None) is None
        ):
            return

        input_tokens = self._attempt_usage_measurement(attempt, "input_tokens")
        cache_read_tokens = self._attempt_usage_measurement(
            attempt,
            "cached_input_tokens",
        )
        reasoning_tokens = self._attempt_usage_measurement(
            attempt,
            "reasoning_tokens",
        )
        if all(
            item.value is None
            for item in (input_tokens, cache_read_tokens, reasoning_tokens)
        ):
            return

        context_window, reserved_output, available = self._context_limits(
            candidate,
            request,
        )
        fields = ContextSnapshotFields(
            context_tokens=input_tokens,
            context_window_tokens=MeasuredValue(
                context_window,
                AVAILABILITY_REPORTED
                if context_window is not None
                else AVAILABILITY_UNAVAILABLE,
                SOURCE_MODEL_CATALOG,
            ),
            reserved_output_tokens=MeasuredValue(
                reserved_output,
                AVAILABILITY_REPORTED
                if reserved_output is not None
                else AVAILABILITY_UNAVAILABLE,
                SOURCE_CONFIGURATION,
            ),
            available_context_tokens=MeasuredValue(
                available,
                AVAILABILITY_ESTIMATED
                if available is not None
                else AVAILABILITY_UNAVAILABLE,
                SOURCE_HEURISTIC,
            ),
            cache_read_tokens=cache_read_tokens,
            reasoning_context_tokens=reasoning_tokens,
        )
        await self.context_service.record_snapshot(
            attempt_id=int(attempt.id),
            operation_id=None,
            revision_id=int(attempt.context_revision_id),
            snapshot_kind="after_request",
            fields=fields,
        )

    async def send_embedding(
        self,
        create: Callable[[], Awaitable[Any]],
        *,
        logical_call_id: str,
        requested: Any,
        effective: Any,
    ) -> tuple[Any, int | None]:
        """Observe one concrete ``embeddings.create`` HTTP send."""
        if self.context is not None and self.context.thread_id is not None:
            raise ValueError(
                "embedding observation requires a threadless InvocationContext"
            )
        started = time.monotonic()
        attempt = await self._best_effort(
            "embedding_attempt_begin",
            lambda: self.begin(
                logical_call_id=logical_call_id,
                attempt_kind="primary",
                purpose="embedding",
                requested=requested,
                effective=effective,
                context_revision_id=None,
            ),
        )
        try:
            response = await create()
        except asyncio.CancelledError:
            if attempt is not None:
                await self._best_effort(
                    "embedding_attempt_cancel",
                    lambda: self.attempt_service.fail(
                        attempt.id,
                        "request cancelled",
                        error_category="cancelled",
                        status="cancelled",
                    ),
                )
            raise
        except BaseException as exc:
            if attempt is not None:
                await self._best_effort(
                    "embedding_attempt_fail",
                    lambda error=exc: self.attempt_service.fail(
                        attempt.id,
                        error,
                        error_category=getattr(
                            getattr(error, "category", None), "value", "unknown"
                        ),
                        retryable=getattr(error, "is_retryable", None),
                        http_status=getattr(error, "status_code", None),
                    ),
                )
            raise
        if attempt is not None:
            await self._best_effort(
                "embedding_attempt_finish",
                lambda: self.attempt_service.finish(
                    attempt.id,
                    raw_usage=getattr(response, "usage", None),
                    provider_request_id=self._provider_request_id(response),
                ),
            )
        logger.bind(
            logical_call_id=logical_call_id,
            attempt_id=getattr(attempt, "id", None),
            purpose="embedding",
            elapsed_seconds=time.monotonic() - started,
        ).info(
            "AI embedding 调用成功: call={} attempt={} elapsed={:.3f}s",
            logical_call_id,
            getattr(attempt, "id", None),
            time.monotonic() - started,
        )
        return response, attempt.id if attempt is not None else None

    async def send_chat(
        self,
        adapter: Any,
        client: Any,
        candidate: Any,
        request: Any,
        *,
        timeout: float | None = None,
        logical_call_id: str,
        attempt_kind: str = "primary",
        purpose: str = "model",
        requested: Any = None,
        retry_of: int | None = None,
        fallback_from: int | None = None,
        context_revision_id: int | None = None,
        reasoning_snapshot: EffectiveReasoningSnapshot | None = None,
    ) -> tuple[Any, int | None]:
        requested = candidate if requested is None else requested
        started = time.monotonic()
        attempt = await self._best_effort(
            "attempt_begin",
            lambda: self.begin(
                logical_call_id=logical_call_id,
                attempt_kind=attempt_kind,
                purpose=purpose,
                requested=requested,
                effective=candidate,
                context_revision_id=context_revision_id,
                retry_of=retry_of,
                fallback_from=fallback_from,
                reasoning_snapshot=reasoning_snapshot,
            ),
        )
        self.last_attempt_id = int(attempt.id) if attempt is not None else None
        self._log_attempt(
            "started",
            logical_call_id=logical_call_id,
            attempt=attempt,
            attempt_kind=attempt_kind,
            purpose=purpose,
            candidate=candidate,
            reasoning_snapshot=reasoning_snapshot,
        )
        await self._best_effort(
            "context_snapshot_before_request",
            lambda: self._record_before_request_context(
                attempt=attempt,
                adapter=adapter,
                candidate=candidate,
                request=request,
            ),
        )
        await self._best_effort(
            "request_projection_capture",
            lambda: self._capture_projection(
                artifact_kind="request_projection",
                attempt=attempt,
                candidate=candidate,
                reasoning_snapshot=reasoning_snapshot,
                payload=adapter.serialize_request(request),
            ),
        )
        try:
            response = await adapter.chat(
                client,
                candidate.endpoint,
                candidate.credential,
                request,
                timeout=timeout,
            )
        except asyncio.CancelledError as exc:
            if attempt is not None:
                attempt = await self._best_effort(
                    "attempt_cancel",
                    lambda: self.attempt_service.fail(
                        attempt.id,
                        "send cancelled",
                        error_category="cancelled",
                        status="cancelled",
                    ),
                    default=attempt,
                )
            self._log_attempt(
                "failed",
                logical_call_id=logical_call_id,
                attempt=attempt,
                attempt_kind=attempt_kind,
                purpose=purpose,
                candidate=candidate,
                reasoning_snapshot=reasoning_snapshot,
                elapsed=time.monotonic() - started,
                error=exc,
            )
            raise
        except BaseException as exc:
            if attempt is not None:
                attempt = await self._best_effort(
                    "attempt_fail",
                    lambda error=exc: self.attempt_service.fail(
                        attempt.id,
                        error,
                        error_category=getattr(
                            getattr(error, "category", None), "value", "unknown"
                        ),
                        retryable=getattr(error, "is_retryable", None),
                        http_status=getattr(error, "status_code", None),
                    ),
                    default=attempt,
                )
            self._log_attempt(
                "failed",
                logical_call_id=logical_call_id,
                attempt=attempt,
                attempt_kind=attempt_kind,
                purpose=purpose,
                candidate=candidate,
                reasoning_snapshot=reasoning_snapshot,
                elapsed=time.monotonic() - started,
                error=exc,
            )
            raise
        policy = configured_reasoning_capture_policy()
        reasoning_payload = getattr(response, "reasoning_content", None)
        reasoning_tokens = getattr(
            getattr(response, "usage", None),
            "reasoning_tokens",
            0,
        )
        reasoning_availability = (
            REASONING_PROVIDER_EXPOSED
            if isinstance(reasoning_payload, str) and reasoning_payload
            else REASONING_OMITTED
            if (
                reasoning_snapshot is not None
                and reasoning_snapshot.effective_thinking_mode
                not in {"disabled", "unsupported"}
                and isinstance(reasoning_tokens, int)
                and reasoning_tokens > 0
            )
            else REASONING_UNAVAILABLE
        )
        await self._best_effort(
            "reasoning_capture",
            lambda: self._capture_reasoning(
                attempt=attempt,
                candidate=candidate,
                reasoning_snapshot=reasoning_snapshot,
                payload=(
                    reasoning_payload if isinstance(reasoning_payload, str) else None
                ),
                availability=reasoning_availability,
                policy=policy,
            ),
        )
        response_projection = (
            response.to_dict()
            if callable(getattr(response, "to_dict", None))
            else response
        )
        await self._best_effort(
            "response_projection_capture",
            lambda: self._capture_projection(
                artifact_kind="response_projection",
                attempt=attempt,
                candidate=candidate,
                reasoning_snapshot=reasoning_snapshot,
                payload=response_projection,
            ),
        )
        if attempt is not None:
            await self._best_effort(
                "attempt_first_token",
                lambda: self.attempt_service.first_token(attempt.id),
            )
            attempt = await self._best_effort(
                "attempt_finish",
                lambda: self.attempt_service.finish(
                    attempt.id,
                    response,
                    provider_request_id=self._provider_request_id(response),
                    stop_reason=getattr(
                        getattr(response, "stop_reason", None), "value", None
                    ),
                ),
                default=attempt,
            )
            await self._best_effort(
                "context_snapshot_after_request",
                lambda: self._record_after_request_context(
                    attempt=attempt,
                    candidate=candidate,
                    request=request,
                ),
            )
        self._log_attempt(
            "completed",
            logical_call_id=logical_call_id,
            attempt=attempt,
            attempt_kind=attempt_kind,
            purpose=purpose,
            candidate=candidate,
            reasoning_snapshot=reasoning_snapshot,
            elapsed=time.monotonic() - started,
        )
        return response, attempt.id if attempt is not None else None

    async def consume_stream_event(
        self,
        event: Any,
        *,
        attempt: Any = None,
        reasoning_policy: ReasoningCapturePolicy | None = None,
        preview_callback: Callable[[str], Awaitable[Any] | Any] | None = None,
        is_admin: bool = False,
        has_admin_channel: bool = False,
    ) -> Any:
        """Record safe reasoning state and optionally emit an admin preview.

        Public callers still receive the original event, but reasoning text is
        never sent through a generic/public callback.  Metadata-only and opaque
        events deliberately have no preview payload.
        """
        event_type = getattr(event, "type", "")
        if event_type.startswith("reasoning_"):
            availability = (
                getattr(event, "reasoning_availability", None) or REASONING_OMITTED
            )
            if attempt is not None and getattr(attempt, "id", None) is not None:
                record = getattr(self.attempt_service, "record_reasoning_event", None)
                if record is not None:
                    await record(
                        attempt.id,
                        event_type=event_type,
                        availability=availability,
                        provider_event_metadata=getattr(
                            event, "provider_event_metadata", None
                        ),
                    )
            policy = reasoning_policy or ReasoningCapturePolicy()
            text = getattr(event, "text", None)
            can_preview = (
                event_type == "reasoning_delta"
                and isinstance(text, str)
                and bool(text)
                and availability in {REASONING_PROVIDER_EXPOSED, REASONING_SUMMARIZED}
                and policy.capture_mode != "metadata_only"
                and is_admin
                and has_admin_channel
                and preview_callback is not None
            )
            if can_preview:
                result = preview_callback(text)
                if hasattr(result, "__await__"):
                    await result
        elif (
            event_type == "usage"
            and attempt is not None
            and getattr(event, "usage", None) is not None
        ):
            # Usage is accumulated until terminal done; the final finish call
            # persists the last provider-reported snapshot.
            pass
        return event

    async def send_stream(
        self,
        adapter: Any,
        client: Any,
        candidate: Any,
        request: Any,
        *,
        timeout: float | None = None,
        logical_call_id: str,
        attempt_kind: str = "primary",
        purpose: str = "model",
        requested: Any = None,
        retry_of: int | None = None,
        fallback_from: int | None = None,
        context_revision_id: int | None = None,
        reasoning_policy: ReasoningCapturePolicy | None = None,
        preview_callback: Callable[[str], Awaitable[Any] | Any] | None = None,
        is_admin: bool = False,
        has_admin_channel: bool = False,
        reasoning_snapshot: EffectiveReasoningSnapshot | None = None,
    ) -> AsyncIterator[Any]:
        requested = candidate if requested is None else requested
        reasoning_policy = reasoning_policy or configured_reasoning_capture_policy()
        settings = get_settings()
        capture_response_projection = settings.activity_request_response_capture_enabled
        capture_reasoning_payload = settings.activity_reasoning_capture_enabled
        started = time.monotonic()
        attempt = await self._best_effort(
            "attempt_begin",
            lambda: self.begin(
                logical_call_id=logical_call_id,
                attempt_kind=attempt_kind,
                purpose=purpose,
                requested=requested,
                effective=candidate,
                context_revision_id=context_revision_id,
                retry_of=retry_of,
                fallback_from=fallback_from,
                reasoning_snapshot=reasoning_snapshot,
            ),
        )
        self.last_attempt_id = int(attempt.id) if attempt is not None else None
        self._log_attempt(
            "started",
            logical_call_id=logical_call_id,
            attempt=attempt,
            attempt_kind=attempt_kind,
            purpose=purpose,
            candidate=candidate,
            reasoning_snapshot=reasoning_snapshot,
        )
        await self._best_effort(
            "context_snapshot_before_request",
            lambda: self._record_before_request_context(
                attempt=attempt,
                adapter=adapter,
                candidate=candidate,
                request=request,
            ),
        )
        await self._best_effort(
            "request_projection_capture",
            lambda: self._capture_projection(
                artifact_kind="request_projection",
                attempt=attempt,
                candidate=candidate,
                reasoning_snapshot=reasoning_snapshot,
                payload=adapter.serialize_request(request),
            ),
        )
        first = False
        final_usage = None
        final_stop_reason = None
        reasoning_chunks: list[str] = []
        reasoning_availability = REASONING_UNAVAILABLE
        response_events: list[Any] = []
        try:
            async for event in adapter.stream(
                client,
                candidate.endpoint,
                candidate.credential,
                request,
                timeout=timeout,
            ):
                await self._best_effort(
                    "stream_event_observation",
                    lambda event=event: self.consume_stream_event(
                        event,
                        attempt=attempt,
                        reasoning_policy=reasoning_policy,
                        preview_callback=preview_callback,
                        is_admin=is_admin,
                        has_admin_channel=has_admin_channel,
                    ),
                )
                if capture_response_projection:
                    response_events.append(event)
                event_type = getattr(event, "type", "")
                if event_type.startswith("reasoning_"):
                    reasoning_availability = (
                        getattr(event, "reasoning_availability", None)
                        or reasoning_availability
                    )
                    text = getattr(event, "text", None)
                    if (
                        capture_reasoning_payload
                        and event_type == "reasoning_delta"
                        and isinstance(text, str)
                    ):
                        reasoning_chunks.append(text)
                if self._is_effective_delta(event) and not first:
                    first = True
                    if attempt is not None:
                        await self._best_effort(
                            "attempt_first_token",
                            lambda: self.attempt_service.first_token(attempt.id),
                        )
                event_usage = getattr(event, "usage", None)
                if event_usage is not None:
                    final_usage = (
                        event_usage
                        if final_usage is None
                        else final_usage.add(event_usage)
                    )
                event_stop_reason = getattr(event, "stop_reason", None)
                if event_stop_reason is not None:
                    final_stop_reason = getattr(
                        event_stop_reason, "value", str(event_stop_reason)
                    )
                yield event
        except asyncio.CancelledError as exc:
            if attempt is not None:
                attempt = await self._best_effort(
                    "attempt_cancel",
                    lambda: self.attempt_service.fail(
                        attempt.id,
                        "stream cancelled",
                        error_category="cancelled",
                        status="cancelled",
                    ),
                    default=attempt,
                )
            self._log_attempt(
                "failed",
                logical_call_id=logical_call_id,
                attempt=attempt,
                attempt_kind=attempt_kind,
                purpose=purpose,
                candidate=candidate,
                reasoning_snapshot=reasoning_snapshot,
                elapsed=time.monotonic() - started,
                error=exc,
            )
            raise
        except BaseException as exc:
            if attempt is not None:
                attempt = await self._best_effort(
                    "attempt_fail",
                    lambda error=exc: self.attempt_service.fail(
                        attempt.id,
                        error,
                        error_category=getattr(
                            getattr(error, "category", None), "value", "unknown"
                        ),
                        retryable=getattr(error, "is_retryable", None),
                        http_status=getattr(error, "status_code", None),
                    ),
                    default=attempt,
                )
            self._log_attempt(
                "failed",
                logical_call_id=logical_call_id,
                attempt=attempt,
                attempt_kind=attempt_kind,
                purpose=purpose,
                candidate=candidate,
                reasoning_snapshot=reasoning_snapshot,
                elapsed=time.monotonic() - started,
                error=exc,
            )
            raise
        else:
            if (
                reasoning_availability == REASONING_UNAVAILABLE
                and reasoning_snapshot is not None
                and reasoning_snapshot.effective_thinking_mode
                not in {"disabled", "unsupported"}
                and int(getattr(final_usage, "reasoning_tokens", 0) or 0) > 0
            ):
                reasoning_availability = REASONING_OMITTED
            await self._best_effort(
                "reasoning_capture",
                lambda: self._capture_reasoning(
                    attempt=attempt,
                    candidate=candidate,
                    reasoning_snapshot=reasoning_snapshot,
                    payload=("".join(reasoning_chunks) if reasoning_chunks else None),
                    availability=reasoning_availability,
                    policy=reasoning_policy,
                ),
            )
            await self._best_effort(
                "response_projection_capture",
                lambda: self._capture_projection(
                    artifact_kind="response_projection",
                    attempt=attempt,
                    candidate=candidate,
                    reasoning_snapshot=reasoning_snapshot,
                    payload={
                        "events": response_events,
                        "stop_reason": final_stop_reason,
                        "usage": final_usage,
                    },
                ),
            )
            if attempt is not None:
                attempt = await self._best_effort(
                    "attempt_finish",
                    lambda: self.attempt_service.finish(
                        attempt.id,
                        raw_usage=final_usage,
                        stop_reason=final_stop_reason or "done",
                    ),
                    default=attempt,
                )
                await self._best_effort(
                    "context_snapshot_after_request",
                    lambda: self._record_after_request_context(
                        attempt=attempt,
                        candidate=candidate,
                        request=request,
                    ),
                )
            self._log_attempt(
                "completed",
                logical_call_id=logical_call_id,
                attempt=attempt,
                attempt_kind=attempt_kind,
                purpose=purpose,
                candidate=candidate,
                reasoning_snapshot=reasoning_snapshot,
                elapsed=time.monotonic() - started,
            )

    @staticmethod
    def _is_effective_delta(event: Any) -> bool:
        event_type = getattr(event, "type", "")
        return event_type in {
            "text_delta",
            "tool_call_delta",
            "tool_call_start",
            "reasoning_delta",
        } and bool(
            getattr(event, "text", "")
            or getattr(event, "tool_call", None)
            or event_type == "reasoning_delta"
        )

    @staticmethod
    def _provider_request_id(response: Any) -> str | None:
        raw = getattr(response, "raw", None)
        headers = getattr(raw, "headers", None)
        if headers:
            return headers.get("x-request-id") or headers.get("request-id")
        return getattr(response, "provider_request_id", None)


class ObservedEmbeddingSender(ObservedModelSender):
    """Specialized name for embedding sends kept separate from model semantics.

    It enforces the embedding-specific Work Unit contract at the public boundary.
    """

    async def send_embedding(
        self,
        create: Callable[[], Awaitable[Any]],
        *,
        logical_call_id: str,
        requested: Any,
        effective: Any,
    ) -> tuple[Any, int | None]:
        if self.context is not None and self.context.thread_id is not None:
            raise ValueError(
                "embedding observation requires a threadless InvocationContext"
            )
        return await super().send_embedding(
            create,
            logical_call_id=logical_call_id,
            requested=requested,
            effective=effective,
        )


__all__ = ["ObservedEmbeddingSender", "ObservedModelSender"]
