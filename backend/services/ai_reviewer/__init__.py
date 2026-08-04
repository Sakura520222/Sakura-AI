"""AI审查器模块

此模块保持向后兼容，原有的导入方式继续工作：
    from backend.services.ai_reviewer import AIReviewer

重构后采用模块化架构，将原单文件拆分为多个专门模块：
- constants: 常量定义
- api_client: AI API 调用
- prompt_builder: 提示词构建
- result_parser: 结果解析
- tools: 工具处理
- compression: 上下文压缩
- label_recommender: 标签推荐
- reviewer: 主类（组合各模块）
"""

# 主类 - 保持向后兼容
# 可导出的子模块（供需要细粒度控制的场景使用）
from .api_client import AIApiClient
from .compression import ContextCompressor
from .constants import (
    DEFAULT_CONTEXT_LINES,
    DEFAULT_MAX_TOKENS,
    MAX_CONTEXT_LINES,
    MAX_FILE_LINES,
    MAX_FILE_SIZE_BYTES,
    SEVERITY_EMOJI,
)
from .label_recommender import LabelRecommender
from .prompt_builder import PromptBuilder
from .result_parser import ReviewResultParser
from .reviewer import AIReviewer
from .tools import FileToolHandler, SearchToolHandler, ToolHandler, ToolManager

__all__ = [
    "DEFAULT_CONTEXT_LINES",
    # 常量
    "DEFAULT_MAX_TOKENS",
    "MAX_CONTEXT_LINES",
    "MAX_FILE_LINES",
    "MAX_FILE_SIZE_BYTES",
    "SEVERITY_EMOJI",
    # 子模块
    "AIApiClient",
    # 主类（保持向后兼容）
    "AIReviewer",
    "ContextCompressor",
    "FileToolHandler",
    "LabelRecommender",
    "PromptBuilder",
    "ReviewResultParser",
    "SearchToolHandler",
    "ToolHandler",
    "ToolManager",
]
