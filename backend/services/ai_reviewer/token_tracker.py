"""Token 消耗追踪器

追踪一次 PR 审查过程中所有 AI API 调用的 token 消耗，
包括主审查、上下文压缩、分批 Map/Reduce 等场景。
"""

from __future__ import annotations

from loguru import logger


class TokenTracker:
    """追踪一次 PR 审查过程中所有 AI API 调用的 token 消耗"""

    def __init__(self) -> None:
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.api_call_count: int = 0

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

    def merge(self, other: TokenTracker) -> None:
        """合并另一个 tracker 的数据（用于分批模式合并多个批次的 token）"""
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.api_call_count += other.api_call_count

    @classmethod
    def from_dict(cls, data: dict) -> TokenTracker:
        """从字典创建 TokenTracker（用于合并分批结果）"""
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
        """序列化为字典"""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "api_call_count": self.api_call_count,
        }
