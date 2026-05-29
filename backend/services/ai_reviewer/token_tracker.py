"""Token 消耗追踪器

追踪一次 PR 审查过程中所有 AI API 调用的 token 消耗，
包括主审查、上下文压缩等场景。
支持实时记录上下文使用率快照。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from loguru import logger


@dataclass
class ContextSnapshot:
    """单次上下文使用率快照"""
    iteration: int
    current_tokens: int
    safe_threshold: int
    percentage: float


class TokenTracker:
    """追踪一次 PR 审查过程中所有 AI API 调用的 token 消耗"""

    def __init__(self) -> None:
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.api_call_count: int = 0
        self.context_usage_log: List[ContextSnapshot] = []

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
                f"Token 累积: +{prompt}+{completion} "
                f"(累计: {self.prompt_tokens}+{self.completion_tokens}, "
                f"{self.api_call_count}次调用)"
            )

    def log_context_usage(
        self, current_tokens: int, safe_threshold: int, iteration: int
    ) -> None:
        """记录上下文使用率快照并输出日志

        Args:
            current_tokens: 当前消息的 token 数
            safe_threshold: 安全上下文阈值
            iteration: 当前轮次
        """
        percentage = (current_tokens / safe_threshold * 100) if safe_threshold > 0 else 0
        current_k = current_tokens / 1000
        safe_k = safe_threshold / 1000

        snapshot = ContextSnapshot(
            iteration=iteration,
            current_tokens=current_tokens,
            safe_threshold=safe_threshold,
            percentage=percentage,
        )
        self.context_usage_log.append(snapshot)

        if percentage >= 90:
            logger.warning(
                "📊 上下文使用率: {:.1f}K / {:.1f}K ({:.0f}%) | 轮次: {} ⚠️ 接近上限",
                current_k, safe_k, percentage, iteration,
            )
        else:
            logger.info(
                "📊 上下文使用率: {:.1f}K / {:.1f}K ({:.0f}%) | 轮次: {}",
                current_k, safe_k, percentage, iteration,
            )

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
