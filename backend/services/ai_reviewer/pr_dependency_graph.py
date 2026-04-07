"""PR 依赖图生成模块

分析 PR 变更文件的 import/模块依赖关系，
通过 AI 生成 Mermaid 依赖图并注入到 PR body 中。
使用独立的 HTML 注释标记区域，与 PR Summary 共存。
"""

import asyncio
import re
from typing import Any, Dict, List

from loguru import logger

from backend.core.config import get_settings, get_strategy_config
from backend.services.ai_reviewer.api_client import AIApiClient
from backend.services.ai_reviewer.pr_summary import PRSummaryService
from backend.services.pr_analyzer import PRAnalysis, PRFileInfo


# 各语言的 import 语句正则模式
_IMPORT_PATTERNS: Dict[str, List[str]] = {
    "python": [
        r"^import\s+([\w.]+)",
        r"^from\s+([\w.]+)\s+import",
    ],
    "javascript": [
        r"""^import\s+.*?\s+from\s+['"]([^'"]+)['"]""",
        r"""^(?:import|require)\s*\(?\s*['"]([^'"]+)['"]\)?\s*;?""",
    ],
    "typescript": [
        r"""^import\s+.*?\s+from\s+['"]([^'"]+)['"]""",
        r"""^(?:import|require)\s*\(?\s*['"]([^'"]+)['"]\)?\s*;?""",
    ],
    "go": [
        r'^import\s+"([\w./\-]+)"\s*$',
        r'^\t"([\w./\-]+)"',
        r'^import\s+\w+\s+"([\w./\-]+)"',
    ],
    "java": [
        r"^import\s+([\w.]+)",
    ],
    "rust": [
        r"use\s+([\w:]+)",
    ],
    "csharp": [
        r"^using\s+([\w.]+)",
    ],
    "cpp": [
        r'#include\s*[<"]([^>"]+)[>"]',
    ],
    "ruby": [
        r"^(?:require|require_relative)\s+['\"]([^'\"]+)['\"]",
    ],
    "php": [
        r"use\s+([\w\\]+)",
        r"^(?:require|include)(?:_once)?\s+['\"]([^'\"]+)['\"]",
    ],
    "swift": [
        r"^import\s+(\w+)",
    ],
    "kotlin": [
        r"^import\s+([\w.]+)",
    ],
}

# 文件扩展名到语言类型的映射
_EXT_TO_LANG: Dict[str, str] = {
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

    def __init__(self, api_client: AIApiClient, model: str):
        self.api_client = api_client
        self.model = model

    # ==================== 公开接口 ====================

    async def generate_dependency_graph(
        self,
        analysis: PRAnalysis,
        pr_info: Dict[str, Any],
        pr: Any,
    ) -> str | None:
        """生成 PR 依赖图并注入到 PR Body

        Returns:
            Mermaid 图文本，失败或无依赖时返回 None
        """
        settings = get_settings()

        # 大型 PR 裁剪
        analysis_files = self._trim_files(analysis, settings)

        # 获取文件内容
        file_contents = await asyncio.to_thread(
            self._fetch_file_contents_sync, analysis_files, pr
        )
        if not file_contents:
            logger.info("无法获取任何变更文件内容，跳过依赖图生成")
            return None

        # 提取 import 并构建上下文
        import_context = self._build_import_context(analysis_files, file_contents)
        if not import_context.strip():
            logger.info("变更文件间无 import 依赖关系，跳过依赖图生成")
            return None

        # 提取上一次的依赖图（增量更新时用于保持上下文连贯）
        previous_graph = self._extract_previous_graph(pr_info.get("body", ""))

        # AI 生成 Mermaid
        system_prompt, user_message = self._build_prompts(
            import_context, pr_info, settings, previous_graph
        )

        response = await self.api_client.call_with_retry(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0.2,
            timeout=60.0,
            max_tokens=2000,
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

        # 注入 PR Body（从 GitHub 读取最新 body，避免竞态覆盖 PR Summary）
        current_body = await asyncio.to_thread(lambda: pr.body or "")
        await self.update_pr_body_with_graph(pr, mermaid_graph, current_body)
        logger.info(f"PR 依赖图已生成，长度: {len(mermaid_graph)} 字符")
        return mermaid_graph

    async def update_pr_body_with_graph(
        self,
        pr: Any,
        mermaid_graph: str,
        original_body: str,
    ) -> None:
        """将依赖图注入到 PR Body

        使用独立的 HTML 注释标记，与 PR Summary 共存。
        """
        original = self._extract_original_body(original_body)
        graph_block = self._build_graph_block(mermaid_graph)

        # 保留 PR Summary 块（如果存在）
        summary_block = self._extract_summary_block(original_body)

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
    def _trim_files(
        analysis: PRAnalysis, settings: Any
    ) -> List[PRFileInfo]:
        """大型 PR 裁剪：按变更量排序取 top N 文件"""
        files = [f for f in analysis.code_files if f.status != "deleted"]
        max_files = settings.pr_dependency_graph_max_files
        if len(files) > max_files:
            files = sorted(files, key=lambda f: f.changes, reverse=True)[
                :max_files
            ]
            logger.info(
                f"PR 变更文件数超过限制，只分析 top {max_files} 个文件"
            )
        return files

    @staticmethod
    def _fetch_file_contents_sync(
        files: List[PRFileInfo], pr: Any
    ) -> Dict[str, str]:
        """同步获取变更文件的代码内容"""
        import base64

        repo = pr.base.repo
        ref = pr.head.sha
        file_contents: Dict[str, str] = {}

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

    def _extract_imports(self, file_path: str, content: str) -> List[str]:
        """从代码内容中提取 import 语句（只扫描文件顶部）"""
        lang = self._get_language(file_path)
        if not lang:
            return []

        # 只扫描文件顶部
        top_content = "\n".join(content.split("\n")[: self._IMPORT_SCAN_LINES])

        patterns = _IMPORT_PATTERNS.get(lang, [])
        imports: List[str] = []
        for pattern in patterns:
            for match in re.finditer(pattern, top_content, re.MULTILINE):
                imp = match.group(1).strip()
                if imp and imp not in imports:
                    imports.append(imp)
        return imports

    def _build_import_context(
        self,
        code_files: List[PRFileInfo],
        file_contents: Dict[str, str],
    ) -> str:
        """构建 AI 分析的上下文文本"""
        lines: List[str] = []

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

    @staticmethod
    def _build_prompts(
        import_context: str,
        pr_info: Dict[str, Any],
        settings: Any,
        previous_graph: str | None = None,
    ) -> tuple[str, str]:
        """构建系统提示词和用户消息"""
        config = get_strategy_config()
        depgraph_cfg = config.config.get("pr_dependency_graph", {})

        system_prompt = depgraph_cfg.get(
            "system_prompt",
            "你是代码依赖分析专家。根据提供的 PR 变更文件及其 import 信息，"
            "生成 Mermaid graph TD 语法的依赖关系图。只输出纯 Mermaid 代码块。",
        ).replace("{max_nodes}", str(settings.pr_dependency_graph_max_nodes))

        user_template = depgraph_cfg.get(
            "user_template",
            "请根据以下 PR 变更信息生成依赖关系图：\n\n"
            "PR 标题: {title}\n变更文件数: {file_count}\n\n"
            "文件依赖关系:\n{import_context}",
        )

        user_message = user_template.format(
            title=pr_info.get("title", ""),
            file_count=len(pr_info.get("code_files", [])),
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
    def _validate_mermaid(mermaid_text: str) -> str:
        """验证并提取 Mermaid 语法"""
        # 从 markdown 代码块中提取
        code_block_match = re.search(
            r"```mermaid\s*\n(.*?)```", mermaid_text, re.DOTALL
        )
        if code_block_match:
            mermaid_text = code_block_match.group(1).strip()

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
            f"## 🔗 Sakura AI Reviewer 依赖图\n\n"
            f"```mermaid\n{mermaid_graph}\n```\n\n"
            f"{self.END_MARKER}"
        )

    def _extract_original_body(self, body: str) -> str:
        """从 PR body 中提取不含任何 AI 注入区域的原始内容"""
        if not body:
            return ""

        # 移除依赖图标记区域
        depgraph_pattern = (
            re.escape(self.START_MARKER)
            + r".*?"
            + re.escape(self.END_MARKER)
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

        pattern = (
            re.escape(self.START_MARKER)
            + r"(.*?)"
            + re.escape(self.END_MARKER)
        )
        match = re.search(pattern, body, flags=re.DOTALL)
        if not match:
            return None

        content = match.group(1).strip()
        if not content:
            return None

        # 提取 Mermaid 代码块
        code_match = re.search(r"```mermaid\s*\n(.*?)```", content, re.DOTALL)
        return code_match.group(1).strip() if code_match else None
