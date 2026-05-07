"""AI 输出语言工具函数

提供根据 output_language 配置返回中/英文文本的辅助函数，
供 AI 审查相关的服务层使用。
"""

from backend.core.config import get_settings


def output_text(zh: str, en: str) -> str:
    """根据 output_language 配置返回对应语言的文本

    Args:
        zh: 中文文本
        en: 英文文本

    Returns:
        当 output_language 为 "en" 时返回英文，否则返回中文
    """
    output_lang = get_settings().output_language
    return en if output_lang == "en" else zh
