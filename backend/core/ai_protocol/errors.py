"""归一化错误 / Normalized AI errors.

将各协议厂商的错误码与错误体映射到 AIErrorCategory，并定义统一异常类型。
Maps vendor-specific status codes and error bodies to AIErrorCategory and
defines the unified exception types used across the protocol layer.
"""

from __future__ import annotations

from typing import Any

from backend.core.ai_protocol.models import AIErrorCategory

# 不再按错误类别禁止故障转移 / No error category is terminal by default.
# 保留空集合以兼容调用方对 ``is_terminal`` 的读取。
TERMINAL_CATEGORIES: frozenset[AIErrorCategory] = frozenset()

# 认证/权限/模型不存在时，当前候选没有重试价值，直接进入下一候选。
# Authentication, permission, and missing-model failures fail over immediately.
FALLBACK_ONLY_CATEGORIES: frozenset[AIErrorCategory] = frozenset(
    {
        AIErrorCategory.AUTH_INVALID,
        AIErrorCategory.PERMISSION_DENIED,
        AIErrorCategory.MODEL_NOT_FOUND,
    }
)

# 其余归一化 AI 错误允许重试，并在当前候选耗尽后故障转移。
# Other normalized AI errors are retried, then failed over after exhaustion.
RETRYABLE_CATEGORIES: frozenset[AIErrorCategory] = frozenset(AIErrorCategory) - (
    FALLBACK_ONLY_CATEGORIES
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
        status_code: int | None = None,
        provider: str = "",
        model: str = "",
        cause: BaseException | None = None,
    ):
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.provider = provider
        self.model = model
        self.__cause__ = cause

    @property
    def is_terminal(self) -> bool:
        """兼容旧调用方；错误不会因类别而阻断故障转移。"""
        return self.category in TERMINAL_CATEGORIES

    @property
    def is_fallback_only(self) -> bool:
        """当前候选不重试，但应继续尝试故障转移候选。"""
        return self.category in FALLBACK_ONLY_CATEGORIES

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
        attempted_candidates: list[str] | None = None,
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

    def __init__(self, message: str, *, attempts: list[dict[str, Any]] | None = None):
        super().__init__(AIErrorCategory.UNKNOWN, message)
        self.attempts = attempts or []


class ReviewCancelledError(Exception):
    """审查被外部信号取消（如 PR 关闭）/ Review cancelled by an external signal.

    不继承 AIError，避免被 unified_client 的 ``except AIError`` 当作候选失败吞掉，
    使其能沿调用栈向上传播到 worker 的取消收尾逻辑。
    """

    def __init__(self, message: str = "审查已被取消", *, task_key: str = ""):
        super().__init__(message)
        self.task_key = task_key


def classify_context_overflow(message_lower: str) -> bool:
    """根据错误文本判断是否为上下文超长 / Classify context overflow by text."""
    return any(kw in message_lower for kw in CONTEXT_OVERFLOW_KEYWORDS)
