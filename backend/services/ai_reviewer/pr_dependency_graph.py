"""PR 依赖图生成模块

分析 PR 变更文件的 import/模块依赖关系，
通过 AI 或静态 import 分析生成 Mermaid 依赖图并注入到 PR body 中。
使用独立的 HTML 注释标记区域，与 PR Summary 共存。
"""

import asyncio
import re
from pathlib import PurePosixPath
from typing import Any

from loguru import logger

from backend.core.config import get_settings, get_strategy_config
from backend.services.ai_reviewer.api_client import AIApiClient
from backend.services.ai_reviewer.pr_summary import PRSummaryService
from backend.services.pr_analyzer import PRAnalysis, PRFileInfo
from backend.services.section_config_service import section_config_service

# 各语言的 import 语句正则模式；预编译到模块级别，避免每次扫描重复编译。
_IMPORT_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "python": [
        re.compile(r"^import\s+([\w.]+)", re.MULTILINE),
        re.compile(r"^from\s+([\w.]+)\s+import", re.MULTILINE),
    ],
    "javascript": [
        re.compile(r"""^import\s+.*?\s+from\s+['"]([^'"]+)['"]""", re.MULTILINE),
        re.compile(
            r"""^(?:import|require)\s*\(?\s*['"]([^'"]+)['"]\)?\s*;?""", re.MULTILINE
        ),
    ],
    "typescript": [
        re.compile(r"""^import\s+.*?\s+from\s+['"]([^'"]+)['"]""", re.MULTILINE),
        re.compile(
            r"""^(?:import|require)\s*\(?\s*['"]([^'"]+)['"]\)?\s*;?""", re.MULTILINE
        ),
    ],
    "go": [
        re.compile(r'^import\s+"([\w./\-]+)"\s*$', re.MULTILINE),
        re.compile(r'^\t"([\w./\-]+)"', re.MULTILINE),
        re.compile(r'^import\s+\w+\s+"([\w./\-]+)"', re.MULTILINE),
    ],
    "java": [
        re.compile(r"^import\s+([\w.]+)", re.MULTILINE),
    ],
    "rust": [
        re.compile(r"use\s+([\w:]+)", re.MULTILINE),
    ],
    "csharp": [
        re.compile(r"^using\s+([\w.]+)", re.MULTILINE),
    ],
    "cpp": [
        re.compile(r'#include\s*[<"]([^>"]+)[>"]', re.MULTILINE),
    ],
    "ruby": [
        re.compile(
            r"^(?:require|require_relative)\s+['\"]([^'\"]+)['\"]", re.MULTILINE
        ),
    ],
    "php": [
        re.compile(r"use\s+([\w\\]+)", re.MULTILINE),
        re.compile(
            r"^(?:require|include)(?:_once)?\s+['\"]([^'\"]+)['\"]", re.MULTILINE
        ),
    ],
    "swift": [
        re.compile(r"^import\s+(\w+)", re.MULTILINE),
    ],
    "kotlin": [
        re.compile(r"^import\s+([\w.]+)", re.MULTILINE),
    ],
}

# 文件扩展名到语言类型的映射
_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".h": "cpp",
    ".c": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".kts": "kotlin",
}


class PRDependencyGraphService:
    """PR 依赖图生成服务"""

    START_MARKER = "<!-- sakura-ai-depgraph-start -->"
    END_MARKER = "<!-- sakura-ai-depgraph-end -->"

    # import 语句通常出现在文件顶部，只扫描前 N 行以提升性能
    _IMPORT_SCAN_LINES: int = 150

    def __init__(self, api_client: AIApiClient, model: str = ""):
        self.api_client = api_client

    # ==================== 公开接口 ====================

    async def generate_dependency_graph(
        self,
        analysis: PRAnalysis,
        pr_info: dict[str, Any],
        pr: Any,
    ) -> str | None:
        """生成 PR 依赖图并注入到 PR Body

        Returns:
            Mermaid 图文本，失败或无依赖时返回 None
        """
        settings = get_settings()

        graph_files, total_file_count = await asyncio.to_thread(
            self._get_graph_files_sync, analysis, pr
        )

        # 大型 PR 裁剪
        priority_paths = (
            {file.path for file in analysis.code_files}
            if analysis.is_incremental
            else None
        )
        analysis_files = self._trim_files(
            graph_files, settings, priority_paths=priority_paths
        )

        # 增量审查只读取本轮变更文件内容；历史文件通过完整文件列表和旧图保留上下文。
        content_files = self._select_content_files(analysis, analysis_files)

        # 获取文件内容
        file_contents = await asyncio.to_thread(
            self._fetch_file_contents_sync, content_files, pr
        )

        graph_mode = await self._get_graph_mode()
        previous_graph = self._extract_previous_graph(pr_info.get("body", ""))
        if not file_contents and not (
            graph_mode == "ai" and analysis.is_incremental and previous_graph
        ):
            logger.info("无法获取任何变更文件内容，跳过依赖图生成")
            return None

        if graph_mode == "static":
            mermaid_graph = self._generate_static_mermaid(
                analysis_files,
                file_contents,
                max_nodes=settings.pr_dependency_graph_max_nodes,
                previous_graph=previous_graph,
            )
            mermaid_graph = self._validate_mermaid(mermaid_graph)
            if not mermaid_graph:
                logger.warning(
                    "静态依赖图分析未生成有效 Mermaid 图，跳过注入；"
                    f"候选文件数: {len(analysis_files)}, 可读取文件数: {len(file_contents)}"
                )
                return None

            await self.update_pr_body_with_graph(pr, mermaid_graph)
            logger.info(f"静态 PR 依赖图已生成，长度: {len(mermaid_graph)} 字符")
            return mermaid_graph

        # AI 模式才需要构建完整 import 上下文，静态模式会直接基于文件内容分析。
        import_context = self._build_import_context(analysis_files, file_contents)
        if not import_context.strip():
            logger.info("变更文件间无 import 依赖关系，跳过依赖图生成")
            return None

        # AI 生成 Mermaid
        system_prompt, user_message = self._build_prompts(
            import_context,
            pr_info,
            settings,
            previous_graph,
            file_count=total_file_count,
            code_file_count=len(graph_files),
            analyzed_file_count=len(analysis_files),
        )

        response = await self.api_client.call_with_retry(
            model="",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0.2,
            max_tokens=16000,
            role="summary",
        )

        if (
            not response.choices
            or not response.choices[0].message
            or not response.choices[0].message.content
        ):
            logger.warning("AI 返回的依赖图内容为空")
            return None

        raw_content = response.choices[0].message.content.strip()

        # 验证 Mermaid 语法
        mermaid_graph = self._validate_mermaid(raw_content)
        if not mermaid_graph:
            logger.info("AI 未生成有效的 Mermaid 图，跳过依赖图注入")
            return None

        # 注入 PR Body
        await self.update_pr_body_with_graph(pr, mermaid_graph)
        logger.info(f"PR 依赖图已生成，长度: {len(mermaid_graph)} 字符")
        return mermaid_graph

    async def update_pr_body_with_graph(
        self,
        pr: Any,
        mermaid_graph: str,
    ) -> None:
        """将依赖图注入到 PR Body

        使用独立的 HTML 注释标记，与 PR Summary 共存。
        从 GitHub 读取最新 body，避免用过期的 pr.body 缓存。
        """
        current_body = await asyncio.to_thread(lambda: pr.body or "")
        original = self._extract_original_body(current_body)
        graph_block = self._build_graph_block(mermaid_graph)

        # 保留 PR Summary 块（如果存在）
        summary_block = self._extract_summary_block(current_body)

        parts = []
        if original.strip():
            parts.append(original)
        if summary_block:
            parts.append(summary_block)
        parts.append(graph_block)

        new_body = "\n\n".join(parts)
        await asyncio.to_thread(pr.edit, body=new_body)
        logger.info("PR body 已更新（注入依赖图）")

    # ==================== 内部方法 ====================

    @staticmethod
    async def _get_graph_mode() -> str:
        """读取依赖图生成模式（ai/static）。

        统一走节配置体系：strategy.pr_dependency_graph 节的 mode 字段优先，
        回退旧动态配置键 pr_dependency_graph_mode（兼容历史部署）。
        """
        mode = await section_config_service.resolve_depgraph_mode()
        mode = str(mode or "static").strip().lower()
        if mode not in {"ai", "static"}:
            logger.warning(f"未知 PR 依赖图模式: {mode}，回退到 static")
            return "static"
        return mode

    @staticmethod
    def _get_graph_files_sync(
        analysis: PRAnalysis, pr: Any
    ) -> tuple[list[PRFileInfo], int]:
        """获取依赖图使用的累计代码文件元信息。"""
        if not analysis.is_incremental:
            return list(analysis.code_files), analysis.total_files

        strategy_config = get_strategy_config()
        pr_files = list(pr.get_files())
        code_files: list[PRFileInfo] = []
        for file in pr_files:
            if strategy_config.should_skip_file(file.filename):
                continue
            if not strategy_config.is_code_file(file.filename):
                continue
            code_files.append(
                PRFileInfo(
                    path=file.filename,
                    status=file.status,
                    additions=file.additions,
                    deletions=file.deletions,
                    changes=file.changes,
                    patch=None,
                    is_code_file=True,
                )
            )

        changed_files = getattr(pr, "changed_files", None)
        total_file_count = (
            changed_files if isinstance(changed_files, int) else len(pr_files)
        )
        logger.info(
            "增量依赖图使用 PR 累计文件元信息: 总文件数 {}, 代码文件数 {}",
            total_file_count,
            len(code_files),
        )
        return code_files, total_file_count

    # GitHub File.status 对删除文件返回 "removed"（透传 GitHub API 原始值），
    # 而非字面量 "deleted"；统一用集合匹配避免漏判。
    _DELETED_STATUSES: frozenset[str] = frozenset({"deleted", "removed"})

    @classmethod
    def _is_deleted_file(cls, status: str) -> bool:
        """判断文件是否为删除状态（兼容 GitHub 的 "removed" 与字面量 "deleted"）。"""
        return status in cls._DELETED_STATUSES

    @staticmethod
    def _trim_files(
        code_files: list[PRFileInfo],
        settings: Any,
        priority_paths: set[str] | None = None,
    ) -> list[PRFileInfo]:
        """大型 PR 裁剪：按变更量排序取 top N 文件"""
        files = [
            f
            for f in code_files
            if not PRDependencyGraphService._is_deleted_file(f.status)
        ]
        max_files = settings.pr_dependency_graph_max_files
        if len(files) > max_files:
            priority_paths = priority_paths or set()
            priority_files = sorted(
                (file for file in files if file.path in priority_paths),
                key=lambda file: file.changes,
                reverse=True,
            )
            remaining_files = sorted(
                (file for file in files if file.path not in priority_paths),
                key=lambda file: file.changes,
                reverse=True,
            )
            files = (priority_files + remaining_files)[:max_files]
            logger.info(f"PR 变更文件数超过限制，只分析 top {max_files} 个文件")
        return files

    @staticmethod
    def _select_content_files(
        analysis: PRAnalysis, graph_files: list[PRFileInfo]
    ) -> list[PRFileInfo]:
        """选择需要获取内容的文件，增量模式仅包含本轮变更。"""
        if not analysis.is_incremental:
            return graph_files

        graph_paths = {file.path for file in graph_files}
        return [
            file
            for file in analysis.code_files
            if not PRDependencyGraphService._is_deleted_file(file.status)
            and file.path in graph_paths
        ]

    @staticmethod
    def _fetch_file_contents_sync(files: list[PRFileInfo], pr: Any) -> dict[str, str]:
        """同步获取变更文件的代码内容"""
        import base64

        repo = pr.base.repo
        ref = pr.head.sha
        file_contents: dict[str, str] = {}

        for file_info in files:
            try:
                content_file = repo.get_contents(file_info.path, ref=ref)
                if content_file and hasattr(content_file, "content"):
                    content = base64.b64decode(content_file.content).decode(
                        "utf-8", errors="ignore"
                    )
                    file_contents[file_info.path] = content
            except Exception as e:
                logger.warning(f"无法获取文件 {file_info.path}: {e}")

        return file_contents

    @staticmethod
    def _get_language(file_path: str) -> str | None:
        """根据文件扩展名获取语言类型"""
        for ext, lang in _EXT_TO_LANG.items():
            if file_path.endswith(ext):
                return lang
        return None

    def _extract_imports(self, file_path: str, content: str) -> list[str]:
        """从代码内容中提取 import 语句（只扫描文件顶部）"""
        lang = self._get_language(file_path)
        if not lang:
            return []

        # 只扫描文件顶部
        top_content = "\n".join(content.split("\n")[: self._IMPORT_SCAN_LINES])

        patterns = _IMPORT_PATTERNS.get(lang, [])
        imports: list[str] = []
        for pattern in patterns:
            for match in pattern.finditer(top_content):
                imp = match.group(1).strip()
                if imp and imp not in imports:
                    imports.append(imp)
        return imports

    def _build_import_context(
        self,
        code_files: list[PRFileInfo],
        file_contents: dict[str, str],
    ) -> str:
        """构建 AI 分析的上下文文本"""
        lines: list[str] = []

        # 文件列表
        lines.append("## 变更文件")
        for i, f in enumerate(code_files, 1):
            status_icon = {"added": "+", "modified": "~", "renamed": "R"}.get(
                f.status, "?"
            )
            lines.append(f"{i}. [{status_icon}] {f.path}")
        lines.append("")

        # 每个文件的 import 信息
        lines.append("## 文件依赖关系")
        for f in code_files:
            content = file_contents.get(f.path, "")
            if not content:
                continue
            imports = self._extract_imports(f.path, content)
            lines.append(f"### {f.path}")
            if imports:
                lines.append("  imports:")
                for imp in imports:
                    lines.append(f"  - {imp}")
            else:
                lines.append("  imports: (无)")
            lines.append("")

        return "\n".join(lines)

    def _generate_static_mermaid(
        self,
        code_files: list[PRFileInfo],
        file_contents: dict[str, str],
        max_nodes: int,
        previous_graph: str | None = None,
    ) -> str:
        """基于 import 关系静态生成 Mermaid 依赖图。

        增量审查时传入 previous_graph，合并历史节点与依赖边，使静态图覆盖
        全量 PR 依赖而非仅本轮变更文件。
        """
        available_files = [
            f
            for f in code_files
            if f.path in file_contents and self._get_language(f.path)
        ]
        if not available_files:
            return ""

        normalized_paths = [self._normalize_path(f.path) for f in available_files]
        path_aliases = dict(
            zip(
                normalized_paths,
                (self._build_file_aliases(f.path) for f in available_files),
            )
        )
        edges: set[tuple[str, str]] = set()

        for source_file, source_path in zip(available_files, normalized_paths):
            imports = self._extract_imports(
                source_file.path, file_contents.get(source_file.path, "")
            )
            for imported in imports:
                target_path = self._resolve_import_to_changed_file(
                    source_file.path,
                    imported,
                    path_aliases,
                )
                if target_path and target_path != source_path:
                    edges.add((source_path, target_path))

        # 合并历史图节点与边：增量审查只读取本轮变更文件内容，历史依赖通过
        # previous_graph 文本补全，避免为历史文件额外发起 GitHub API 调用。
        # 历史节点按 previous_graph 中出现顺序追加，保证 max_nodes 截断结果稳定。
        candidate_paths = list(path_aliases.keys())
        if previous_graph:
            node_id_to_path, historical_edges = self._parse_previous_graph(
                previous_graph
            )
            if historical_edges:
                edges |= historical_edges
                endpoints = {path for edge in historical_edges for path in edge}
                existing_paths = set(candidate_paths)
                for path in node_id_to_path.values():
                    if path in endpoints and path not in existing_paths:
                        candidate_paths.append(path)
                        existing_paths.add(path)

        selected_nodes = self._select_static_graph_nodes(
            candidate_paths,
            edges,
            max_nodes,
        )
        if not selected_nodes:
            return ""

        selected_node_set = set(selected_nodes)
        selected_edges = [
            edge
            for edge in sorted(edges)
            if edge[0] in selected_node_set and edge[1] in selected_node_set
        ]

        node_ids = {path: f"N{i}" for i, path in enumerate(selected_nodes, 1)}
        lines = ["graph TD"]
        for path in selected_nodes:
            lines.append(f'    {node_ids[path]}["{self._escape_mermaid_label(path)}"]')

        for source_path, target_path in selected_edges:
            lines.append(f"    {node_ids[source_path]} --> {node_ids[target_path]}")

        if edges and len(selected_edges) < len(edges):
            omitted = len(edges) - len(selected_edges)
            lines.append(f'    OMITTED["... {omitted} more dependencies omitted"]')

        return "\n".join(lines)

    @staticmethod
    def _normalize_path(path: str) -> str:
        """统一文件路径分隔符。"""
        return PRDependencyGraphService._strip_leading_current_dirs(
            path.replace("\\", "/")
        )

    @staticmethod
    def _strip_leading_current_dirs(value: str) -> str:
        """仅移除开头的 ./ 片段，不误删合法的 . 或 / 字符。"""
        normalized = value
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized

    @classmethod
    def _build_file_aliases(cls, file_path: str) -> set[str]:
        """为文件路径生成可匹配 import 字符串的候选别名。"""
        normalized = cls._normalize_path(file_path)
        path = PurePosixPath(normalized)
        without_suffix = str(path.with_suffix("")) if path.suffix else normalized
        basename = path.stem

        aliases = {normalized, without_suffix, basename}
        aliases.add(without_suffix.replace("/", "."))

        parts = list(PurePosixPath(without_suffix).parts)
        for index in range(1, len(parts)):
            suffix = "/".join(parts[index:])
            if suffix:
                aliases.add(suffix)
                aliases.add(suffix.replace("/", "."))

        if path.stem == "__init__" and len(parts) > 1:
            package = "/".join(parts[:-1])
            aliases.add(package)
            aliases.add(package.replace("/", "."))

        if (
            path.name in {"index.js", "index.jsx", "index.ts", "index.tsx"}
            and len(parts) > 1
        ):
            module = "/".join(parts[:-1])
            aliases.add(module)
            aliases.add(module.replace("/", "."))

        return {
            stripped
            for alias in aliases
            if (stripped := cls._strip_leading_current_dirs(alias))
        }

    @classmethod
    def _normalize_import(cls, source_path: str, import_path: str) -> set[str]:
        """将 import 字符串转换为可能的路径/模块名候选。"""
        raw_import = import_path.strip().strip("'\"")
        if not raw_import:
            return set()

        normalized = raw_import.replace("\\", "/")
        candidates = {
            cls._strip_leading_current_dirs(normalized),
            # 仅移除模块风格单点前缀（如 .mymodule），路径式 ./ 会在下一分支解析。
            normalized.replace("/", ".").removeprefix("."),
        }

        # 处理 Python 多级相对导入，如 "..utils" 或 "...pkg"。
        # "./"、"../" 属于 JS/TS/Ruby 等路径式相对导入，交给下一分支解析。
        if normalized.startswith(".") and not normalized.startswith(("./", "../")):
            leading_dot_count = len(normalized) - len(normalized.lstrip("."))
            module_part = normalized[leading_dot_count:]
            source_parts = list(PurePosixPath(cls._normalize_path(source_path)).parts)
            package_parts = source_parts[:-1]
            keep_parts = package_parts[
                : max(0, len(package_parts) - leading_dot_count + 1)
            ]
            resolved_parts = keep_parts + ([module_part] if module_part else [])
            resolved = "/".join(part for part in resolved_parts if part)
            candidates.add(resolved)
            candidates.add(resolved.replace("/", "."))
        elif normalized.startswith("."):
            source_dir = PurePosixPath(cls._normalize_path(source_path)).parent
            relative_target = source_dir.joinpath(normalized)
            resolved_parts: list[str] = []
            for part in relative_target.parts:
                if part in {"", "."}:
                    continue
                if part == "..":
                    if resolved_parts:
                        resolved_parts.pop()
                    continue
                resolved_parts.append(part)
            resolved = "/".join(resolved_parts)
            candidates.add(resolved)
            candidates.add(resolved.replace("/", "."))
        elif normalized.startswith("@/"):
            # 轻量约定：@/ 映射到仓库常见源码根路径的后缀匹配，
            # 不解析 tsconfig/jsconfig paths 等项目级 alias 配置。
            alias_path = normalized[2:]
            candidates.add(alias_path)
            candidates.add(alias_path.replace("/", "."))

        expanded = set(candidates)
        for candidate in candidates:
            if candidate.endswith(".*"):
                expanded.add(candidate[:-2])
            if candidate.endswith("/*"):
                expanded.add(candidate[:-2])
        return {
            candidate
            for candidate in expanded
            if candidate and candidate != "/" and candidate.strip(".")
        }

    @classmethod
    def _resolve_import_to_changed_file(
        cls,
        source_path: str,
        import_path: str,
        path_aliases: dict[str, set[str]],
    ) -> str | None:
        """将 import 解析到变更文件路径。"""
        import_candidates = cls._normalize_import(source_path, import_path)
        if not import_candidates:
            return None

        for target_path, aliases in path_aliases.items():
            if import_candidates & aliases:
                return target_path

        for candidate in import_candidates:
            for target_path, aliases in path_aliases.items():
                if any(
                    alias.startswith((f"{candidate}.", f"{candidate}/"))
                    for alias in aliases
                ):
                    return target_path
        return None

    @staticmethod
    def _select_static_graph_nodes(
        file_paths: list[str],
        edges: set[tuple[str, str]],
        max_nodes: int,
    ) -> list[str]:
        """按依赖关系优先选择静态图节点。"""
        max_nodes = max(1, max_nodes)
        if not edges:
            return file_paths[:max_nodes]

        connected_node_set = {path for edge in edges for path in edge}
        connected_nodes = [path for path in file_paths if path in connected_node_set]
        selected = connected_nodes[:max_nodes]
        if len(selected) < max_nodes:
            for path in file_paths:
                if path not in selected:
                    selected.append(path)
                    if len(selected) >= max_nodes:
                        break
        return selected

    @staticmethod
    def _escape_mermaid_label(label: str) -> str:
        """转义 Mermaid 节点 label。"""
        replacements = {
            "&": "&amp;",
            '"': "'",
            "<": "&lt;",
            ">": "&gt;",
            "{": "&#123;",
            "}": "&#125;",
            "[": "&#91;",
            "]": "&#93;",
            "|": "&#124;",
            "(": "&#40;",
            ")": "&#41;",
            "#": "&#35;",
            "%": "&#37;",
        }
        normalized = label.replace("\\", "/")
        return "".join(replacements.get(char, char) for char in normalized)

    @staticmethod
    def _unescape_mermaid_label(label: str) -> str:
        """反转义 Mermaid 节点 label（_escape_mermaid_label 的逆操作）。

        '"' 与 '\\' 的转义不可逆，还原时保留字面量。用于从 previous_graph
        还原节点 label 为原始文件路径。
        """
        reverse = {
            "&amp;": "&",
            "&lt;": "<",
            "&gt;": ">",
            "&#123;": "{",
            "&#125;": "}",
            "&#91;": "[",
            "&#93;": "]",
            "&#124;": "|",
            "&#40;": "(",
            "&#41;": ")",
            "&#35;": "#",
            "&#37;": "%",
        }
        return re.sub(
            r"&#?\w+;",
            lambda match: reverse.get(match.group(0), match.group(0)),
            label,
        )

    @classmethod
    def _parse_previous_graph(
        cls, previous_graph: str
    ) -> tuple[dict[str, str], set[tuple[str, str]]]:
        """解析历史 Mermaid 图的节点定义与依赖边。

        仅识别静态/AI 生成图常见的 ``N1["label"]`` 节点定义与 ``N1 --> N2`` 边，
        用于增量审查时合并历史依赖上下文。label 经 _unescape_mermaid_label 还原，
        路径经 _normalize_path 统一分隔符。

        逐行用 ``str.find``/``strip`` 手工定界（不用正则），单行 O(行长)、总体
        线性，从根本上规避正则多项式回溯（CodeQL ReDoS）。每个节点/边需独占一行
        （Sakura 生成格式）。两遍扫描：先收齐节点，再解析边，使边能解析到完整
        节点映射。

        Returns:
            (node_id -> normalized_path 映射, 归一化后的边集合)
        """
        node_id_to_path: dict[str, str] = {}
        for line in previous_graph.splitlines():
            node = cls._parse_node_line(line)
            if node is not None:
                node_id, label = node
                node_id_to_path[node_id] = cls._normalize_path(
                    cls._unescape_mermaid_label(label)
                )

        edges: set[tuple[str, str]] = set()
        for line in previous_graph.splitlines():
            edge = cls._parse_edge_line(line)
            if edge is not None:
                source = node_id_to_path.get(edge[0])
                target = node_id_to_path.get(edge[1])
                if source and target and source != target:
                    edges.add((source, target))

        return node_id_to_path, edges

    @staticmethod
    def _parse_node_line(line: str) -> tuple[str, str] | None:
        """从 ``<id>["<label>"]`` 行提取 (id, label)；不匹配返回 None。

        用 ``str.find`` 定界（无正则），单行 O(行长)。id 经 strip 后须非空且不含
        空白/括号/引号（等价于原 ``[^\\[\\]\\s"]+`` 语义）。
        """
        start = line.find('["')
        if start <= 0:
            return None
        end = line.find('"]', start + 2)
        if end == -1:
            return None
        node_id = line[:start].strip()
        label = line[start + 2 : end]
        if not node_id or any(c.isspace() or c in '[]"' for c in node_id):
            return None
        return node_id, label

    @staticmethod
    def _parse_edge_line(line: str) -> tuple[str, str] | None:
        """从 ``<id> --> <id>`` 行提取 (source_id, target_id)；不匹配返回 None。

        用 ``str.find`` 定界（无正则），单行 O(行长)。两端 id 经 strip 后须非空
        且不含空白/括号/引号。
        """
        arrow = line.find("-->")
        if arrow <= 0:
            return None
        source = line[:arrow].strip()
        target = line[arrow + 3 :].strip()
        if not source or not target:
            return None
        if any(c.isspace() or c in '[]"' for c in source + target):
            return None
        return source, target

    @staticmethod
    def _build_prompts(
        import_context: str,
        pr_info: dict[str, Any],
        settings: Any,
        previous_graph: str | None = None,
        *,
        file_count: int,
        code_file_count: int,
        analyzed_file_count: int,
    ) -> tuple[str, str]:
        """构建系统提示词和用户消息"""
        config = get_strategy_config()
        depgraph_cfg = config.config.get("pr_dependency_graph", {})

        system_prompt = depgraph_cfg.get(
            "system_prompt",
            "你是代码依赖分析专家。根据提供的 PR 变更文件及其 import 信息，"
            "生成 Mermaid graph TD 语法的依赖关系图。\n"
            "节点总数不超过 {max_nodes} 个。只输出纯 Mermaid 代码块。",
        ).replace("{max_nodes}", str(settings.pr_dependency_graph_max_nodes))

        user_template = depgraph_cfg.get(
            "user_template",
            "请根据以下 PR 变更信息生成依赖关系图：\n\n"
            "PR 标题: {title}\n"
            "总文件数: {file_count}  代码文件数: {code_file_count}  "
            "本轮分析文件数: {analyzed_file_count}\n\n"
            "文件依赖关系:\n{import_context}",
        )

        # 三个计数控件均为必填：唯一调用方 generate_dependency_graph 始终从
        # analysis/graph_files 提供真实值；pr_info（webhook 构造）从不携带 code_files，
        # 不再用恒为 0 的误导性回退。
        user_message = user_template.format(
            title=pr_info.get("title", ""),
            file_count=file_count,
            code_file_count=code_file_count,
            analyzed_file_count=analyzed_file_count,
            import_context=import_context,
        )

        # 增量更新时，注入上一次的依赖图让 AI 在其基础上整合
        if previous_graph:
            user_message += (
                "\n\n---\n"
                "以下是该 PR 之前的依赖图，请在此基础上根据新的变更信息更新依赖图"
                "（保留未变更部分的节点命名和布局风格，补充新增依赖，移除已不存在的依赖）：\n\n"
                f"```mermaid\n{previous_graph}\n```"
            )

        return system_prompt, user_message

    @staticmethod
    def _extract_mermaid_fence(text: str) -> str | None:
        """使用线性分隔符查找提取 Mermaid fence，避免正则回溯。"""
        fence = "```mermaid"
        search_start = 0
        while True:
            fence_start = text.find(fence, search_start)
            if fence_start == -1:
                return None
            language_end = fence_start + len(fence)
            code_start = text.find("\n", language_end)
            if code_start == -1:
                return None
            if not text[language_end:code_start].strip():
                break
            search_start = language_end

        code_end = text.find("```", code_start + 1)
        if code_end == -1:
            return None
        return text[code_start + 1 : code_end].strip()

    @staticmethod
    def _validate_mermaid(mermaid_text: str) -> str:
        """验证并提取 Mermaid 语法"""
        fenced_mermaid = PRDependencyGraphService._extract_mermaid_fence(mermaid_text)
        if fenced_mermaid is not None:
            mermaid_text = fenced_mermaid

        # 检查是否包含有效图类型声明
        if not re.search(r"^(graph|flowchart)\s+", mermaid_text, re.MULTILINE):
            return ""

        # 长度限制
        if len(mermaid_text) > 4000:
            lines = mermaid_text.split("\n")
            mermaid_text = "\n".join(lines[:100])

        return mermaid_text

    def _build_graph_block(self, mermaid_graph: str) -> str:
        """构建带 HTML 注释标记的依赖图块"""
        return (
            f"{self.START_MARKER}\n\n"
            f"## 依赖图\n\n"
            f"```mermaid\n{mermaid_graph}\n```\n\n"
            f"{self.END_MARKER}"
        )

    def _extract_original_body(self, body: str) -> str:
        """从 PR body 中提取不含任何 AI 注入区域的原始内容"""
        if not body:
            return ""

        # 移除依赖图标记区域
        depgraph_pattern = (
            re.escape(self.START_MARKER) + r".*?" + re.escape(self.END_MARKER)
        )
        clean = re.sub(depgraph_pattern, "", body, flags=re.DOTALL)

        # 移除 PR Summary 标记区域
        summary_pattern = (
            re.escape(PRSummaryService.START_MARKER)
            + r".*?"
            + re.escape(PRSummaryService.END_MARKER)
        )
        clean = re.sub(summary_pattern, "", clean, flags=re.DOTALL).strip()

        return clean

    @staticmethod
    def _extract_summary_block(body: str) -> str | None:
        """从 PR body 中提取 PR Summary 块（保留原样）"""
        if not body:
            return None

        pattern = (
            re.escape(PRSummaryService.START_MARKER)
            + r".*?"
            + re.escape(PRSummaryService.END_MARKER)
        )
        match = re.search(pattern, body, flags=re.DOTALL)
        return match.group(0).strip() if match else None

    def _extract_previous_graph(self, body: str) -> str | None:
        """从 PR body 中提取上一次的依赖图 Mermaid 内容"""
        if not body:
            return None

        marker_start = body.find(self.START_MARKER)
        if marker_start == -1:
            return None

        content_start = marker_start + len(self.START_MARKER)
        marker_end = body.find(self.END_MARKER, content_start)
        if marker_end == -1:
            return None

        content = body[content_start:marker_end].strip()
        if not content:
            return None

        # 使用确定性的分隔符查找，避免在不受信任的 PR body 上进行回溯式正则匹配。
        return self._extract_mermaid_fence(content)
