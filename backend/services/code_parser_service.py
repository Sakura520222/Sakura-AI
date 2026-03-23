"""代码解析服务

提供语法感知的代码分块功能：
- Python: 按类、函数、方法分块
- JavaScript/TypeScript: 按函数、类分块
- 其他语言: 按代码块和逻辑段分块
- Context Padding: 为每个代码块添加语义上下文
"""

from typing import List, Dict, Any, Optional
from loguru import logger
import re
from pathlib import Path
from dataclasses import dataclass


@dataclass
class CodeChunk:
    """代码块数据结构"""

    id: str
    content: str
    metadata: Dict[str, Any]
    start_line: int
    end_line: int


class CodeParserService:
    """代码解析服务

    提供语法感知的代码分块功能，支持多种编程语言
    """

    # 支持的语言及其文件扩展名
    LANGUAGE_MAP = {
        "python": [".py"],
        "javascript": [".js", ".jsx", ".mjs"],
        "typescript": [".ts", ".tsx"],
        "go": [".go"],
        "java": [".java"],
        "rust": [".rs"],
        "cpp": [".cpp", ".cc", ".cxx", ".hpp", ".h"],
        "c": [".c", ".h"],
        "csharp": [".cs"],
        "php": [".php"],
        "ruby": [".rb"],
        "swift": [".swift"],
        "kotlin": [".kt", ".kts"],
    }

    def __init__(
        self,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        enable_context_padding: bool = True,
    ):
        """初始化代码解析服务

        Args:
            chunk_size: 代码块大小（字符数）
            chunk_overlap: 代码块重叠大小
            enable_context_padding: 是否启用上下文填充
        """
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.enable_context_padding = enable_context_padding

    def detect_language(self, file_path: str) -> Optional[str]:
        """检测文件的语言类型

        Args:
            file_path: 文件路径

        Returns:
            语言名称，如果无法识别则返回 None
        """
        ext = Path(file_path).suffix.lower()
        for language, extensions in self.LANGUAGE_MAP.items():
            if ext in extensions:
                return language
        return None

    def parse_code_file(
        self,
        file_path: str,
        content: str,
        repo_full_name: str,
        pr_number: Optional[int] = None,
        commit_sha: Optional[str] = None,
    ) -> List[CodeChunk]:
        """解析代码文件为代码块

        Args:
            file_path: 文件路径
            content: 文件内容
            repo_full_name: 仓库名称
            pr_number: PR编号（可选）
            commit_sha: Commit SHA（可选）

        Returns:
            代码块列表
        """
        language = self.detect_language(file_path)

        # 根据语言选择解析策略
        if language == "python":
            chunks = self._parse_python(content, file_path)
        elif language in ("javascript", "typescript"):
            chunks = self._parse_javascript_typescript(content, file_path)
        elif language == "go":
            chunks = self._parse_go(content, file_path)
        elif language in ("java", "csharp", "kotlin"):
            chunks = self._parse_java_like(content, file_path)
        else:
            # 使用通用解析策略
            chunks = self._parse_generic(content, file_path)

        # 添加元数据和上下文填充
        enriched_chunks = []
        for chunk in chunks:
            metadata = {
                "file_path": file_path,
                "language": language or "unknown",
                "repo_full_name": repo_full_name,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
            }

            # 添加PR关联
            if pr_number is not None:
                metadata["pr_number"] = str(pr_number)

            # 添加Commit SHA
            if commit_sha is not None:
                metadata["commit_sha"] = commit_sha

            # 添加函数/类名（如果有）
            if "function_name" in chunk.metadata:
                metadata["function_name"] = chunk.metadata["function_name"]
            if "class_name" in chunk.metadata:
                metadata["class_name"] = chunk.metadata["class_name"]

            # 上下文填充
            enriched_content = (
                self._add_context_padding(chunk.content, metadata)
                if self.enable_context_padding
                else chunk.content
            )

            enriched_chunks.append(
                CodeChunk(
                    id=chunk.id,
                    content=enriched_content,
                    metadata=metadata,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                )
            )

        logger.debug(
            f"解析文件 {file_path} ({language}): 生成 {len(enriched_chunks)} 个代码块"
        )
        return enriched_chunks

    def _add_context_padding(self, content: str, metadata: Dict[str, Any]) -> str:
        """为代码块添加语义上下文

        Args:
            content: 原始代码内容
            metadata: 元数据

        Returns:
            添加了上下文的代码内容
        """
        context_parts = []

        # 文件路径
        file_path = metadata.get("file_path", "unknown")
        context_parts.append(f"[File: {file_path}]")

        # 类名
        if "class_name" in metadata:
            context_parts.append(f"[Class: {metadata['class_name']}]")

        # 函数名
        if "function_name" in metadata:
            context_parts.append(f"[Function: {metadata['function_name']}]")

        # 语言
        language = metadata.get("language", "unknown")
        context_parts.append(f"[Language: {language}]")

        # 组合上下文和内容
        context_header = " ".join(context_parts)
        return f"{context_header}\n{content}"

    def _parse_python(self, content: str, file_path: str) -> List[CodeChunk]:
        """解析Python代码

        按类、函数、方法进行分块

        Args:
            content: 代码内容
            file_path: 文件路径

        Returns:
            代码块列表
        """
        chunks = []
        lines = content.split("\n")

        # Python语法模式
        class_pattern = re.compile(r"^(\s*)class\s+(\w+)")
        function_pattern = re.compile(r"^(\s*)def\s+(\w+)")
        decorator_pattern = re.compile(r"^(\s*)@\w+")

        current_chunk = []
        current_indent = 0
        current_class = None
        current_function = None
        start_line = 1

        for i, line in enumerate(lines, 1):
            # 检查装饰器
            decorator_match = decorator_pattern.match(line)
            if decorator_match:
                indent = len(decorator_match.group(1))
                # 保存当前块
                if current_chunk:
                    chunks.append(
                        self._create_chunk(
                            current_chunk,
                            file_path,
                            start_line,
                            i - 1,
                            current_class,
                            current_function,
                        )
                    )
                    current_chunk = []
                    start_line = i
                current_chunk.append(line)
                current_indent = indent
                continue

            # 检查类定义
            class_match = class_pattern.match(line)
            if class_match:
                indent = len(class_match.group(1))
                # 保存当前块
                if current_chunk:
                    chunks.append(
                        self._create_chunk(
                            current_chunk,
                            file_path,
                            start_line,
                            i - 1,
                            current_class,
                            current_function,
                        )
                    )
                    current_chunk = []

                current_chunk.append(line)
                current_class = class_match.group(2)
                current_function = None
                current_indent = indent
                start_line = i
                continue

            # 检查函数定义
            function_match = function_pattern.match(line)
            if function_match:
                indent = len(function_match.group(1))
                # 顶层函数或方法
                if indent <= current_indent or current_chunk:
                    if current_chunk:
                        chunks.append(
                            self._create_chunk(
                                current_chunk,
                                file_path,
                                start_line,
                                i - 1,
                                current_class,
                                current_function,
                            )
                        )
                        current_chunk = []

                current_chunk.append(line)
                current_function = function_match.group(2)
                current_indent = indent
                start_line = i
                continue

            current_chunk.append(line)

            # 检查块大小
            chunk_text = "\n".join(current_chunk)
            if len(chunk_text) >= self.chunk_size + self.chunk_overlap:
                chunks.append(
                    self._create_chunk(
                        current_chunk,
                        file_path,
                        start_line,
                        i,
                        current_class,
                        current_function,
                    )
                )
                current_chunk = []
                start_line = i + 1

        # 保存最后的块
        if current_chunk:
            chunks.append(
                self._create_chunk(
                    current_chunk,
                    file_path,
                    start_line,
                    len(lines),
                    current_class,
                    current_function,
                )
            )

        return chunks

    def _parse_javascript_typescript(
        self, content: str, file_path: str
    ) -> List[CodeChunk]:
        """解析JavaScript/TypeScript代码

        按函数、类进行分块

        Args:
            content: 代码内容
            file_path: 文件路径

        Returns:
            代码块列表
        """
        chunks = []
        lines = content.split("\n")

        # JS/TS语法模式
        class_pattern = re.compile(r"^(\s*)class\s+(\w+)")
        function_pattern = re.compile(
            r"^(\s*)(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(|\(\w+\)\s*=>)"
        )
        method_pattern = re.compile(r"^(\s*)(\w+)\s*\(")

        current_chunk = []
        current_class = None
        current_function = None
        start_line = 1
        brace_count = 0
        in_function = False

        for i, line in enumerate(lines, 1):
            current_chunk.append(line)
            brace_count += line.count("{") - line.count("}")

            # 检查类定义
            class_match = class_pattern.match(line)
            if class_match:
                if current_chunk and brace_count == 0:
                    chunks.append(
                        self._create_chunk(
                            current_chunk[:-1],
                            file_path,
                            start_line,
                            i - 1,
                            current_class,
                            current_function,
                        )
                    )
                    current_chunk = [line]
                    start_line = i
                current_class = class_match.group(2)
                continue

            # 检查函数定义
            function_match = function_pattern.match(line)
            if function_match:
                if current_chunk and brace_count == 0:
                    chunks.append(
                        self._create_chunk(
                            current_chunk[:-1],
                            file_path,
                            start_line,
                            i - 1,
                            current_class,
                            current_function,
                        )
                    )
                    current_chunk = [line]
                    start_line = i
                func_name = function_match.group(2) or function_match.group(3)
                current_function = func_name
                in_function = True
                continue

            # 检查块大小
            chunk_text = "\n".join(current_chunk)
            if (
                len(chunk_text) >= self.chunk_size + self.chunk_overlap
                and brace_count == 0
            ):
                chunks.append(
                    self._create_chunk(
                        current_chunk,
                        file_path,
                        start_line,
                        i,
                        current_class,
                        current_function,
                    )
                )
                current_chunk = []
                start_line = i + 1

        # 保存最后的块
        if current_chunk:
            chunks.append(
                self._create_chunk(
                    current_chunk,
                    file_path,
                    start_line,
                    len(lines),
                    current_class,
                    current_function,
                )
            )

        return chunks

    def _parse_go(self, content: str, file_path: str) -> List[CodeChunk]:
        """解析Go代码

        按函数、方法、结构体进行分块

        Args:
            content: 代码内容
            file_path: 文件路径

        Returns:
            代码块列表
        """
        chunks = []
        lines = content.split("\n")

        # Go语法模式
        func_pattern = re.compile(r"^func\s+(?:\(\s*\w+\s+\*?\w+\s*\)\s+)?(\w+)")
        type_pattern = re.compile(r"^type\s+(\w+)\s+struct")

        current_chunk = []
        current_function = None
        start_line = 1

        for i, line in enumerate(lines, 1):
            current_chunk.append(line)

            # 检查函数定义
            func_match = func_pattern.search(line)
            if func_match:
                # 保存之前的块
                if len(current_chunk) > 1:
                    chunks.append(
                        self._create_chunk(
                            current_chunk[:-1],
                            file_path,
                            start_line,
                            i - 1,
                            None,
                            current_function,
                        )
                    )
                    current_chunk = [line]
                    start_line = i
                current_function = func_match.group(1)
                continue

            # 检查块大小
            chunk_text = "\n".join(current_chunk)
            if len(chunk_text) >= self.chunk_size + self.chunk_overlap:
                chunks.append(
                    self._create_chunk(
                        current_chunk, file_path, start_line, i, None, current_function
                    )
                )
                current_chunk = []
                start_line = i + 1

        # 保存最后的块
        if current_chunk:
            chunks.append(
                self._create_chunk(
                    current_chunk,
                    file_path,
                    start_line,
                    len(lines),
                    None,
                    current_function,
                )
            )

        return chunks

    def _parse_java_like(self, content: str, file_path: str) -> List[CodeChunk]:
        """解析Java类语言（Java, C#, Kotlin）

        按类、方法进行分块

        Args:
            content: 代码内容
            file_path: 文件路径

        Returns:
            代码块列表
        """
        # 使用简化的解析策略，类似JavaScript
        return self._parse_javascript_typescript(content, file_path)

    def _parse_generic(self, content: str, file_path: str) -> List[CodeChunk]:
        """通用代码解析

        基于缩进和代码块进行分块

        Args:
            content: 代码内容
            file_path: 文件路径

        Returns:
            代码块列表
        """
        chunks = []
        lines = content.split("\n")

        current_chunk = []
        start_line = 1
        current_indent = 0

        for i, line in enumerate(lines, 1):
            # 计算缩进
            stripped = line.lstrip()
            indent = len(line) - len(stripped)

            # 空行跳过
            if not stripped:
                current_chunk.append(line)
                continue

            # 检测缩进减少（新块开始）
            if current_chunk and indent < current_indent:
                chunk_text = "\n".join(current_chunk)
                if len(chunk_text) > 100:  # 最小块大小
                    chunks.append(
                        self._create_chunk(
                            current_chunk, file_path, start_line, i - 1, None, None
                        )
                    )
                    current_chunk = []
                    start_line = i

            current_chunk.append(line)
            current_indent = indent

            # 检查块大小
            chunk_text = "\n".join(current_chunk)
            if len(chunk_text) >= self.chunk_size + self.chunk_overlap:
                chunks.append(
                    self._create_chunk(
                        current_chunk, file_path, start_line, i, None, None
                    )
                )
                current_chunk = []
                start_line = i + 1

        # 保存最后的块
        if current_chunk:
            chunks.append(
                self._create_chunk(
                    current_chunk, file_path, start_line, len(lines), None, None
                )
            )

        return chunks

    def _create_chunk(
        self,
        lines: List[str],
        file_path: str,
        start_line: int,
        end_line: int,
        class_name: Optional[str],
        function_name: Optional[str],
    ) -> CodeChunk:
        """创建代码块

        Args:
            lines: 代码行列表
            file_path: 文件路径
            start_line: 起始行号
            end_line: 结束行号
            class_name: 类名（可选）
            function_name: 函数名（可选）

        Returns:
            代码块对象
        """
        content = "\n".join(lines).strip()

        # 生成唯一ID
        import hashlib

        content_hash = hashlib.md5(
            f"{file_path}:{start_line}:{end_line}".encode()
        ).hexdigest()[:12]

        chunk_id = f"chunk_{content_hash}"

        metadata = {}
        if class_name:
            metadata["class_name"] = class_name
        if function_name:
            metadata["function_name"] = function_name

        return CodeChunk(
            id=chunk_id,
            content=content,
            metadata=metadata,
            start_line=start_line,
            end_line=end_line,
        )


# 全局单例
_code_parser_instance: Optional[CodeParserService] = None


def get_code_parser(
    chunk_size: int = 500,
    chunk_overlap: int = 50,
    enable_context_padding: bool = True,
) -> CodeParserService:
    """获取代码解析服务单例"""
    global _code_parser_instance
    if _code_parser_instance is None:
        _code_parser_instance = CodeParserService(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            enable_context_padding=enable_context_padding,
        )
    return _code_parser_instance
