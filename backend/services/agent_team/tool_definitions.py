"""Agent 专家团队 - 工具定义

定义全栈专家和专业审查角色可用的 function calling 工具。
这些工具通过 OpenAI 兼容的 function calling 机制暴露给 AI。
"""

from __future__ import annotations

# 全栈专家可用工具
AGENT_READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "读取工作区内指定文件的内容。"
            "支持完整读取或行范围读取。返回带行号的文件内容。"
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
                    "description": "起始行号（从1开始），可选",
                },
                "end_line": {
                    "type": "integer",
                    "description": "结束行号（包含该行），可选",
                },
            },
            "required": ["file_path"],
        },
    },
}

AGENT_LIST_DIRECTORY_TOOL = {
    "type": "function",
    "function": {
        "name": "list_directory",
        "description": "列出指定目录下的文件和子目录，支持递归。",
        "parameters": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "要列出的目录路径（相对于项目根目录），默认为 '.'",
                    "default": ".",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "是否递归列出子目录，默认 false",
                    "default": False,
                },
            },
            "required": [],
        },
    },
}

AGENT_WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "写入文件到工作区。如果文件已存在则覆盖，不存在则创建。"
            "content 必须是完整的文件内容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "要写入的文件路径（相对于项目根目录）",
                },
                "content": {
                    "type": "string",
                    "description": "完整的文件内容",
                },
            },
            "required": ["file_path", "content"],
        },
    },
}

AGENT_EDIT_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": (
            "精确替换文件中的文本片段。查找 old_text 并替换为 new_text。"
            "适用于小范围修改（修 bug、改函数签名、改返回值等）。"
            "\n\n重要规则："
            "\n- old_text 必须与文件内容完全一致（包括缩进、空格、换行），建议从 read_file 输出精确复制"
            "\n- 如果 old_text 在文件中匹配多处，必须扩大上下文使匹配唯一，或设 replace_all=true"
            "\n- 不适合大段修改，大段修改请用 replace_lines 或 write_file"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "要编辑的文件路径（相对于项目根目录）",
                },
                "old_text": {
                    "type": "string",
                    "description": (
                        "要被替换的原始文本，必须与文件内容完全一致。"
                        "建议从 read_file 的输出中精确复制。"
                    ),
                },
                "new_text": {
                    "type": "string",
                    "description": "替换后的新文本",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "是否替换所有匹配项，默认 false（只替换第一个）",
                    "default": False,
                },
            },
            "required": ["file_path", "old_text", "new_text"],
        },
    },
}

AGENT_REPLACE_LINES_TOOL = {
    "type": "function",
    "function": {
        "name": "replace_lines",
        "description": (
            "按行号范围替换文件内容。将文件的 start_line 到 end_line（含）替换为 new_content。"
            "\n\n典型用法：先用 read_file 查看文件内容（输出带行号），"
            "确定要替换的行号范围后，用本工具直接替换。"
            "\n\n适合以下场景："
            "\n- 替换整个函数体（如第 10-25 行）"
            "\n- 替换一个 class 的某几个方法"
            "\n- 修改配置文件的某一段"
            "\n- 删除若干行（new_content 设为空字符串）"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "要编辑的文件路径（相对于项目根目录）",
                },
                "start_line": {
                    "type": "integer",
                    "description": "起始行号（从 1 开始，包含该行）。对应 read_file 输出中的行号。",
                },
                "end_line": {
                    "type": "integer",
                    "description": "结束行号（包含该行）。对应 read_file 输出中的行号。",
                },
                "new_content": {
                    "type": "string",
                    "description": "替换后的新内容（不含末尾换行）。设为空字符串可删除指定行。",
                },
            },
            "required": ["file_path", "start_line", "end_line", "new_content"],
        },
    },
}

AGENT_INSERT_LINES_TOOL = {
    "type": "function",
    "function": {
        "name": "insert_lines",
        "description": (
            "在文件的指定行号之后插入新内容。"
            "\n\n典型用法：先用 read_file 查看文件确定插入位置，再用本工具插入。"
            "\n\n适合以下场景："
            "\n- 在某个函数后添加新函数（after_line 设为该函数最后一行的行号）"
            "\n- 在 import 块后添加新 import（after_line 设为最后一个 import 的行号）"
            "\n- 在文件开头插入（after_line=0）"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "要编辑的文件路径（相对于项目根目录）",
                },
                "after_line": {
                    "type": "integer",
                    "description": (
                        "在哪个行号之后插入。0 = 文件开头（第 1 行之前），"
                        "5 = 第 5 行之后。对应 read_file 输出中的行号。"
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "要插入的文本内容",
                },
            },
            "required": ["file_path", "after_line", "content"],
        },
    },
}

AGENT_SEARCH_IN_FILES_TOOL = {
    "type": "function",
    "function": {
        "name": "search_in_files",
        "description": "在工作区内搜索指定文本，返回匹配的文件和行内容。",
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
            },
            "required": ["keyword"],
        },
    },
}

AGENT_RUN_COMMAND_TOOL = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "在工作区内执行 shell 命令（如运行测试、检查语法等）。"
            "命令在工作区根目录执行，不允许跳出工作区。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 shell 命令",
                },
            },
            "required": ["command"],
        },
    },
}

AGENT_FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish_task",
        "description": (
            "标记任务完成。当你认为所有必要的代码修改已完成且测试通过时调用此工具。"
            "提供修改总结和风险评估。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "本次修改的简要总结",
                },
                "modified_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "已修改的文件路径列表",
                },
                "risk_level": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "修改的风险评估",
                },
                "test_result": {
                    "type": "string",
                    "description": "测试执行结果摘要",
                },
            },
            "required": ["summary"],
        },
    },
}

# 审查专用工具
AGENT_REVIEW_FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_review",
        "description": "提交审查结果。在完成所有文件审查后调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["pass", "needs_improvement", "reject"],
                    "description": "审查结论",
                },
                "score": {
                    "type": "integer",
                    "description": "评分 1-10，>=7 为通过",
                },
                "summary": {
                    "type": "string",
                    "description": "审查总结",
                },
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "severity": {
                                "type": "string",
                                "enum": ["critical", "major", "minor", "suggestion"],
                            },
                            "file": {"type": "string"},
                            "message": {"type": "string"},
                            "suggestion": {"type": "string"},
                        },
                        "required": ["severity", "file", "message"],
                    },
                    "description": "审查发现列表",
                },
                "improvement_suggestions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "改进建议",
                },
            },
            "required": ["verdict", "score", "summary"],
        },
    },
}

# 工具集合
FULLSTACK_EXPERT_TOOLS = [
    AGENT_READ_FILE_TOOL,
    AGENT_LIST_DIRECTORY_TOOL,
    AGENT_WRITE_FILE_TOOL,
    AGENT_EDIT_FILE_TOOL,
    AGENT_REPLACE_LINES_TOOL,
    AGENT_INSERT_LINES_TOOL,
    AGENT_SEARCH_IN_FILES_TOOL,
    AGENT_RUN_COMMAND_TOOL,
    AGENT_FINISH_TOOL,
]

REVIEWER_TOOLS = [
    AGENT_READ_FILE_TOOL,
    AGENT_LIST_DIRECTORY_TOOL,
    AGENT_SEARCH_IN_FILES_TOOL,
    AGENT_RUN_COMMAND_TOOL,
    AGENT_REVIEW_FINISH_TOOL,
]
