"""AI审查器模块

此模块保持向后兼容，原有的导入方式继续工作：
    from backend.services.ai_reviewer import AIReviewer

重构后采用模块化架构，将原 2856 行的单文件拆分为多个专门模块：
- constants: 常量定义
- api_client: AI API 调用
- prompt_builder: 提示词构建
- result_parser: 结果解析
- batch_processor: 批处理逻辑
- tools: 工具处理
- compression: 上下文压缩
- label_recommender: 标签推荐
- reviewer: 主类（组合各模块）
"""

# 主类 - 保持向后兼容
from .reviewer import AIReviewer

# 可导出的子模块（供需要细粒度控制的场景使用）
from .api_client import AIApiClient
from .batch_processor import BatchProcessor
from .compression import ContextCompressor
from .constants import *
from .label_recommender import LabelRecommender
from .prompt_builder import PromptBuilder
from .result_parser import ReviewResultParser
from .tools import FileToolHandler, SearchToolHandler, ToolHandler, ToolManager

__all__ = [
    # 主类（保持向后兼容）
    "AIReviewer",
    # 子模块
    "AIApiClient",
    "PromptBuilder",
    "ReviewResultParser",
    "BatchProcessor",
    "ContextCompressor",
    "LabelRecommender",
    "ToolHandler",
    "ToolManager",
    "FileToolHandler",
    "SearchToolHandler",
    # 常量
    "DEFAULT_API_TIMEOUT",
    "DEFAULT_MAX_TOKENS",
    "MAX_RETRIES",
    "MAX_FILE_SIZE_BYTES",
    "MAX_FILE_LINES",
    "MAX_FILES_PER_BATCH",
    "MAX_LINES_PER_BATCH",
    "SEVERITY_EMOJI",
    "EMOJI_TO_SEVERITY",
    "SEVERITY_TO_ISSUES_KEY",
]
