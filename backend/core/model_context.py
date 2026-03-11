"""AI模型上下文管理模块

用于管理不同AI模型的上下文窗口大小，支持预定义和手动配置。
"""

from typing import Optional, Dict
from loguru import logger

from backend.core.config import get_settings


class ModelContextManager:
    """模型上下文管理器"""

    # 预定义的模型上下文窗口大小（单位：K tokens）
    # 数据来源：各模型官方文档
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
        "deepseek-r1": 64,
        "deepseek-v3": 64,
        # Zhipu AI Models (GLM)
        "glm-4": 128,
        "glm-4.7": 200,
        "glm-4-plus": 128,
        # Claude Models (Anthropic)
        "claude-3-5-sonnet-20241022": 200,
        "claude-3-5-sonnet-20240620": 200,
        "claude-3-5-haiku-20241022": 200,
        "claude-3-opus-20240229": 200,
        "claude-3-sonnet-20240229": 200,
        "claude-3-haiku-20240307": 200,
        # Google Models (Gemini)
        "gemini-2.0-flash-exp": 1000,
        "gemini-1.5-pro": 1000,
        "gemini-1.5-flash": 1000,
        # 其他常见模型
        "llama-3.1-405b": 128,
        "llama-3.1-70b": 128,
        "mistral-large": 128,
        "qwen-plus": 128,
        "qwen-turbo": 8,
    }

    def __init__(self):
        self.settings = get_settings()
        self._context_cache: Dict[str, int] = {}

    def get_context_window(self, model_name: Optional[str] = None) -> int:
        """获取模型的上下文窗口大小（单位：K tokens）

        优先级：
        1. 用户自定义配置（环境变量 MODEL_CONTEXT_WINDOW）
        2. 预定义模型映射表
        3. 默认值（128K）

        Args:
            model_name: 模型名称，如果为 None 则使用配置中的默认模型

        Returns:
            上下文窗口大小（K tokens）
        """
        # 使用默认模型名称
        if model_name is None:
            model_name = self.settings.openai_model

        # 1. 检查用户自定义配置
        if (
            hasattr(self.settings, "model_context_window")
            and self.settings.model_context_window
        ):
            custom_context = self.settings.model_context_window
            logger.info(f"使用自定义上下文窗口: {custom_context}K tokens")
            return custom_context

        # 2. 检查缓存
        if model_name in self._context_cache:
            return self._context_cache[model_name]

        # 3. 尝试从预定义映射表获取
        context_size = self._get_from_predefined(model_name)
        if context_size:
            self._context_cache[model_name] = context_size
            logger.info(
                f"从预定义表获取模型上下文: {model_name} = {context_size}K tokens"
            )
            return context_size

        # 4. 使用默认值
        default_context = 128  # 默认 128K
        logger.warning(
            f"未找到模型 {model_name} 的上下文信息，使用默认值: {default_context}K tokens。"
            f"请在 .env 中设置 MODEL_CONTEXT_WINDOW 或确保模型在预定义列表中"
        )
        return default_context

    def _get_from_predefined(self, model_name: str) -> Optional[int]:
        """从预定义映射表获取上下文大小

        Args:
            model_name: 模型名称

        Returns:
            上下文大小（K tokens），如果未找到则返回 None
        """
        # 标准化模型名称（转换为小写）
        model_name_normalized = model_name.lower().strip()

        # 精确匹配
        if model_name_normalized in self.PREDEFINED_MODELS:
            return self.PREDEFINED_MODELS[model_name_normalized]

        # 模糊匹配（处理模型名称变体）
        for predefined_model, context_size in self.PREDEFINED_MODELS.items():
            # 检查是否包含关键词
            if (
                predefined_model in model_name_normalized
                or model_name_normalized in predefined_model
            ):
                logger.debug(
                    f"模糊匹配: {model_name} -> {predefined_model} ({context_size}K)"
                )
                return context_size

        return None

    def calculate_safe_context(
        self, model_name: Optional[str] = None, safety_ratio: float = 0.8
    ) -> int:
        """计算安全的上下文窗口大小

        考虑到：
        - 输入 token 和输出 token 都需要空间
        - 需要预留一定安全空间
        - 避免达到模型的硬限制

        Args:
            model_name: 模型名称
            safety_ratio: 安全比例（0-1），默认 0.8 表示使用 80% 的上下文

        Returns:
            安全的上下文大小（tokens，不是 K tokens）
        """
        total_context_k = self.get_context_window(model_name)
        safe_context_k = int(total_context_k * safety_ratio)

        # 转换为 tokens（乘以 1000）
        safe_context_tokens = safe_context_k * 1000

        logger.debug(
            f"计算安全上下文: {total_context_k}K * {safety_ratio} = {safe_context_k}K = {safe_context_tokens} tokens"
        )

        return safe_context_tokens

    def estimate_tokens(self, text: str) -> int:
        """估算文本的 token 数量

        这是一个粗略估算，实际 token 数取决于模型和分词器。
        经验法则：中文约 1.5 字符/token，英文约 4 字符/token。

        Args:
            text: 输入文本

        Returns:
            估算的 token 数量
        """
        if not text:
            return 0

        # 简单估算：按字符数计算
        # 中文字符通常占用更多 token
        chinese_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        other_chars = len(text) - chinese_chars

        # 估算：中文 1.5 字符/token，英文 4 字符/token
        estimated_tokens = int(chinese_chars / 1.5 + other_chars / 4)

        return estimated_tokens

    def format_context_size(self, size_k: int) -> str:
        """格式化上下文大小为可读字符串

        Args:
            size_k: 上下文大小（K tokens）

        Returns:
            格式化后的字符串
        """
        if size_k >= 1000:
            return f"{size_k / 1000:.1f}M"
        return f"{size_k}K"


# 全局单例
_model_context_manager: Optional[ModelContextManager] = None


def get_model_context_manager() -> ModelContextManager:
    """获取模型上下文管理器单例"""
    global _model_context_manager
    if _model_context_manager is None:
        _model_context_manager = ModelContextManager()
    return _model_context_manager
