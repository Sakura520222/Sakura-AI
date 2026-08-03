"""协议信封修复循环公共 helper / Protocol envelope repair loop helper.

为 PR 审查与 Issue 分析提供统一的"解析失败 → 累积式带错误重试 → 降级"流程。
review/issue 两侧 caller 把各自差异（解析器、错误类型、修复指令、降级函数）
作为参数传入，调用同一份实现。
"""
from typing import Any, Awaitable, Callable, Coroutine, List, Optional

from loguru import logger

from backend.webui.sse import publish_event

VIOLATION_SUFFIX = (
    "\n\nSpecific violation in your previous response:\n{error}"
)

_SIDE_BY_LABEL = {"审查": "review", "Issue 分析": "issue"}


async def run_protocol_repair_loop(
    *,
    parse_fn: Callable[[str], dict[str, Any]],
    error_type: type[Exception],
    base_messages: List[dict[str, Any]],
    final_text: str,
    repair_instruction: str,
    api_client: Any,
    tracker: Any,
    max_attempts: int,
    fallback_result_fn: Callable[[Exception], dict[str, Any]],
    log_label: str,
    sse_channel: str,
    invocation_context: Any = None,
    observer: Any = None,
    event_callback: Optional[Callable[[str, dict[str, Any]], Coroutine]] = None,
    on_repaired: Optional[Callable[[str, str, dict[str, Any]], Awaitable[None]]] = None,
    on_parse_failure: Optional[Callable[[BaseException], Awaitable[None]]] = None,
    attempt_kind: str = "protocol_repair",
) -> dict[str, Any]:
    """Run up to ``max_attempts`` cumulative format-only repair rounds.

    先本地解析 final_text（零模型调用的快路径）；失败则进入累积式修复循环，
    每轮把当轮具体协议违规注入 user 修复消息。上限内全部失败则降级。
    """
    # 1. 本地解析快路径
    try:
        result = parse_fn(final_text)
        if on_repaired is not None:
            await on_repaired(final_text, final_text, result)
        return result
    except error_type as first_error:
        logger.warning(
            "{label} 协议解析失败，进入修复循环（上限 {n}）: {err}",
            label=log_label,
            n=max_attempts,
            err=first_error,
        )
        if on_parse_failure is not None:
            await on_parse_failure(first_error)
        last_error: Exception = first_error

    current_text = final_text
    repair_messages = list(base_messages)  # tool-loop 历史（不含 final turn）

    for attempt in range(1, max_attempts + 1):
        instruction = repair_instruction + VIOLATION_SUFFIX.format(error=last_error)
        repair_messages = [
            *repair_messages,
            {"role": "assistant", "content": current_text},
            {"role": "user", "content": instruction},
        ]

        if event_callback is not None:
            await _emit_event(
                event_callback, "message", {"role": "user", "content": instruction}
            )

        await _publish_repair_event(
            sse_channel,
            attempt=attempt,
            max_attempts=max_attempts,
            error=last_error,
            log_label=log_label,
            outcome="attempting",
        )

        try:
            response = await api_client.call_with_retry(
                model="",
                messages=repair_messages,
                temperature=0,
                role="main",
                context=invocation_context,
                observer=observer,
                attempt_kind=attempt_kind,
            )
        except Exception as call_error:
            logger.error(
                "{label} 协议修复调用失败（第 {n} 轮），降级: {err}",
                label=log_label,
                n=attempt,
                err=call_error,
            )
            await _publish_repair_event(
                sse_channel,
                attempt=attempt,
                max_attempts=max_attempts,
                error=call_error,
                log_label=log_label,
                outcome="call_failed",
            )
            return fallback_result_fn(call_error)

        tracker.accumulate(response)
        current_text = response.choices[0].message.content or ""

        if event_callback is not None:
            await _emit_event(
                event_callback,
                "message",
                {"role": "assistant", "content": current_text},
            )

        try:
            result = parse_fn(current_text)
            if on_repaired is not None:
                await on_repaired(final_text, current_text, result)
            await _publish_repair_event(
                sse_channel,
                attempt=attempt,
                max_attempts=max_attempts,
                error=last_error,
                log_label=log_label,
                outcome="succeeded",
            )
            return result
        except error_type as err:
            last_error = err
            logger.warning(
                "{label} 协议修复第 {i}/{n} 轮仍失败: {err}",
                label=log_label,
                i=attempt,
                n=max_attempts,
                err=err,
            )

    logger.error(
        "{label} 协议修复 {n} 轮均失败，降级为人工复核",
        label=log_label,
        n=max_attempts,
    )
    await _publish_repair_event(
        sse_channel,
        attempt=max_attempts,
        max_attempts=max_attempts,
        error=last_error,
        log_label=log_label,
        outcome="degraded",
    )
    return fallback_result_fn(last_error)


async def _emit_event(
    callback: Callable[[str, dict[str, Any]], Coroutine],
    event_type: str,
    data: dict[str, Any],
) -> None:
    """best-effort 推送事件，吞掉可观测性侧异常，不影响修复主流程。"""
    try:
        await callback(event_type, data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("protocol repair event_callback failed: {}", exc)


async def _publish_repair_event(
    channel: str,
    *,
    attempt: int,
    max_attempts: int,
    error: BaseException,
    log_label: str,
    outcome: str,
) -> None:
    """best-effort 推送协议修复进度 SSE 事件。"""
    payload = {
        "attempt": attempt,
        "max_attempts": max_attempts,
        "error": str(error),
        "side": _SIDE_BY_LABEL.get(log_label, log_label),
        "outcome": outcome,
    }
    try:
        await publish_event("protocol_repair_attempt", payload, channel=channel)
    except Exception as exc:  # noqa: BLE001
        logger.warning("protocol repair SSE publish failed: {}", exc)
