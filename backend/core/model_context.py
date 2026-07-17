"""AI 模型上下文管理 / AI model context-window management.

重构后按“每模型独立上下文窗口”解析，替代旧的全局 MODEL_CONTEXT_WINDOW。
优先级：
1. 调用方显式传入的 per-model 覆盖（来自 ai_model_configs 用户覆盖）
2. 新协议层模型目录（builtin + discovered）
3. 旧预定义模型映射表（兼容）
4. 保守兜底（128K，并提示管理员补充）

Refactored to resolve context windows per-model instead of a single global
MODEL_CONTEXT_WINDOW. Priority: explicit per-model override → protocol-layer
model catalog → legacy predefined table → conservative fallback.
"""

from typing import Dict, Optional

from loguru import logger

from backend.core.ai_protocol.models import DEFAULT_CONTEXT_WINDOW_TOKENS
from backend.core.config import get_settings


class ModelContextManager:
    """模型上下文管理器 / Model context-window manager."""

    # 旧预定义表（单位 K tokens），作为新目录的兜底补充
    # Legacy predefined table (K tokens), kept as a fallback supplement.
    PREDEFINED_MODELS = {
        # OpenAI Models
        "gpt-4": 128,
        "gpt-4-turbo": 128,
        "gpt-4-turbo-preview": 128,
        "gpt-4o": 128,
        "gpt-4o-mini": 128,
        "gpt-3.5-turbo": 16,
        "gpt-3.5-turbo-16k": 16,
        "gpt-35-turbo": 16,
        # DeepSeek Models
        "deepseek-chat": 128,
        "deepseek-coder": 128,
        "deepseek-reasoner": 64,
        "deepseek-r1": 64,
        "deepseek-v3": 64,
        # Zhipu AI Models (GLM)
        "glm-4": 128,
        "glm-4.7": 200,
        "glm-4-plus": 128,
        # Claude Models (Anthropic)
        "claude-sonnet-5": 1000,
        "claude-opus-4-8": 1000,
        "claude-opus-4-7": 1000,
        "claude-opus-4-6": 200,
        "claude-3-5-sonnet-20241022": 200,
        "claude-3-5-sonnet-20240620": 200,
        "claude-3-5-haiku-20241022": 200,
        "claude-3-opus-20240229": 200,
        "claude-3-sonnet-20240229": 200,
        "claude-3-haiku-20240307": 200,
        # Google Models (Gemini)
        "gemini-2.0-flash": 1000,
        "gemini-2.0-flash-exp": 1000,
        "gemini-1.5-pro": 2000,
        "gemini-1.5-flash": 1000,
        # 其他常见模型 / other common models
        "llama-3.1-405b": 128,
        "llama-3.1-70b": 128,
        "mistral-large": 128,
        "qwen-plus": 128,
        "qwen-max": 32,
        "qwen-turbo": 8,
    }

    # 保守兜底（K tokens）/ Conservative fallback (K tokens)
    CONSERVATIVE_FALLBACK_K = DEFAULT_CONTEXT_WINDOW_TOKENS // 1000

    def __init__(self):
        self.settings = get_settings()
        self._context_cache: Dict[str, int] = {}
        # per-model 覆盖入口（由配置层注入，避免本模块依赖数据库）
        # Per-model override hook (injected by config layer; this module
        # deliberately does not import SQLAlchemy to avoid circular deps).
        self._overrides: Dict[str, int] = {}

    def set_overrides(self, overrides: Dict[str, int]) -> None:
        """注入 per-model 上下文窗口覆盖（K tokens）/ Inject per-model overrides."""
        self._overrides = {k: v for k, v in overrides.items() if v and v > 0}
        self._context_cache.clear()

    def get_context_window(self, model_name: Optional[str] = None) -> int:
        """获取模型的上下文窗口大小（单位 K tokens）/ Get context window (K tokens).

        优先级：
        1. per-model 覆盖
        2. 旧全局 MODEL_CONTEXT_WINDOW（兼容，仅当未设置覆盖时）
        3. 预定义模型映射表（含新模型）
        4. 保守兜底 128K
        """
        if model_name is None:
            logger.warning(
                "未提供模型上下文信息，使用默认 {}K tokens。",
                self.CONSERVATIVE_FALLBACK_K,
            )
            return self.CONSERVATIVE_FALLBACK_K

        normalized = model_name.lower().strip()

        # 1. per-model 覆盖
        if normalized and normalized in self._overrides:
            return self._overrides[normalized]

        # 2. 旧全局 MODEL_CONTEXT_WINDOW（兼容）
        if (
            hasattr(self.settings, "model_context_window")
            and self.settings.model_context_window
        ):
            # 注意：全局值仅在未命中 per-model 时作为最后已知手段，并打日志提示
            # Global value is a legacy last-resort; log a hint to migrate.
            custom_context = self.settings.model_context_window
            logger.debug(
                "使用旧全局 MODEL_CONTEXT_WINDOW={}K（建议迁移为 per-model 配置）",
                custom_context,
            )
            return custom_context

        # 3. 缓存 → 预定义表
        if normalized and normalized in self._context_cache:
            return self._context_cache[normalized]
        context_size = self._get_from_predefined(normalized) if normalized else None
        if context_size:
            self._context_cache[normalized] = context_size
            return context_size

        # 4. 保守兜底 / conservative fallback
        logger.warning(
            "未找到模型 {} 的上下文信息，使用保守兜底 {}K tokens。"
            "请在配置页为该模型设置上下文窗口或触发模型发现。",
            model_name,
            self.CONSERVATIVE_FALLBACK_K,
        )
        return self.CONSERVATIVE_FALLBACK_K

    def _get_from_predefined(self, model_name: str) -> Optional[int]:
        """从预定义映射表获取上下文大小 / Look up predefined table."""
        if not model_name:
            return None
        model_name_normalized = model_name.lower().strip()

        # 精确匹配 / exact match
        if model_name_normalized in self.PREDEFINED_MODELS:
            return self.PREDEFINED_MODELS[model_name_normalized]

        # 模糊匹配（处理模型名称变体）/ fuzzy match for variants
        for predefined_model, context_size in self.PREDEFINED_MODELS.items():
            if (
                predefined_model in model_name_normalized
                or model_name_normalized in predefined_model
            ):
                logger.debug(
                    "模糊匹配: {} -> {} ({}K)",
                    model_name,
                    predefined_model,
                    context_size,
                )
                return context_size

        return None

    def calculate_safe_context(
        self, model_name: Optional[str] = None, safety_ratio: float = 0.8
    ) -> int:
        """计算安全上下文预算（tokens）/ Calculate safe budget in tokens."""
        total_context_k = self.get_context_window(model_name)
        safe_context_k = int(total_context_k * safety_ratio)
        safe_context_tokens = safe_context_k * 1000
        logger.debug(
            "安全上下文: {}K * {} = {}K = {} tokens",
            total_context_k,
            safety_ratio,
            safe_context_k,
            safe_context_tokens,
        )
        return safe_context_tokens

    def get_compression_budget(
        self, model_name: Optional[str] = None, threshold: float = 0.8
    ) -> int:
        """获取压缩预算（tokens，统一入口）/ Unified compression budget (tokens)."""
        return self.calculate_safe_context(model_name, threshold)

    def estimate_tokens(self, text: str) -> int:
        """估算文本的 token 数量 / Estimate token count for text."""
        if not text:
            return 0
        chinese_chars = sum(1 for c in text if "一" <= c <= "鿿")
        other_chars = len(text) - chinese_chars
        estimated_tokens = int(chinese_chars / 1.5 + other_chars / 4)
        return estimated_tokens

    def format_context_size(self, size_k: int) -> str:
        """格式化上下文大小为可读字符串 / Format context size."""
        if size_k >= 1000:
            return f"{size_k / 1000:.1f}M"
        return f"{size_k}K"


# 全局单例 / Global singleton
_model_context_manager: Optional[ModelContextManager] = None


def get_model_context_manager() -> ModelContextManager:
    """获取模型上下文管理器单例 / Get singleton manager."""
    global _model_context_manager
    if _model_context_manager is None:
        _model_context_manager = ModelContextManager()
    return _model_context_manager
