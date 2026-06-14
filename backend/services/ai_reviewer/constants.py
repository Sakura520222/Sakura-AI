"""AI审查器常量定义

集中管理所有魔法数字和字符串，便于维护和修改。
"""

from typing import Dict

# =============================================================================
# API 调用参数
# =============================================================================
DEFAULT_MAX_TOKENS = 16000  # 默认最大输出 token 数

# 总结/标签推荐参数
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
# 严重程度映射
# =============================================================================
# severity → emoji 的权威映射，全局唯一来源。同时覆盖 severity 单数
# （critical/major/minor/suggestion）与 issue category 复数别名
# （suggestions），让 comment_service / scan_report_service /
# decision_engine 等所有调用方都能直接查表 / Canonical severity→emoji
# mapping. Covers both singular severity values and the plural
# issue-category alias ("suggestions") so every caller can look up directly.
SEVERITY_EMOJI: Dict[str, str] = {
    "critical": "🔴",
    "major": "🟡",
    "minor": "🔵",
    "suggestion": "💡",
    "suggestions": "💡",
}

# 问题类别
ISSUE_CATEGORIES = ["critical", "major", "minor", "suggestions"]

# =============================================================================
# 工具定义
# =============================================================================
BASE_TOOLS = [
    "read_file",
    "list_directory",
    "search_in_files",
    "get_git_info",
    "list_commits",
]
# PR diff 工具（始终可用，AI 按需查看文件变更）
DIFF_TOOLS = ["get_file_diff", "list_changed_files"]
RAG_TOOLS = ["search_project_docs"]
CODE_INDEX_TOOLS = ["search_code_context"]
WEB_SEARCH_TOOLS = ["search_web", "fetch_url"]
SAKURA_TOOLS = ["read_sakura_docs", "list_sakura_directory", "read_sakura_memory"]

ALL_TOOLS = BASE_TOOLS + RAG_TOOLS + CODE_INDEX_TOOLS + WEB_SEARCH_TOOLS + SAKURA_TOOLS

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
# 日志消息模板
# =============================================================================
LOG_MESSAGES = {
    "ai_call_success": "AI调用成功（耗时 {duration:.1f}秒，重试 {retry} 次）",
    "ai_call_retry": "AI调用失败 [{error_type}]: {error}，{delay:.1f}秒后重试 ({attempt}/{max_retries}, 已耗时 {elapsed:.1f}s)",
    "context_usage": "📊 上下文使用率: {current_k:.1f}K / {safe_k:.1f}K ({percentage:.0f}%) | 轮次: {iteration}",
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
        "description": """搜索互联网获取最新文档、API 参考、最佳实践等信息。适用于：
- 查询最新的 API 文档、版本变更或技术规范
- 了解特定技术/框架的最佳实践和推荐用法
- 获取与代码相关的最新社区讨论和解决方案""",
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

FETCH_URL_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": """抓取指定 URL 的网页内容并转换为纯文本。适用于：
- 深入阅读搜索结果中的链接内容
- 获取官方文档、API 参考的完整页面
- 查看特定技术文章或博客的详细内容
注意：仅支持 HTTP/HTTPS 协议，大页面内容会被截断。""",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要抓取的网页 URL，例如：'https://docs.python.org/3/library/asyncio.html'",
                },
            },
            "required": ["url"],
        },
    },
}

SEARCH_IN_FILES_TOOL = {
    "type": "function",
    "function": {
        "name": "search_in_files",
        "description": "在仓库中跨文件搜索指定关键词，返回所有匹配的文件和行内容。类似于 grep 搜索。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词",
                },
                "file_extension": {
                    "type": "string",
                    "description": "可选：限定文件后缀，如 .py、.ts",
                },
                "directory": {
                    "type": "string",
                    "description": "可选：限定搜索目录",
                },
                "context_lines": {
                    "type": "integer",
                    "description": "匹配行上下文行数",
                    "default": 3,
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大返回匹配结果数",
                    "default": 20,
                },
            },
            "required": ["keyword"],
        },
    },
}

GET_GIT_INFO_TOOL = {
    "type": "function",
    "function": {
        "name": "get_git_info",
        "description": "获取仓库基本信息，包括描述、默认分支、语言统计、分支列表等。",
        "parameters": {
            "type": "object",
            "properties": {
                "branch_count": {
                    "type": "integer",
                    "description": "返回的分支数量",
                    "default": 20,
                },
            },
            "required": [],
        },
    },
}

LIST_COMMITS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_commits",
        "description": "查看指定分支的提交历史记录。",
        "parameters": {
            "type": "object",
            "properties": {
                "branch": {
                    "type": "string",
                    "description": "分支名，默认为 PR 的 HEAD 分支或仓库默认分支",
                },
                "per_page": {
                    "type": "integer",
                    "description": "返回的提交数量",
                    "default": 10,
                },
            },
            "required": [],
        },
    },
}

READ_SAKURA_DOCS_TOOL = {
    "type": "function",
    "function": {
        "name": "read_sakura_docs",
        "description": (
            "读取项目 .sakura/ 目录中的指导文档（编码规范、架构设计、review规则等）。"
            "如果不指定 doc_path，返回所有文档的概览；指定路径则返回该文档的完整内容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doc_path": {
                    "type": "string",
                    "description": ".sakura/ 下的文档路径，如 'rules/review-rules.md'。留空返回所有文档概览。",
                },
            },
            "required": [],
        },
    },
}

LIST_SAKURA_DIRECTORY_TOOL = {
    "type": "function",
    "function": {
        "name": "list_sakura_directory",
        "description": (
            "列出项目 .sakura/ 目录的结构，查看其中有哪些指导文档和子目录。"
            "用于了解项目文档的组织方式。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subdirectory": {
                    "type": "string",
                    "description": ".sakura/ 下的子目录路径，留空列出根目录。",
                },
            },
            "required": [],
        },
    },
}

READ_SAKURA_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "read_sakura_memory",
        "description": (
            "读取 .sakura/memory/ 目录下的审查反思文件，了解历史审查经验和项目模式。"
            "不指定 file_name 时返回最近反思文件列表。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_name": {
                    "type": "string",
                    "description": ".sakura/memory/ 下的文件名，如 '2024-01-15_PR42_abc1234.md'。留空返回最近反思文件列表。",
                },
                "count": {
                    "type": "integer",
                    "description": "列出最近 N 个反思文件，默认 5",
                },
            },
            "required": [],
        },
    },
}

ALL_TOOL_DEFINITIONS = [
    READ_FILE_TOOL,
    LIST_DIRECTORY_TOOL,
    SEARCH_PROJECT_DOCS_TOOL,
    SEARCH_CODE_CONTEXT_TOOL,
    SEARCH_WEB_TOOL,
    FETCH_URL_TOOL,
    SEARCH_IN_FILES_TOOL,
    GET_GIT_INFO_TOOL,
    LIST_COMMITS_TOOL,
    READ_SAKURA_DOCS_TOOL,
    LIST_SAKURA_DIRECTORY_TOOL,
    READ_SAKURA_MEMORY_TOOL,
]

GET_FILE_DIFF_TOOL = {
    "type": "function",
    "function": {
        "name": "get_file_diff",
        "description": (
            "获取当前 PR 中指定文件的完整 diff（代码变更内容）。"
            "当 prompt 中没有包含某文件的 diff 时，使用此工具获取。"
            "返回的内容包含完整的增删行信息（+/- 前缀）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "要查看 diff 的文件路径（相对于项目根目录）",
                },
            },
            "required": ["file_path"],
        },
    },
}

LIST_CHANGED_FILES_TOOL = {
    "type": "function",
    "function": {
        "name": "list_changed_files",
        "description": (
            "列出当前 PR 中所有变更文件的概览，包括路径、状态、变更行数。"
            "用于在审查前了解整体变更范围。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}

# 工具名称到定义的映射
TOOL_NAME_TO_DEFINITION = {
    "read_file": READ_FILE_TOOL,
    "list_directory": LIST_DIRECTORY_TOOL,
    "search_project_docs": SEARCH_PROJECT_DOCS_TOOL,
    "search_code_context": SEARCH_CODE_CONTEXT_TOOL,
    "search_web": SEARCH_WEB_TOOL,
    "fetch_url": FETCH_URL_TOOL,
    "search_in_files": SEARCH_IN_FILES_TOOL,
    "get_git_info": GET_GIT_INFO_TOOL,
    "list_commits": LIST_COMMITS_TOOL,
    "read_sakura_docs": READ_SAKURA_DOCS_TOOL,
    "list_sakura_directory": LIST_SAKURA_DIRECTORY_TOOL,
    "read_sakura_memory": READ_SAKURA_MEMORY_TOOL,
    "get_file_diff": GET_FILE_DIFF_TOOL,
    "list_changed_files": LIST_CHANGED_FILES_TOOL,
}
