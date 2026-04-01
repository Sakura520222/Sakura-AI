"""AI审查器常量定义

集中管理所有魔法数字和字符串，便于维护和修改。
"""

from typing import Dict

# =============================================================================
# API 调用参数
# =============================================================================
DEFAULT_API_TIMEOUT = 120.0  # API 调用超时时间（秒）
DEFAULT_MAX_TOKENS = 16000  # 默认最大输出 token 数
MAX_RETRIES = 5  # 最大重试次数
INITIAL_DELAY = 1.0  # 初始重试延迟（秒）
TOTAL_TIMEOUT = 900.0  # 总超时时间（15分钟）

# 批处理专用参数
SUMMARY_TIMEOUT = 60.0  # 总结阶段超时
SUMMARY_MAX_TOKENS = 4000  # 总结阶段最大输出
LABEL_RECOMMENDATION_TIMEOUT = 60.0  # 标签推荐超时

# =============================================================================
# 文件限制
# =============================================================================
MAX_FILE_SIZE_BYTES = 200000  # 最大文件大小（200KB）
MAX_FILE_LINES = 500  # 最大文件行数（fallback 默认值，实际值从策略配置读取）
DEFAULT_CONTEXT_LINES = 20  # 搜索匹配时的默认上下文行数
MAX_CONTEXT_LINES = 200  # 搜索匹配时的最大上下文行数

# =============================================================================
# 批处理配置
# =============================================================================
MAX_FILES_PER_BATCH = 5  # 每批最大文件数
MAX_LINES_PER_BATCH = 2000  # 每批最大行数
BATCH_CONCURRENCY = 2  # 批次并发数
BATCH_JITTER_SECONDS = 0.3  # 批次抖动时间

# =============================================================================
# 严重程度映射
# =============================================================================
SEVERITY_EMOJI: Dict[str, str] = {
    "critical": "🔴",
    "major": "🟡",
    "minor": "⚠️",
    "suggestion": "💡",
}

EMOJI_TO_SEVERITY: Dict[str, str] = {v: k for k, v in SEVERITY_EMOJI.items()}

# 严重程度到问题字典的映射
SEVERITY_TO_ISSUES_KEY: Dict[str, str] = {
    "critical": "critical",
    "major": "major",
    "minor": "minor",
    "suggestion": "suggestions",  # 单数转复数
}

# 问题类别
ISSUE_CATEGORIES = ["critical", "major", "minor", "suggestions"]

# =============================================================================
# 工具定义
# =============================================================================
BASE_TOOLS = ["read_file", "list_directory"]
RAG_TOOLS = ["search_project_docs"]
CODE_INDEX_TOOLS = ["search_code_context"]
WEB_SEARCH_TOOLS = ["search_web"]

ALL_TOOLS = BASE_TOOLS + RAG_TOOLS + CODE_INDEX_TOOLS + WEB_SEARCH_TOOLS

# =============================================================================
# 上下文压缩配置
# =============================================================================
DEFAULT_COMPRESSION_KEEP_ROUNDS = 2  # 默认保留的对话轮数

# =============================================================================
# 工具调用配置
# =============================================================================
MAX_TOOL_ITERATIONS = 20  # 最大工具调用轮次

# =============================================================================
# 标签推荐配置
# =============================================================================
LABEL_RECOMMENDATION_TEMPERATURE = 0.3  # 标签推荐温度
MAX_LABEL_RECOMMENDATIONS = 5  # 最大推荐标签数
DEFAULT_LABEL_CONFIDENCE = 0.6  # 默认标签置信度

# =============================================================================
# 行内评论配置
# =============================================================================
INLINE_COMMENT_PATTERN = (
    r"###\s*[🔴🟡💡⚠️]\s+([^\s:]+):([\d\-\s,]+?)\s*\n(.*?)(?=###\s*[🔴🟡💡⚠️]|##|\Z)"
)

# =============================================================================
# 日志消息模板
# =============================================================================
LOG_MESSAGES = {
    "ai_call_success": "AI调用成功（耗时 {duration:.1f}秒，重试 {retry} 次）",
    "ai_call_retry": "AI调用失败 [{error_type}]: {error}，{delay:.1f}秒后重试 ({attempt}/{max_retries}, 已耗时 {elapsed:.1f}s)",
    "batch_start": "开始审查批次 {batch_idx}/{total_batches} ({file_count} 个文件, 工具: {use_tools})",
    "batch_complete": "批次 {batch_idx}/{total_batches} 审查完成: {comments} 条评论, {inline} 条行内评论",
    "compression_start": "开始压缩对话历史，当前大小: {tokens} tokens",
    "compression_complete": "压缩完成: {before} → {after} tokens (保留了 {rounds} 轮工具调用)",
}

# =============================================================================
# 工具定义常量（用于 OpenAI 函数调用）
# =============================================================================
READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "读取指定文件的内容，用于理解代码实现细节。"
            "支持三种模式：\n"
            "1. 完整读取（仅指定file_path）\n"
            "2. 行范围读取（指定start_line和end_line）\n"
            "3. 内容搜索（指定search_pattern，返回匹配行及上下文）\n"
            "返回内容始终包含行号，方便定位。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "要读取的文件路径（相对于项目根目录）",
                },
                "start_line": {
                    "type": "integer",
                    "description": (
                        "起始行号（从1开始）。仅当需要读取文件特定范围时指定。"
                    ),
                },
                "end_line": {
                    "type": "integer",
                    "description": (
                        "结束行号（从1开始，包含该行）。仅当需要读取文件特定范围时指定。"
                    ),
                },
                "search_pattern": {
                    "type": "string",
                    "description": (
                        "在文件中搜索包含此文本的行（简单文本匹配，非正则），"
                        "返回所有匹配行及其周围的上下文行，带行号。"
                        "与start_line/end_line互斥。"
                    ),
                },
                "context_lines": {
                    "type": "integer",
                    "description": (
                        "搜索模式下的上下文行数（在匹配行前后各显示多少行），默认20，最大200"
                    ),
                    "default": 20,
                },
            },
            "required": ["file_path"],
        },
    },
}

LIST_DIRECTORY_TOOL = {
    "type": "function",
    "function": {
        "name": "list_directory",
        "description": "列出指定目录下的文件和子目录",
        "parameters": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "要列出的目录路径（相对于项目根目录）",
                }
            },
            "required": ["directory"],
        },
    },
}

SEARCH_PROJECT_DOCS_TOOL = {
    "type": "function",
    "function": {
        "name": "search_project_docs",
        "description": """检索项目的指导文档（编码规范、架构准则、业务逻辑等），用于了解项目特定的规则和知识。

使用场景：
- 当你在审查代码发现不符合常理的架构设计时
- 需要确认项目特定的命名规范、代码风格时
- 遇到业务逻辑不确定其实现是否符合要求时
- 需要了解项目的技术栈选型和设计原则时

注意：如果未找到相关文档，说明项目文档库中可能不包含该主题的规范，此时应基于通用最佳实践进行审查。""",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索关键词或问题，例如：'错误处理规范'、'API设计原则'、'用户认证流程'",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回最相关的文档数量，默认 5",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
}

SEARCH_CODE_CONTEXT_TOOL = {
    "type": "function",
    "function": {
        "name": "search_code_context",
        "description": """检索代码仓库中的相关代码片段，用于理解代码上下文、查找相似实现、了解项目结构。

使用场景：
- 需要了解某个功能的实现方式时
- 查找类似代码模式或用法示例时
- 理解代码的依赖关系和调用链时
- 需要查看某个类或函数的完整实现时

注意：该工具检索已索引的代码片段，如果未找到相关代码，可能需要使用 read_file 查看具体文件。""",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索关键词或问题，例如：'用户认证实现'、'数据库连接配置'、'错误处理逻辑'",
                },
                "language": {
                    "type": "string",
                    "description": "可选：限定编程语言，例如：'python'、'javascript'、'go'等",
                },
                "file_path": {
                    "type": "string",
                    "description": "可选：限定在特定文件中检索",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回相关代码片段数量，默认 5",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
}

SEARCH_WEB_TOOL = {
    "type": "function",
    "function": {
        "name": "search_web",
        "description": """搜索互联网获取最新文档、API 参考、最佳实践等信息。

重要：仅在以下情况使用此工具：
- 本地文档搜索（search_project_docs）和代码搜索（search_code_context）均未找到答案时
- 需要查询最新的 API 文档或版本变更时
- 需要了解特定技术/框架的最新最佳实践时

不要用于可以通过本地工具解决的问题。""",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询，例如：'FastAPI dependency injection 最佳实践'、'Python 3.12 新特性'",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回搜索结果数量，默认 3",
                    "default": 3,
                },
            },
            "required": ["query"],
        },
    },
}

ALL_TOOL_DEFINITIONS = [
    READ_FILE_TOOL,
    LIST_DIRECTORY_TOOL,
    SEARCH_PROJECT_DOCS_TOOL,
    SEARCH_CODE_CONTEXT_TOOL,
    SEARCH_WEB_TOOL,
]

# 工具名称到定义的映射
TOOL_NAME_TO_DEFINITION = {
    "read_file": READ_FILE_TOOL,
    "list_directory": LIST_DIRECTORY_TOOL,
    "search_project_docs": SEARCH_PROJECT_DOCS_TOOL,
    "search_code_context": SEARCH_CODE_CONTEXT_TOOL,
    "search_web": SEARCH_WEB_TOOL,
}
