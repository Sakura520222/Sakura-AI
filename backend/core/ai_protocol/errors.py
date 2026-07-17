"""归一化错误 / Normalized AI errors.

将各协议厂商的错误码与错误体映射到 AIErrorCategory，并定义统一异常类型。
Maps vendor-specific status codes and error bodies to AIErrorCategory and
defines the unified exception types used across the protocol layer.
"""

from __future__ import annotations

from typing import Any, Optional

from backend.core.ai_protocol.models import AIErrorCategory

# 终端错误：不重试、不回退 / Terminal errors: do not retry, do not fall back
TERMINAL_CATEGORIES: frozenset[AIErrorCategory] = frozenset(
    {
        AIErrorCategory.AUTH_INVALID,
        AIErrorCategory.PERMISSION_DENIED,
        AIErrorCategory.MODEL_NOT_FOUND,
        AIErrorCategory.BAD_REQUEST,
        AIErrorCategory.REFUSAL,
    }
)

# 可恢复错误：可重试 / Recoverable errors: retryable
RETRYABLE_CATEGORIES: frozenset[AIErrorCategory] = frozenset(
    {
        AIErrorCategory.RATE_LIMITED,
        AIErrorCategory.SERVER_ERROR,
        AIErrorCategory.OVERLOADED,
        AIErrorCategory.NETWORK,
        AIErrorCategory.EMPTY_RESPONSE,
        AIErrorCategory.UNKNOWN,
    }
)

# 上下文超长关键词（跨协议累积）/ Context overflow keywords (cross-protocol)
CONTEXT_OVERFLOW_KEYWORDS: list[str] = [
    # OpenAI 兼容
    "context_length",
    "maximum context length",
    "context window",
    "reduce the length",
    "too many tokens",
    "token limit",
    "prompt exceeds max length",
    "exceeds max length",
    "prompt too long",
    "input is too long",
    "input exceeds",
    # Anthropic
    "prompt is too long",
    "exceeds the maximum number of tokens",
    # Gemini
    "exceeds the maximum input tokens",
]


class AIError(Exception):
    """统一 AI 异常 / Unified AI exception.

    category 决定重试与回退策略；原始异常附在 cause。
    category drives retry/fallback strategy; the original exception is in cause.
    """

    def __init__(
        self,
        category: AIErrorCategory,
        message: str,
        *,
        status_code: Optional[int] = None,
        provider: str = "",
        model: str = "",
        cause: Optional[BaseException] = None,
    ):
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.provider = provider
        self.model = model
        self.__cause__ = cause

    @property
    def is_terminal(self) -> bool:
        return self.category in TERMINAL_CATEGORIES

    @property
    def is_retryable(self) -> bool:
        return self.category in RETRYABLE_CATEGORIES


class ContextOverflowError(AIError):
    """上下文超限（已耗尽压缩与回退仍无法承载）/ Context overflow."""

    def __init__(
        self,
        message: str,
        *,
        estimated_tokens: int = 0,
        model: str = "",
        provider: str = "",
        attempted_candidates: Optional[list[str]] = None,
    ):
        super().__init__(
            AIErrorCategory.CONTEXT_OVERFLOW,
            message,
            model=model,
            provider=provider,
        )
        self.estimated_tokens = estimated_tokens
        self.attempted_candidates = attempted_candidates or []


class AllCandidatesFailedError(AIError):
    """所有候选模型均失败 / All fallback candidates failed."""

    def __init__(self, message: str, *, attempts: Optional[list[dict[str, Any]]] = None):
        super().__init__(AIErrorCategory.UNKNOWN, message)
        self.attempts = attempts or []


def classify_context_overflow(message_lower: str) -> bool:
    """根据错误文本判断是否为上下文超长 / Classify context overflow by text."""
    return any(kw in message_lower for kw in CONTEXT_OVERFLOW_KEYWORDS)
