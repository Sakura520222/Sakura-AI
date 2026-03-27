"""提示词构建器

从原 ai_reviewer.py 迁移的提示词构建相关方法：
- _build_user_message (284-353行)
- _build_user_message_with_tools (1810-1835行)
- _build_system_prompt_with_tools (1713-1808行)
- _build_label_recommendation_message (2261-2327行)
- _annotate_patch_with_line_numbers (2592-2646行)
"""

import re
from typing import Any, Dict, List

from backend.core.config import get_strategy_config


class PromptBuilder:
    """提示词构建器

    负责构建各种场景下的提示词：
    - 系统提示词
    - 用户消息（标准模式、工具模式）
    - 标签推荐消息
    """

    def build_user_message(
        self,
        context: Dict[str, Any],
        strategy: str,
        include_tools: bool = False,
    ) -> str:
        """构建用户消息

        Args:
            context: 审查上下文
            strategy: 审查策略名称
            include_tools: 是否包含工具说明

        Returns:
            构建好的用户消息
        """
        # 从 analysis 对象中获取统计数据
        analysis = context.get("analysis")
        if analysis:
            file_count = analysis.code_file_count
            total_changes = analysis.code_changes
        else:
            file_count = len(context.get("files", []))
            total_changes = sum(
                f.get("changes", 0) for f in context.get("files", [])
            )

        # 获取策略名称
        strategy_config_data = get_strategy_config().get_strategy(strategy)
        strategy_name = strategy_config_data.get("name", strategy)

        message_parts = [
            "## PR信息",
            f"- 策略: {strategy_name}",
            f"- 文件数: {file_count}",
            f"- 变更行数: {total_changes}",
            "",
        ]

        # 添加文件信息
        files = context.get("files", [])
        if files:
            message_parts.append("## 代码变更")
            message_parts.append(
                "**注意**：下方的 diff 中已标注行号（基于 patch 的行号），创建行内评论时请使用这些行号！\n"
            )

            for i, file in enumerate(files, 1):
                message_parts.append(f"\n### {i}. {file['path']}")
                message_parts.append(f"- 状态: {file['status']}")
                message_parts.append(
                    f"- 变更: +{file.get('additions', 0)} -{file.get('deletions', 0)}"
                )

                # 添加patch（带行号标注）
                if file.get("patch"):
                    patch = file["patch"]
                    patch_with_line_numbers = self.annotate_patch_with_line_numbers(
                        patch, file["path"], context
                    )
                    message_parts.append(f"\n```diff\n{patch_with_line_numbers}\n```")

        # 添加剩余文件信息
        if context.get("remaining_files"):
            message_parts.append(
                f"\n注意: 还有 {context['remaining_files']} 个文件未显示"
            )

        # 添加文件摘要（针对large策略）
        if context.get("file_summary"):
            message_parts.append("\n## 文件变更摘要")
            for file in context["file_summary"]:
                message_parts.append(
                    f"- {file['path']}: {file['status']} ({file['changes']} 行)"
                )

        # 添加工具说明（如果需要）
        if include_tools:
            message_parts.append(
                """

## 可用工具

你可以使用以下工具来更好地理解代码：
- `read_file`: 读取任意文件的完整内容
- `list_directory`: 列出目录中的文件

请根据需要使用工具查看相关文件。
"""
            )

        return "\n".join(message_parts)

    def build_system_prompt(
        self,
        base_prompt: str,
        context: Dict[str, Any],
        include_tools: bool = False,
    ) -> str:
        """构建系统提示词

        Args:
            base_prompt: 基础提示词
            context: 审查上下文
            include_tools: 是否包含工具说明

        Returns:
            构建好的系统提示词
        """
        if not include_tools:
            return base_prompt

        tools_instruction = """

## 可用工具

你可以使用以下工具来更好地理解代码：

1. **read_file**: 读取指定文件的完整内容
   - 使用场景：需要理解某个函数的完整实现、查看配置文件详情、了解依赖模块
   - 参数：file_path（文件路径）
   - **注意**：对于新增文件，工具会自动从PR的HEAD分支读取；对于已存在的文件，会从base分支读取

2. **list_directory**: 列出目录中的文件和子目录
   - 使用场景：了解模块结构、查找相关文件、探索项目组织
   - 参数：directory（目录路径）
   - **注意**：对于新增目录，工具会自动从PR的HEAD分支读取；对于已存在的目录，会从base分支读取

3. **search_project_docs**: 检索项目的指导文档（编码规范、架构准则、业务逻辑等）
   - 使用场景：需要了解项目特定的规则和知识、确认编码规范、理解业务逻辑要求
   - 参数：query（检索关键词或问题）
   - **注意**：如果未找到相关文档，将基于通用最佳实践进行审查

## 使用建议

- 优先审查PR中变更的文件
- 审查前建议先使用 search_project_docs 检索项目相关的编码规范和架构准则
- 当需要理解依赖关系时，使用 read_file 查看相关文件
- 当需要了解模块结构时，使用 list_directory 查看目录
- 合理使用工具，避免不必要的文件读取

## ⚠️ 工具错误处理

如果工具返回错误，例如：
- "文件在PR的HEAD和base分支中都不存在"：这可能是文件路径错误或文件已被删除
- "该路径在跳过列表中"：系统配置跳过了该路径（如node_modules、.git等）
- "文件过大"：文件超过大小限制，请基于diff进行审查

**重要**：如果工具返回错误，请不要重复尝试读取该文件，而是：
1. 基于PR diff中的patch内容进行审查
2. 在整体评论中说明无法访问该文件
3. 继续审查其他可访问的文件
"""

        # 添加行号安全区信息
        changed_lines_map = context.get("changed_lines_map", {})
        if changed_lines_map:
            tools_instruction += """

## ⚠️ 行内评论重要提示

**必须使用 diff 中的行号，不要使用完整文件的行号！**

创建行内评论时，**只能评论以下行号**（这些是 PR diff 中实际变更的行）：

"""
            for file_path, lines in changed_lines_map.items():
                sorted_lines = sorted(lines)
                lines_preview = sorted_lines[:10]  # 只显示前10个
                lines_str = ", ".join(map(str, lines_preview))
                if len(sorted_lines) > 10:
                    lines_str += f" ... (共{len(sorted_lines)}行)"
                tools_instruction += f"- **{file_path}**: {lines_str}\n"

            tools_instruction += """
**重要**：
- ✅ 使用 diff 中显示的行号（基于 patch 的行号）
- ❌ 不要使用完整文件的行号（通过 read_file 查看到的行号）
- 行内评论的行号必须在上述列表中
- 不要评论未变更的行号
- 如果问题不在上述行号中，请在整体评论中说明
- 格式：`### 🔴 文件路径:diff中的行号`

**示例**：
```
### 🔴 config.py:18
**问题**: 边界情况处理不当
**建议**: 添加空值检查
```
"""

        # 添加项目结构
        project_structure_str = "\n".join(context.get("project_structure", []))
        tools_instruction += f"""

## 项目结构

以下是项目的完整目录结构，可以帮助你了解项目组织：

```
{project_structure_str}
```
"""

        return base_prompt + tools_instruction

    def build_label_recommendation_message(
        self,
        context: Dict[str, Any],
        available_labels: Dict[str, Any],
        pr_info: Dict[str, Any],
    ) -> str:
        """构建标签推荐的用户消息

        Args:
            context: 审查上下文
            available_labels: 可用的标签字典
            pr_info: PR信息（包含标题、描述等）

        Returns:
            构建好的用户消息
        """
        lines = [
            "## Pull Request 信息",
            f"- 标题: {pr_info.get('title', 'N/A')}",
            f"- 作者: {pr_info.get('author', 'N/A')}",
            f"- 分支: {pr_info.get('branch', 'N/A')} → {pr_info.get('base_branch', 'N/A')}",
            "",
        ]

        # 增量审查时，添加新提交的标题和内容
        analysis = context.get("analysis")
        if analysis and getattr(analysis, "is_incremental", False) and getattr(analysis, "new_commits", None):
            lines.append("## 本次新增提交")
            for commit in analysis.new_commits:
                title = commit.get('title', '无标题')
                author = commit.get('author', 'Unknown')
                lines.append(f"- **{commit.get('sha', '')}** {title}（by {author}）")
                body = commit.get("body")
                if body:
                    if len(body) > 200:
                        body = body[:200] + "..."
                    lines.append(f"  > {body}")
            lines.append("")

        # 添加可用标签
        lines.append("## 可用的标签")
        for label_name, label_info in available_labels.items():
            desc = label_info.get("description", "")
            lines.append(f"- **{label_name}**: {desc}")

        # 从 analysis 对象获取统计信息
        if analysis:
            file_count = analysis.code_file_count
            total_changes = analysis.code_changes
        else:
            file_count = len(context.get("files", []))
            total_changes = sum(
                f.get("changes", 0) for f in context.get("files", [])
            )

        # 添加代码变更信息
        files = context.get("files", [])
        if files:
            lines.append("\n## 代码变更")

            for i, file in enumerate(files[:10], 1):  # 限制前10个文件
                lines.append(f"\n### {i}. {file['path']}")
                lines.append(f"- 状态: {file['status']}")
                lines.append(
                    f"- 变更: +{file.get('additions', 0)} -{file.get('deletions', 0)}"
                )

                # 添加简化的patch（只显示前200字符）
                if file.get("patch"):
                    patch = file["patch"]
                    if len(patch) > 200:
                        patch = patch[:200] + "\n... (truncated)"
                    lines.append(f"\n```diff\n{patch}\n```")

            if len(files) > 10:
                lines.append(f"\n*还有 {len(files) - 10} 个文件未显示*")

        # 添加统计信息
        lines.append("\n## 变更统计")
        lines.append(f"- 文件数: {file_count}")
        lines.append(f"- 总变更行数: {total_changes}")

        lines.append("\n请分析以上信息，推荐最合适的标签。")

        return "\n".join(lines)

    def annotate_patch_with_line_numbers(
        self, patch: str, file_path: str, context: Dict[str, Any]
    ) -> str:
        """为 patch 添加行号标注

        在 diff 的每一行前面标注行号（基于 patch 的行号），
        帮助 AI 识别正确的行号来创建行内评论。

        Args:
            patch: 原始 patch 内容
            file_path: 文件路径
            context: 审查上下文

        Returns:
            带行号标注的 patch
        """
        lines = patch.split("\n")
        result = []

        for line in lines:
            # 匹配 hunk header: @@ -old_start,old_count +new_start,new_count @@
            hunk_match = re.match(
                r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@", line
            )

            if hunk_match:
                # 这是 hunk header，提取新旧文件的起始行号
                old_start = int(hunk_match.group(1))
                new_start = int(hunk_match.group(3))
                current_line = new_start

                # 在 hunk header 后面添加清晰的注释说明
                result.append(line)
                result.append(
                    f"# 👆 上方 hunk: PR后文件第{new_start}行开始 | 原文件第{old_start}行开始"
                )
            elif line.startswith("+") and not line.startswith("+++"):
                # 新增行 - 标注行号
                result.append(f"{line}  # 👉 [PR后第{current_line}行] 新增")
                current_line += 1
            elif line.startswith("-") and not line.startswith("---"):
                # 删除行 - 标注原文件行号
                result.append(f"{line}  # 👈 [原文件行] 删除")
                # current_line 不增加
            elif not line.startswith("\\"):
                # 上下文行 - 标注行号
                result.append(f"{line}  # 👉 [PR后第{current_line}行] 上下文")
                current_line += 1
            else:
                # 其他行（如 \ No newline at end of file）
                result.append(line)

        return "\n".join(result)
