"""Effective AI request policy resolution.

This module is the single boundary where model metadata, user overrides, task
output caps, capabilities, and the candidate context window are combined.  The
unified client and its compressor must both consume this policy so preflight
and the eventual wire request cannot disagree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from backend.core.ai_protocol.models import (
    MetadataSource,
    ModelMetadata,
    ResolvedModel,
    UnifiedMessage,
    UnifiedTool,
)
from backend.core.model_context import get_model_context_manager

_DEFAULT_SAFETY_RESERVE_TOKENS = 256


def filter_reasoning_params(
    metadata: ModelMetadata,
    *,
    temperature: float | None,
    top_p: float | None,
    top_k: int | None,
    thinking: dict[str, Any] | None,
    effort: str | None,
) -> dict[str, Any]:
    """Apply caller intent and model configuration, then capability gates."""
    capabilities = metadata.capabilities
    configured = metadata.reasoning_params

    def pick(passed: Any, model_value: Any, allowed: bool) -> Any:
        if not allowed:
            return None
        return passed if passed is not None else model_value

    return {
        "temperature": pick(
            temperature, configured.temperature, capabilities.temperature
        ),
        "top_p": pick(top_p, configured.top_p, capabilities.top_p),
        "top_k": pick(top_k, configured.top_k, capabilities.top_k),
        "thinking": pick(thinking, configured.thinking, capabilities.thinking),
        "effort": pick(effort, configured.effort, capabilities.effort),
    }


def estimate_unified_messages(
    messages: list[UnifiedMessage], *, model_context: Any | None = None
) -> int:
    """Return the same conservative token estimate used by context preflight."""
    estimator = model_context or get_model_context_manager()
    total = 0
    for message in messages:
        total += estimator.estimate_tokens(message.content or "")
        for image in message.images or []:
            if image.data:
                # Base64 decodes to bytes; the manager's ~4 chars/token heuristic
                # is applied to the decoded size so multimodal payloads cannot
                # appear artificially free to preflight.
                total += max(1, int(len(image.data) * 3 / 4 / 4))
            elif image.url:
                total += estimator.estimate_tokens(image.url)
        for tool_call in message.tool_calls or []:
            total += estimator.estimate_tokens(
                tool_call.name + tool_call.arguments,
            )
    return total


def estimate_unified_tools(
    tools: list[UnifiedTool] | None, *, model_context: Any | None = None
) -> int:
    """Estimate wire-visible function declarations, including JSON schemas."""
    if not tools:
        return 0
    estimator = model_context or get_model_context_manager()
    return sum(
        estimator.estimate_tokens(
            f"{tool.name} {tool.description} "
            + json.dumps(tool.parameters, ensure_ascii=False, separators=(",", ":"))
        )
        for tool in tools
    )


def default_safety_reserve(context_window_tokens: int) -> int:
    """Reserve protocol overhead without taking a fixed large bite from small models."""
    if context_window_tokens <= 0:
        return _DEFAULT_SAFETY_RESERVE_TOKENS
    return min(_DEFAULT_SAFETY_RESERVE_TOKENS, max(32, context_window_tokens // 20))


@dataclass(frozen=True, slots=True)
class EffectiveRequestPolicy:
    """Credential- and prompt-free description of one concrete AI request."""

    role: str
    provider: str
    model: str
    protocol: str
    context_window_tokens: int
    estimated_input_tokens: int
    safety_reserve_tokens: int
    max_output_tokens: int
    temperature: float | None
    top_p: float | None
    top_k: int | None
    thinking: dict[str, Any] | None
    effort: str | None
    tools_enabled: bool
    stream: bool
    parameter_sources: dict[str, str] = field(default_factory=dict)

    @property
    def fits_context(self) -> bool:
        return (
            self.max_output_tokens > 0
            and self.estimated_input_tokens
            + self.max_output_tokens
            + self.safety_reserve_tokens
            <= self.context_window_tokens
        )

    def safe_log_dict(self) -> dict[str, Any]:
        """Projection safe for logs and response metadata; never includes prompts."""
        return {
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "protocol": self.protocol,
            "context_window_tokens": self.context_window_tokens,
            "estimated_input_tokens": self.estimated_input_tokens,
            "safety_reserve_tokens": self.safety_reserve_tokens,
            "effective_max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "thinking_mode": (
                None
                if self.thinking is None
                else str(self.thinking.get("type", "enabled"))
            ),
            "effort": self.effort,
            "tools_enabled": self.tools_enabled,
            "stream": self.stream,
            "parameter_sources": dict(self.parameter_sources),
        }


def resolve_effective_request_policy(
    candidate: ResolvedModel,
    messages: list[UnifiedMessage],
    *,
    role: str,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    thinking: dict[str, Any] | None = None,
    effort: str | None = None,
    max_tokens: int | None = None,
    output_token_cap: int | None = None,
    safety_reserve_tokens: int | None = None,
    tools: list[UnifiedTool] | None = None,
    clamp_to_context: bool = True,
    stream: bool = False,
) -> EffectiveRequestPolicy:
    """Resolve one policy; explicit output values are caps, never model overrides."""
    params = filter_reasoning_params(
        candidate.model,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        thinking=thinking,
        effort=effort,
    )

    model_output = max(1, int(candidate.model.reasoning_params.max_output_tokens))
    output_sources: list[str] = []
    requested_output = model_output
    for cap_name, cap_value in (("legacy_max_tokens", max_tokens), ("task_cap", output_token_cap)):
        if cap_value is None:
            continue
        try:
            cap = int(cap_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{cap_name} must be an integer") from exc
        if cap <= 0:
            raise ValueError(f"{cap_name} must be positive")
        if cap < requested_output:
            requested_output = cap
            output_sources = [cap_name]
        elif cap == requested_output and not output_sources:
            output_sources = [cap_name]

    model_source = (
        "model_override"
        if candidate.model.source == MetadataSource.USER_OVERRIDE
        else "builtin_metadata"
    )
    if not output_sources:
        output_sources = [model_source]

    context_window = int(candidate.model.context_window_tokens)
    if context_window <= 0:
        context_window = max(
            1,
            get_model_context_manager().get_context_window(candidate.model.model_id)
            * 1000,
        )
    estimated_input = estimate_unified_messages(
        messages
    ) + estimate_unified_tools(tools)
    reserve = safety_reserve_tokens or default_safety_reserve(context_window)
    reserve = max(0, int(reserve))
    context_available = context_window - estimated_input - reserve
    effective_output = (
        min(requested_output, context_available)
        if clamp_to_context
        else requested_output
    )
    if clamp_to_context and effective_output < requested_output:
        output_sources.append("context_budget")
    effective_output = max(0, effective_output)

    parameter_sources = {
        "max_output_tokens": "+".join(output_sources),
        **{
            name: (
                "caller"
                if passed is not None and passed == effective
                else model_source
                if effective is not None
                else "unsupported"
            )
            for name, passed, effective in (
                ("temperature", temperature, params["temperature"]),
                ("top_p", top_p, params["top_p"]),
                ("top_k", top_k, params["top_k"]),
                ("thinking", thinking, params["thinking"]),
                ("effort", effort, params["effort"]),
            )
        },
    }
    protocol = getattr(
        candidate.effective_protocol,
        "value",
        candidate.effective_protocol,
    )
    return EffectiveRequestPolicy(
        role=role,
        provider=candidate.provider.id,
        model=candidate.model.model_id,
        protocol=str(protocol),
        context_window_tokens=context_window,
        estimated_input_tokens=estimated_input,
        safety_reserve_tokens=reserve,
        max_output_tokens=effective_output,
        temperature=params["temperature"],
        top_p=params["top_p"],
        top_k=params["top_k"],
        thinking=params["thinking"],
        effort=params["effort"],
        tools_enabled=tools is not None,
        stream=stream,
        parameter_sources=parameter_sources,
    )
