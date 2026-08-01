"""Token 消耗追踪器

追踪一次 PR 审查过程中所有 AI API 调用的 token 消耗，
包括主审查、上下文压缩等场景。
支持实时记录上下文使用率快照。
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger


@dataclass
class ContextSnapshot:
    """单次上下文使用率快照"""

    iteration: int
    current_tokens: int
    context_window_tokens: int | None
    percentage: float | None
    source: str = "provider"


class TokenTracker:
    """追踪一次 PR 审查过程中所有 AI API 调用的 token 消耗"""

    def __init__(self) -> None:
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.api_call_count: int = 0
        self.context_usage_log: list[ContextSnapshot] = []

    def accumulate(self, response: object) -> None:
        """从 OpenAI API 响应中累积 token 使用量"""
        usage = getattr(response, "usage", None)
        if usage is None:
            return

        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0

        if prompt > 0 or completion > 0:
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.api_call_count += 1
            logger.debug(
                "Token 累积: +{}+{} (累计: {}+{}, {}次调用)",
                prompt,
                completion,
                self.prompt_tokens,
                self.completion_tokens,
                self.api_call_count,
            )

    def log_context_usage(
        self,
        response: object,
        context_window_tokens: int | None,
        iteration: int,
    ) -> int | None:
        """记录 Provider 上报的精确请求上下文并输出日志。

        Args:
            response: 当前 Provider 的统一响应
            context_window_tokens: 模型完整上下文窗口（非压缩安全阈值）
            iteration: 当前轮次

        Returns:
            Provider 明确上报的 input tokens；未上报时返回 None。
        """
        usage = getattr(response, "usage", None)
        if usage is None:
            logger.info(
                "📊 当前请求上下文: Provider 未返回 usage | 轮次: {}",
                iteration,
            )
            return None

        reported_fields = getattr(usage, "reported_fields", None)
        if reported_fields is not None and "input_tokens" not in reported_fields:
            logger.info(
                "📊 当前请求上下文: Provider 未报告精确 input_tokens | 轮次: {}",
                iteration,
            )
            return None

        current_tokens = getattr(usage, "input_tokens", None)
        if current_tokens is None and reported_fields is None:
            current_tokens = getattr(usage, "prompt_tokens", None)
        if (
            not isinstance(current_tokens, int)
            or isinstance(current_tokens, bool)
            or current_tokens < 0
        ):
            logger.info(
                "📊 当前请求上下文: Provider 未报告精确 input_tokens | 轮次: {}",
                iteration,
            )
            return None

        response_meta = getattr(response, "meta", None)
        winner_window = getattr(response_meta, "context_window_tokens", None)
        if (
            isinstance(winner_window, int)
            and not isinstance(winner_window, bool)
            and winner_window > 0
        ):
            context_window_tokens = winner_window
        elif (
            not isinstance(context_window_tokens, int)
            or isinstance(context_window_tokens, bool)
            or context_window_tokens <= 0
        ):
            context_window_tokens = None

        percentage = (
            current_tokens / context_window_tokens * 100
            if context_window_tokens is not None
            else None
        )

        snapshot = ContextSnapshot(
            iteration=iteration,
            current_tokens=current_tokens,
            context_window_tokens=context_window_tokens,
            percentage=percentage,
        )
        self.context_usage_log.append(snapshot)

        if percentage is None:
            logger.info(
                "📊 当前请求上下文: {:,} tokens | 完整上下文窗口未知 | "
                "轮次: {} | 来源: Provider",
                current_tokens,
                iteration,
            )
        elif percentage >= 90:
            logger.warning(
                "📊 当前请求上下文: {:,} / {:,} tokens ({:.1f}%) | "
                "轮次: {} | 来源: Provider ⚠️ 接近上限",
                current_tokens,
                context_window_tokens,
                percentage,
                iteration,
            )
        else:
            logger.info(
                "📊 当前请求上下文: {:,} / {:,} tokens ({:.1f}%) | "
                "轮次: {} | 来源: Provider",
                current_tokens,
                context_window_tokens,
                percentage,
                iteration,
            )
        return current_tokens

    def merge(self, other: TokenTracker) -> None:
        """合并另一个 tracker 的数据"""
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.api_call_count += other.api_call_count

    def add_tokens(self, prompt_tokens: int, completion_tokens: int) -> None:
        """直接累加 token 数值（无需构造临时 TokenTracker 对象）。"""
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens

    @classmethod
    def from_dict(cls, data: dict) -> TokenTracker:
        """从字典创建 TokenTracker"""
        tracker = cls()
        tracker.prompt_tokens = data.get("prompt_tokens", 0)
        tracker.completion_tokens = data.get("completion_tokens", 0)
        tracker.api_call_count = data.get("api_call_count", 0)
        return tracker

    def calculate_cost(self, price_prompt: float, price_completion: float) -> int:
        """计算预估成本，返回 int(cost * 100) 与 Issue 保持一致"""
        if self.prompt_tokens == 0 and self.completion_tokens == 0:
            return 0
        cost = (self.prompt_tokens / 1000) * price_prompt + (
            self.completion_tokens / 1000
        ) * price_completion
        return int(cost * 100)

    def to_dict(self) -> dict:
        """序列化为字典（仅持久化 token 统计，context_usage_log 仅用于运行时日志）"""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "api_call_count": self.api_call_count,
        }
