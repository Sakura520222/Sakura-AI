"""AI审查引擎"""

from typing import Dict, List, Any
from openai import AsyncOpenAI
from loguru import logger
import json
import asyncio
import random

from backend.core.config import get_settings, get_strategy_config
from backend.core.model_context import get_model_context_manager

settings = get_settings()
strategy_config = get_strategy_config()
model_context_mgr = get_model_context_manager()


class AIReviewer:
    """AI审查器"""

    def __init__(self):
        self.client = AsyncOpenAI(
            base_url=settings.openai_api_base, api_key=settings.openai_api_key
        )

        # 上下文压缩配置
        self.enable_compression = settings.enable_context_compression
        self.compression_threshold = settings.context_compression_threshold
        self.keep_rounds = settings.context_compression_keep_rounds

        # 定义可用工具
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "读取指定文件的完整内容，用于理解代码实现细节",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "要读取的文件路径（相对于项目根目录）",
                            }
                        },
                        "required": ["file_path"],
                    },
                },
            },
            {
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
            },
        ]

    async def _call_ai_with_retry(self, **kwargs) -> Any:
        """带重试机制的AI API调用（优化的指数退避策略）

        重试策略：
        - 前3次：快速重试（1s, 2s, 4s）
        - 后续次数：慢速重试（8s, 16s, 32s...）
        - 总超时：30秒

        处理空响应、异常、网络错误等情况

        Returns:
            OpenAI API响应对象

        Raises:
            Exception: 重试失败或超时
        """
        # 1. 动态调优参数
        # 120s 给模型足够的 Prefill 时间，16k 确保报告不会中途截断
        kwargs.setdefault("timeout", 120.0)
        kwargs.setdefault("max_tokens", 16000)

        # 2. 优化后的重试参数
        max_retries = 5  # 减少到5次
        initial_delay = 1.0  # 初始延迟1秒
        # 总超时应远大于单次超时 * 最大重试次数
        # timeout=120s, max_retries=5, 理论需要600s
        # 加上网络延迟和缓冲时间，设置为900s（15分钟）
        total_timeout = 900.0  # 总超时15分钟

        # 记录开始时间
        start_time = asyncio.get_event_loop().time()

        for attempt in range(max_retries):
            # 检查总超时
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > total_timeout:
                logger.error(
                    f"重试总超时（已耗时 {elapsed:.1f}秒 > {total_timeout}秒），放弃重试"
                )
                raise Exception(f"AI调用失败：重试总超时（{total_timeout}秒）")

            try:
                # 调用AI API
                # kwargs 会直接透传给 OpenAI 异步客户端
                response = await self.client.chat.completions.create(**kwargs)

                # 检查空响应
                if not response.choices or not response.choices[0].message.content:
                    if attempt < max_retries - 1:
                        # 混合退避策略：前3次快速，后面慢速
                        if attempt < 3:
                            delay = initial_delay * (2**attempt)  # 1s, 2s, 4s
                        else:
                            delay = 8 * (2 ** (attempt - 3))  # 8s, 16s...

                        # 添加随机抖动（±20%），避免惊群效应
                        jitter = random.uniform(0.8, 1.2)
                        delay = delay * jitter

                        logger.warning(
                            f"AI返回空响应，{delay:.1f}秒后重试 "
                            f"({attempt + 1}/{max_retries}, 已耗时 {elapsed:.1f}s)"
                        )
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logger.error("AI返回空响应，已达最大重试次数")
                        raise Exception("AI返回空响应，已达最大重试次数")

                # 成功返回
                total_time = asyncio.get_event_loop().time() - start_time
                logger.info(
                    f"✅ AI调用成功（耗时 {total_time:.1f}秒，重试 {attempt} 次）"
                )
                return response

            except Exception as e:
                # 记录具体的错误类型（是 Timeout 还是 RateLimit）
                error_type = type(e).__name__
                if attempt < max_retries - 1:
                    # 混合退避策略：前3次快速，后面慢速
                    if attempt < 3:
                        delay = initial_delay * (2**attempt)  # 1s, 2s, 4s
                    else:
                        delay = 8 * (2 ** (attempt - 3))  # 8s, 16s...

                    # 添加随机抖动（±20%）
                    jitter = random.uniform(0.8, 1.2)
                    delay = delay * jitter

                    logger.warning(
                        f"AI调用失败 [{error_type}]: {e}，{delay:.1f}秒后重试 "
                        f"({attempt + 1}/{max_retries}, 已耗时 {elapsed:.1f}s)"
                    )
                    await asyncio.sleep(delay)
                else:
                    total_time = asyncio.get_event_loop().time() - start_time
                    logger.error(
                        f"AI调用失败 [{error_type}]，已达最大重试次数 "
                        f"(总耗时 {total_time:.1f}s): {e}"
                    )
                    raise

    async def review_pr(self, context: Dict[str, any], strategy: str) -> Dict[str, any]:
        """审查PR"""
        try:
            logger.info(f"开始AI审查，策略: {strategy}")

            # 获取策略配置
            strategy_config_data = strategy_config.get_strategy(strategy)
            system_prompt = strategy_config_data.get("prompt", "")

            # 构建用户消息
            user_message = self._build_user_message(context, strategy)

            # 调用AI API（带重试）
            response = await self._call_ai_with_retry(
                model=settings.openai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=settings.openai_temperature,
            )

            # 提取回复
            review_text = response.choices[0].message.content

            # 解析审查结果
            result = self._parse_review_result(review_text, strategy)

            logger.info(f"AI审查完成，策略: {strategy}")
            return result

        except Exception as e:
            logger.error(f"AI审查时出错: {e}", exc_info=True)
            raise

    def _build_user_message(self, context: Dict[str, any], strategy: str) -> str:
        """构建用户消息

        优化说明：
        - 从 context.analysis 中获取统计数据，避免重复
        - 移除重复的 patch 截断逻辑（已在 pr_analyzer 中处理）
        - 简化策略名称获取
        """
        # 从 analysis 对象中获取统计数据
        analysis = context.get("analysis")
        if analysis:
            file_count = analysis.code_file_count
            total_changes = analysis.code_changes
        else:
            file_count = len(context.get("files", []))
            total_changes = sum(f.get("changes", 0) for f in context.get("files", []))

        # 获取策略名称
        strategy_info = strategy_config.get_strategy(strategy)
        strategy_name = strategy_info.get("name", strategy)

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
                    # Patch 已在 pr_analyzer 中统一截断，这里不再重复处理

                    # 为 diff 添加行号标注
                    patch_with_line_numbers = self._annotate_patch_with_line_numbers(
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

        return "\n".join(message_parts)

    def _parse_review_result(self, review_text: str, strategy: str) -> Dict[str, any]:
        """解析审查结果"""
        result = {
            "summary": review_text,
            "comments": [],
            "inline_comments": [],  # 新增：行内评论
            "overall_score": None,
            "issues": {"critical": [], "major": [], "minor": [], "suggestions": []},
        }

        try:
            # 对所有策略尝试提取评分（如果有）
            import re

            score_match = re.search(r"评分[：:]\s*(\d+)", review_text)
            if score_match:
                result["overall_score"] = int(score_match.group(1))
                logger.info(
                    f"✅ 成功提取评分: {result['overall_score']}/10 (策略: {strategy})"
                )
            else:
                logger.debug(f"⚠️ 未在审查结果中找到评分 (策略: {strategy})")

            # 先提取行内评论（### 文件路径:行号 格式）
            self._extract_inline_comments(result, review_text)

            # 提取结构化评论
            lines = review_text.split("\n")
            current_section = None
            current_content = []

            for line in lines:
                # 检查是否为标题
                if line.strip().startswith("##") or line.strip().startswith("#"):
                    if current_section and current_content:
                        self._add_comment_from_section(
                            result, current_section, current_content
                        )
                    current_section = line.strip()
                    current_content = []
                else:
                    current_content.append(line)

            # 处理最后一个部分
            if current_section and current_content:
                self._add_comment_from_section(result, current_section, current_content)

            # 如果没有提取到结构化评论，将整个文本作为摘要
            if not result["comments"]:
                result["summary"] = review_text

        except Exception as e:
            logger.warning(f"解析审查结果时出错: {e}")
            result["summary"] = review_text

        return result

    def _add_comment_from_section(
        self, result: Dict[str, any], section: str, content: List[str]
    ):
        """从章节中添加评论"""
        content_text = "\n".join(content).strip()
        if not content_text:
            return

        # 跳过行内评论格式的章节（如：### 🔴 config.py:28）
        # 这些章节由 _extract_inline_comments 单独处理，不应重复计数
        import re

        inline_comment_pattern = r"###\s*[🔴🟡💡⚠️]\s+[^\s:]+:[\d\-\s,]+"
        if re.search(inline_comment_pattern, section):
            return

        # 根据章节标题确定严重程度
        section_lower = section.lower()

        severity = "suggestion"
        if "严重" in section or "critical" in section_lower or "🔴" in section:
            severity = "critical"
        elif "重要" in section or "major" in section_lower or "🟡" in section:
            severity = "major"
        elif "优化" in section or "suggestion" in section_lower or "💡" in section:
            severity = "suggestion"
        elif "做得好" in section or "✅" in section:
            # 正面反馈，不作为问题
            return

        # 提取列表项
        import re

        items = re.split(r"^[\-\*]\s*", content_text, flags=re.MULTILINE)

        for item in items:
            item = item.strip()
            if item and len(item) > 10:  # 忽略太短的项
                result["comments"].append(
                    {"content": item, "severity": severity, "type": "overall"}
                )
                # 修复：直接使用 severity，不要加 "s"
                if severity in result["issues"]:
                    result["issues"][severity].append(item)

    async def review_file(
        self, file_path: str, patch: str, strategy: str
    ) -> Dict[str, any]:
        """审查单个文件"""
        try:
            # 获取策略配置
            strategy_config_data = strategy_config.get_strategy(strategy)
            system_prompt = strategy_config_data.get("prompt", "")

            # 构建文件审查消息
            user_message = f"""请审查以下文件的代码变更：

文件: {file_path}

```diff
{patch}
```

请指出潜在的问题和改进建议。"""

            # 调用AI API（带重试）
            response = await self._call_ai_with_retry(
                model=settings.openai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=settings.openai_temperature,
            )

            review_text = response.choices[0].message.content

            return {"file_path": file_path, "review": review_text}

        except Exception as e:
            logger.error(f"审查文件 {file_path} 时出错: {e}")
            return {"file_path": file_path, "review": f"审查失败: {str(e)}"}

    def _split_files_into_batches(
        self, files: List[Dict[str, Any]], max_files: int = 5, max_lines: int = 2000
    ) -> List[List[Dict[str, Any]]]:
        """将文件列表分割成多个批次

        Args:
            files: 文件列表
            max_files: 每批最大文件数
            max_lines: 每批最大行数

        Returns:
            批次列表，每个批次是一个文件列表
        """
        batches = []
        current_batch = []
        current_batch_lines = 0

        for file in files:
            file_lines = file.get("changes", 0)

            # 如果当前批次为空，或者添加该文件不会超过限制
            if not current_batch or (
                len(current_batch) < max_files
                and current_batch_lines + file_lines <= max_lines
            ):
                current_batch.append(file)
                current_batch_lines += file_lines
            else:
                # 当前批次已满，保存并开始新批次
                batches.append(current_batch)
                current_batch = [file]
                current_batch_lines = file_lines

        # 添加最后一个批次
        if current_batch:
            batches.append(current_batch)

        logger.info(
            f"文件分批完成: {len(files)} 个文件 → {len(batches)} 个批次 "
            f"(每批最多 {max_files} 文件 / {max_lines} 行)"
        )

        return batches

    async def _review_batch(
        self,
        batch_files: List[Dict[str, Any]],
        batch_idx: int,
        total_batches: int,
        context: Dict[str, any],
        strategy: str,
        repo: Any,
        pr: Any,
        use_tools: bool = False,
    ) -> Dict[str, any]:
        """审查单个批次

        Args:
            batch_files: 该批次的文件列表
            batch_idx: 批次索引（从0开始）
            total_batches: 总批次数
            context: 审查上下文
            strategy: 审查策略
            repo: GitHub仓库对象
            pr: GitHub PR对象
            use_tools: 是否使用AI工具（分批模式默认False）

        Returns:
            该批次的审查结果
        """
        try:
            logger.info(
                f"开始审查批次 {batch_idx + 1}/{total_batches} "
                f"({len(batch_files)} 个文件, 工具: {use_tools})"
            )

            # 构建批次上下文（只包含该批次的文件）
            batch_context = context.copy()
            batch_context["files"] = batch_files
            batch_context["batch_info"] = {
                "current": batch_idx + 1,
                "total": total_batches,
            }

            # 根据use_tools参数选择审查方法
            if use_tools:
                # 使用AI工具（仅用于小PR）
                logger.info("批次审查使用AI工具增强模式")
                result = await self.review_pr_with_tools(
                    batch_context, strategy, repo, pr
                )
            else:
                # 不使用AI工具（分批审查的标准模式）
                logger.info("批次审查使用标准模式（禁用AI工具，基于patch审查）")
                result = await self.review_pr(batch_context, strategy)

            logger.info(
                f"批次 {batch_idx + 1}/{total_batches} 审查完成: "
                f"{len(result.get('comments', []))} 条评论, "
                f"{len(result.get('inline_comments', []))} 条行内评论"
            )

            return result

        except Exception as e:
            logger.error(f"批次 {batch_idx + 1}/{total_batches} 审查失败: {e}")
            # 返回一个空结果，避免中断整个审查流程
            return {
                "summary": f"批次 {batch_idx + 1} 审查失败: {str(e)}",
                "comments": [],
                "inline_comments": [],
                "overall_score": None,
                "issues": {"critical": [], "major": [], "minor": [], "suggestions": []},
            }

    async def _ai_reduce_results(
        self,
        batch_results: List[Dict[str, any]],
        strategy: str,
        context: Dict[str, any],
        pr: Any,
    ) -> Dict[str, any]:
        """AI智能总结多个批次的审查结果（MapReduce的Reduce阶段）

        Args:
            batch_results: 所有批次的审查结果列表
            strategy: 审查策略
            context: 审查上下文
            pr: GitHub PR对象

        Returns:
            AI总结后的审查结果
        """
        try:
            # 1. 过滤异常结果，提取有效结果
            valid_results = []
            for idx, result in enumerate(batch_results):
                if isinstance(result, Exception):
                    logger.warning(f"批次 {idx + 1} 失败: {result}")
                    continue
                valid_results.append(result)

            if not valid_results:
                logger.error("所有批次都失败了，回退到机械合并")
                return self._merge_batch_results(batch_results, strategy)

            # 2. 构建精简的批次摘要（不包含完整patch）
            batch_summaries = self._format_batch_results_for_summary(valid_results)

            # 3. 构建总结prompt
            summary_prompt = f"""你是一个资深代码审查专家，需要智能汇总多个批次的审查结果，生成一份连贯的整体报告。

## PR信息
- 仓库: {context.get("repo_full_name", "N/A")}
- PR编号: {pr.number}
- 策略: {strategy}
- 总批次数: {len(valid_results)}

## 各批次审查摘要

{batch_summaries}

## 任务要求

请生成一份**连贯的整体审查报告**，要求：

1. **全局视角**：
   - 识别跨批次的系统性问题（如多个文件中相同的错误模式）
   - 去除重复或相似的问题
   - 按优先级重新排序（critical > major > minor > suggestions）

2. **连贯叙事**：
   - 用流畅的语言总结PR的主要变更
   - 指出核心问题和风险
   - 提供优先修复建议（Top 3-5）

3. **总体评分**：
   - 基于1-10分给出总体评分
   - 评分应考虑所有批次的发现

4. **输出格式**：
   - 使用JSON格式返回
   - 包含字段：summary, overall_score, top_issues（数组）

## JSON输出格式

```json
{{
  "summary": "整体审查总结（200-500字）",
  "overall_score": 7,
  "top_issues": [
    {{
      "severity": "critical",
      "description": "问题描述",
      "files_affected": ["file1.py", "file2.py"]
    }}
  ]
}}
```

**重要**：
- 请仅输出JSON，不要包含任何解释文字或markdown标记
- 确保以 '{{' 开头，以 '}}' 结尾
- summary要简洁专业，突出核心问题
- top_issues最多5个最严重的问题
"""

            logger.info("🧠 调用AI进行智能总结...")

            # 4. 调用AI总结（使用较低温度确保稳定）
            response = await self._call_ai_with_retry(
                model=settings.openai_model,
                messages=[
                    {
                        "role": "system",
                        "content": "你是资深代码审查专家，擅长分析和汇总代码审查结果。",
                    },
                    {"role": "user", "content": summary_prompt},
                ],
                temperature=0.3,  # 低温度，确保输出稳定
                timeout=60.0,  # 总结阶段60秒超时
                max_tokens=4000,  # 限制输出长度
            )

            # 5. 解析AI总结结果
            summary_text = response.choices[0].message.content.strip()
            logger.info(f"✅ AI总结完成，响应长度: {len(summary_text)} 字符")

            # 6. 解析JSON
            try:
                # 清理可能的markdown标记
                if "```json" in summary_text:
                    start = summary_text.find("```json") + 7
                    end = summary_text.find("```", start)
                    summary_text = summary_text[start:end].strip()
                elif "```" in summary_text:
                    start = summary_text.find("```") + 3
                    end = summary_text.find("```", start)
                    summary_text = summary_text[start:end].strip()

                ai_summary = json.loads(summary_text)

                # 7. 获取PR统计数据
                analysis = context.get("analysis")
                if analysis:
                    file_count = analysis.code_file_count
                    total_changes = analysis.code_changes
                else:
                    file_count = len(context.get("files", []))
                    total_changes = sum(
                        f.get("changes", 0) for f in context.get("files", [])
                    )

                # 8. 机械合并所有批次的详细数据（获取具体问题列表）
                mechanical_result = self._merge_batch_results(batch_results, strategy)

                # 9. 收集所有行内评论
                all_inline_comments = []
                for result in valid_results:
                    inline_comments = result.get("inline_comments", [])
                    all_inline_comments.extend(inline_comments)

                # 10. 收集问题统计（用于决策引擎和统计看板）
                issue_stats = {"critical": 0, "major": 0, "minor": 0, "suggestions": 0}
                for result in valid_results:
                    issues = result.get("issues", {})
                    for severity in ["critical", "major", "minor", "suggestions"]:
                        issue_stats[severity] += len(issues.get(severity, []))

                # 11. 构建统计看板
                stats_table = f"""
| 文件数 | 总行数 | Critical | Major | Minor | Suggestion |
| :---: | :---: | :---: | :---: | :---: | :---: |
| {file_count} | {total_changes} | {issue_stats["critical"]} | {issue_stats["major"]} | {issue_stats["minor"]} | {issue_stats["suggestions"]} |
"""

                # 12. 构建最终的混合报告（AI总结 + 统计看板 + 详细问题列表）
                combined_summary = f"""## Sakura总结

{ai_summary.get("summary", "")}

**总体评分**: {ai_summary.get("overall_score")}/10

---

## 📊 变更统计

{stats_table}

---

## 📋 详细审查结果

{mechanical_result["summary"]}
"""

                # 13. 构建最终结果
                final_result = {
                    "summary": combined_summary,  # 混合报告
                    "overall_score": ai_summary.get("overall_score"),
                    "comments": mechanical_result.get(
                        "comments", []
                    ),  # 保留具体问题列表
                    "inline_comments": all_inline_comments,
                    "issues": mechanical_result.get("issues", {}),  # 完整的问题统计
                    "ai_summary": ai_summary,  # 保存AI的原始总结
                }

                logger.info(
                    f"🎉 混合报告生成完成: "
                    f"评分={final_result['overall_score']}/10, "
                    f"整体评论={len(final_result['comments'])}条, "
                    f"行内评论={len(all_inline_comments)}条"
                )

                return final_result

            except json.JSONDecodeError as e:
                logger.error(f"AI总结JSON解析失败: {e}, 响应: {summary_text[:500]}")
                logger.warning("回退到机械合并模式")
                return self._merge_batch_results(batch_results, strategy)

        except Exception as e:
            logger.error(f"AI智能总结失败: {e}", exc_info=True)
            logger.warning("回退到机械合并模式")
            return self._merge_batch_results(batch_results, strategy)

    def _format_batch_results_for_summary(
        self, batch_results: List[Dict[str, any]]
    ) -> str:
        """格式化批次结果用于AI总结

        只发送关键信息，不包含完整patch

        Args:
            batch_results: 批次结果列表

        Returns:
            格式化后的摘要文本
        """
        summary_parts = []

        for idx, result in enumerate(batch_results, 1):
            summary_parts.append(f"\n### 批次 {idx}")

            # 评分
            score = result.get("overall_score")
            if score:
                summary_parts.append(f"- 评分: {score}/10")

            # 评论统计
            comments = result.get("comments", [])
            inline_comments = result.get("inline_comments", [])

            # 按严重程度统计
            issues = result.get("issues", {})
            critical_count = len(issues.get("critical", []))
            major_count = len(issues.get("major", []))
            minor_count = len(issues.get("minor", []))
            suggestion_count = len(issues.get("suggestions", []))

            summary_parts.append(
                f"- 问题统计: {critical_count} critical, "
                f"{major_count} major, {minor_count} minor, "
                f"{suggestion_count} suggestions"
            )
            summary_parts.append(f"- 整体评论: {len(comments)} 条")
            summary_parts.append(f"- 行内评论: {len(inline_comments)} 条")

            # 提取关键问题（每个严重程度最多3个）
            if critical_count > 0:
                critical_issues = issues.get("critical", [])[:3]
                summary_parts.append("\n**严重问题**:")
                for issue in critical_issues:
                    # 截断过长的描述
                    issue_str = issue[:150] + "..." if len(issue) > 150 else issue
                    summary_parts.append(f"  - {issue_str}")

            if major_count > 0:
                major_issues = issues.get("major", [])[:3]
                summary_parts.append("\n**重要问题**:")
                for issue in major_issues[:2]:  # 最多2个
                    issue_str = issue[:150] + "..." if len(issue) > 150 else issue
                    summary_parts.append(f"  - {issue_str}")

            # 批次摘要（前200字）
            batch_summary = result.get("summary", "")
            if batch_summary:
                # 移除markdown标记
                batch_summary_clean = batch_summary.replace("```", "").replace("#", "")
                if len(batch_summary_clean) > 200:
                    batch_summary_clean = batch_summary_clean[:200] + "..."
                summary_parts.append(f"\n**摘要**: {batch_summary_clean}")

        return "\n".join(summary_parts)

    def _merge_batch_results(
        self, batch_results: List[Dict[str, any]], strategy: str
    ) -> Dict[str, any]:
        """合并多个批次的审查结果

        Args:
            batch_results: 所有批次的审查结果列表
            strategy: 审查策略

        Returns:
            合并后的审查结果
        """
        merged_result = {
            "summary": "",
            "comments": [],
            "inline_comments": [],
            "overall_score": None,
            "issues": {"critical": [], "major": [], "minor": [], "suggestions": []},
        }

        # 收集所有摘要
        summaries = []
        for idx, result in enumerate(batch_results):
            if isinstance(result, Exception):
                summaries.append(f"### 批次 {idx + 1}\n审查失败: {str(result)}")
                continue

            summary = result.get("summary", "")
            if summary:
                summaries.append(f"### 批次 {idx + 1}\n{summary}")

        # 合并摘要
        merged_result["summary"] = "\n\n".join(summaries)

        # 合并整体评论
        all_comments = []
        for result in batch_results:
            if isinstance(result, Exception):
                continue
            comments = result.get("comments", [])
            all_comments.extend(comments)

        merged_result["comments"] = all_comments

        # 合并行内评论
        all_inline_comments = []
        for result in batch_results:
            if isinstance(result, Exception):
                continue
            inline_comments = result.get("inline_comments", [])
            all_inline_comments.extend(inline_comments)

        merged_result["inline_comments"] = all_inline_comments

        # 合并问题统计
        for severity in ["critical", "major", "minor", "suggestions"]:
            for result in batch_results:
                if isinstance(result, Exception):
                    continue
                issues = result.get("issues", {}).get(severity, [])
                merged_result["issues"][severity].extend(issues)

        # 计算平均评分
        scores = []
        for result in batch_results:
            if isinstance(result, Exception):
                continue
            score = result.get("overall_score")
            if score is not None:
                scores.append(score)

        if scores:
            merged_result["overall_score"] = int(sum(scores) / len(scores))
            logger.info(
                f"所有批次审查完成，平均评分: {merged_result['overall_score']}/10 "
                f"({len(scores)} 个批次有评分)"
            )

        logger.info(
            f"合并批次结果: {len(all_comments)} 条整体评论, "
            f"{len(all_inline_comments)} 条行内评论"
        )

        return merged_result

    async def review_pr_with_tools_batched(
        self,
        context: Dict[str, any],
        strategy: str,
        repo: Any,
        pr: Any,
        max_files_per_batch: int = 5,
        max_lines_per_batch: int = 2000,
    ) -> Dict[str, any]:
        """使用函数工具审查PR，支持分批处理大型PR

        对于大型PR（文件数或行数超过阈值），自动启用分批模式：
        - 将文件分成多个批次
        - 并行审查各批次（禁用AI工具，避免上下文爆炸）
        - 汇总所有批次的审查结果

        Args:
            context: 审查上下文
            strategy: 审查策略
            repo: GitHub仓库对象
            pr: GitHub PR对象
            max_files_per_batch: 每批最大文件数
            max_lines_per_batch: 每批最大行数

        Returns:
            审查结果字典
        """
        try:
            files = context.get("files", [])

            # 判断是否需要分批
            if len(files) <= max_files_per_batch:
                # 小PR，直接审查（使用AI工具）
                logger.info(
                    f"PR规模较小 ({len(files)} 个文件)，使用标准审查模式（启用AI工具）"
                )
                return await self.review_pr_with_tools(context, strategy, repo, pr)

            # 大PR，启用分批模式
            # 根据配置决定是否启用AI工具（依赖上下文压缩）
            enable_tools_in_batch = strategy_config.get_context_enhancement_config().get(
                "enable_ai_tools_in_batch", True
            )
            
            if enable_tools_in_batch:
                logger.info(
                    f"🚨 PR规模较大 ({len(files)} 个文件)，启用分批审查模式 "
                    f"（启用AI工具，依赖自动上下文压缩）"
                )
            else:
                logger.warning(
                    f"🚨 PR规模较大 ({len(files)} 个文件)，启用分批审查模式 "
                    f"（禁用AI工具，仅基于patch审查）"
                )

            # 将文件分批
            batches = self._split_files_into_batches(
                files, max_files_per_batch, max_lines_per_batch
            )

            logger.info(
                f"🚀 启动MapReduce模式：{len(batches)} 个批次并行审查（并发限制: 2）"
            )

            # Map阶段：使用Semaphore(2)控制并发，平衡性能和稳定性
            import random

            semaphore = asyncio.Semaphore(2)

            async def review_batch_with_semaphore(batch, idx):
                async with semaphore:
                    # 添加微小随机抖动（0-0.3秒），避免同时触发API
                    await asyncio.sleep(random.random() * 0.3)
                    return await self._review_batch(
                        batch,
                        idx,
                        len(batches),
                        context,
                        strategy,
                        repo,
                        pr,
                        use_tools=enable_tools_in_batch,  # 根据配置决定是否启用工具
                    )

            # 并行执行所有批次（受信号量限制）
            batch_results = await asyncio.gather(
                *[
                    review_batch_with_semaphore(batch, idx)
                    for idx, batch in enumerate(batches)
                ],
                return_exceptions=True,
            )

            logger.info(f"✅ Map阶段完成：{len(batches)} 个批次审查结果已收集")

            # Reduce阶段：AI智能总结
            if len(batches) > 1:
                logger.info("🧠 启动Reduce阶段：AI智能总结中...")
                merged_result = await self._ai_reduce_results(
                    batch_results, strategy, context, pr
                )
            else:
                # 单批次直接使用结果
                merged_result = (
                    batch_results[0]
                    if not isinstance(batch_results[0], Exception)
                    else {
                        "summary": "审查失败",
                        "comments": [],
                        "inline_comments": [],
                        "overall_score": None,
                    }
                )

            logger.info(
                f"分批审查完成: {len(batches)} 个批次, "
                f"{len(merged_result.get('comments', []))} 条整体评论, "
                f"{len(merged_result.get('inline_comments', []))} 条行内评论"
            )

            return merged_result

        except Exception as e:
            logger.error(f"分批审查失败: {str(e)}", exc_info=True)
            raise

    async def review_pr_with_tools(
        self, context: Dict[str, any], strategy: str, repo: Any, pr: Any
    ) -> Dict[str, any]:
        """使用函数工具审查PR，支持AI主动查看文件

        Args:
            context: 审查上下文
            strategy: 审查策略
            repo: GitHub仓库对象
            pr: GitHub PR对象

        Returns:
            审查结果字典
        """
        try:
            logger.info(f"开始AI审查（带工具支持），策略: {strategy}")

            # 获取策略配置
            strategy_config_data = strategy_config.get_strategy(strategy)
            system_prompt = self._build_system_prompt_with_tools(
                strategy_config_data.get("prompt", ""), context
            )

            # 构建用户消息
            user_message = self._build_user_message_with_tools(context, strategy)

            # 初始化消息列表
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ]

            # 多轮对话循环
            max_iterations = 10  # 防止无限循环
            iteration = 0

            while iteration < max_iterations:
                iteration += 1

                # 调用AI API（带重试）
                response = await self._call_ai_with_retry(
                    model=settings.openai_model,
                    messages=messages,
                    tools=self.tools,
                    tool_choice="auto",
                    temperature=settings.openai_temperature,
                )

                # 检查是否有工具调用
                tool_calls = response.choices[0].message.tool_calls

                if not tool_calls:
                    # AI完成了审查，返回结果
                    review_text = response.choices[0].message.content
                    result = self._parse_review_result(review_text, strategy)
                    logger.info(
                        f"AI审查完成（使用了{iteration}轮对话），策略: {strategy}"
                    )
                    return result

                # 处理工具调用
                assistant_message = response.choices[0].message
                assistant_msg_dict = {
                    "role": "assistant",
                    "content": assistant_message.content,
                    "tool_calls": tool_calls,
                }

                # DeepSeek-R1 特有：必须包含 reasoning_content
                # 仅当模型支持 reasoning_content 时才添加该字段
                if (
                    hasattr(assistant_message, "reasoning_content")
                    and assistant_message.reasoning_content
                    and strategy_config.is_model_supports_reasoning_content(
                        settings.openai_model
                    )
                ):
                    assistant_msg_dict["reasoning_content"] = (
                        assistant_message.reasoning_content
                    )

                messages.append(assistant_msg_dict)

                # 执行每个工具调用
                for tool_call in tool_calls:
                    try:
                        result = await self._handle_tool_call(tool_call, repo, pr)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": json.dumps(result, ensure_ascii=False),
                            }
                        )
                        logger.info(
                            f"执行工具 {tool_call.function.name}: {tool_call.function.arguments}"
                        )
                    except Exception as e:
                        logger.error(f"执行工具 {tool_call.function.name} 失败: {e}")
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": json.dumps({"error": str(e)}),
                            }
                        )

                # 🔑 检查上下文是否超限，触发压缩
                if self.enable_compression:
                    current_tokens = self._estimate_messages_tokens(messages)
                    safe_context = model_context_mgr.calculate_safe_context(
                        settings.openai_model, settings.context_safety_threshold
                    )
                    threshold_tokens = int(safe_context * self.compression_threshold)

                    if current_tokens > threshold_tokens:
                        # 转换为 K tokens 用于显示
                        current_k = current_tokens / 1000
                        threshold_k = threshold_tokens / 1000
                        logger.warning(
                            f"🚨 上下文超限: {current_k:.1f}K tokens > {threshold_k:.1f}K tokens "
                            f"(阈值 {self.compression_threshold * 100}%), 启动压缩..."
                        )

                        # 使用智能压缩，保留工具调用的完整性
                        messages = await self._compress_conversation_history(
                            messages, system_prompt, threshold_tokens
                        )

                        logger.info("✅ 压缩完成，继续审查...")

            # 超过最大迭代次数，强制返回
            logger.warning(f"超过最大迭代次数 {max_iterations}，强制结束")
            last_response = await self._call_ai_with_retry(
                model=settings.openai_model,
                messages=messages,
                temperature=settings.openai_temperature,
            )
            review_text = last_response.choices[0].message.content
            return self._parse_review_result(review_text, strategy)

        except Exception as e:
            logger.error(f"AI审查（带工具）时出错: {str(e)}", exc_info=True)
            raise

    async def _handle_tool_call(
        self, tool_call: Any, repo: Any, pr: Any
    ) -> Dict[str, any]:
        """处理AI的工具调用请求

        Args:
            tool_call: OpenAI工具调用对象
            repo: GitHub仓库对象
            pr: GitHub PR对象

        Returns:
            工具执行结果
        """
        function_name = tool_call.function.name
        arguments = json.loads(tool_call.function.arguments)

        if function_name == "read_file":
            return await self._tool_read_file(arguments["file_path"], repo, pr)
        elif function_name == "list_directory":
            return await self._tool_list_directory(arguments["directory"], repo, pr)
        else:
            return {"error": f"未知工具: {function_name}"}

    async def _tool_read_file(
        self, file_path: str, repo: Any, pr: Any
    ) -> Dict[str, any]:
        """读取文件内容的工具实现

        Args:
            file_path: 文件路径
            repo: GitHub仓库对象
            pr: GitHub PR对象

        Returns:
            文件内容
        """
        try:
            # 检查是否应该跳过该路径
            skip_paths = strategy_config.get_file_filters().get("skip_paths", [])
            for skip_path in skip_paths:
                if file_path.startswith(skip_path.rstrip("/")):
                    logger.info(f"跳过读取文件（在skip_paths中）: {file_path}")
                    return {
                        "file_path": file_path,
                        "error": "该路径在跳过列表中，无法访问",
                    }

            # 智能分支选择：优先尝试PR的HEAD分支（包含最新变更）
            content_file = None
            tried_branches = []

            # 1. 先尝试从PR的HEAD分支读取（包含新增和修改的文件）
            try:
                content_file = repo.get_contents(file_path, pr.head.sha)
                tried_branches.append("HEAD")
                logger.debug(f"✅ 从PR的HEAD分支读取文件成功: {file_path}")
            except Exception as head_error:
                logger.debug(
                    f"⚠️  从PR的HEAD分支读取失败: {file_path}, 错误: {head_error}"
                )

                # 2. 如果HEAD分支失败，尝试从base分支读取（可能被删除的文件）
                try:
                    content_file = repo.get_contents(file_path, pr.base.sha)
                    tried_branches.append("base")
                    logger.debug(f"✅ 从PR的base分支读取文件成功: {file_path}")
                except Exception as base_error:
                    logger.debug(
                        f"⚠️  从PR的base分支读取也失败: {file_path}, 错误: {base_error}"
                    )

                    # 3. 都失败了，返回友好的错误提示
                    return {
                        "file_path": file_path,
                        "error": "文件在PR的HEAD和base分支中都不存在",
                        "hint": "这可能是一个新增的文件，请基于PR diff中的patch进行审查",
                        "tried_branches": tried_branches,
                    }

            if not content_file:
                return {
                    "file_path": file_path,
                    "error": "无法获取文件内容",
                    "tried_branches": tried_branches,
                }

            if content_file.size > 100000:  # 限制100KB
                return {
                    "file_path": file_path,
                    "error": "文件过大",
                    "size": content_file.size,
                    "content": None,
                    "tried_branches": tried_branches,
                    "hint": "请基于PR diff中的patch进行审查，避免读取完整文件",
                }

            # 解码文件内容
            content = content_file.decoded_content.decode("utf-8")

            # 新增：检查行数，超大文件只返回前500行
            lines = content.split("\n")
            if len(lines) > 500:
                truncated_content = "\n".join(lines[:500])
                logger.warning(
                    f"文件 {file_path} 过大 ({len(lines)} 行)，已截断为前 500 行"
                )
                return {
                    "file_path": file_path,
                    "content": truncated_content,
                    "size": content_file.size,
                    "original_lines": len(lines),
                    "truncated_lines": 500,
                    "warning": f"文件过大，仅显示前 500 行（共 {len(lines)} 行）",
                    "branch": tried_branches[0] if tried_branches else "unknown",
                }

            # 解码文件内容
            content = content_file.decoded_content.decode("utf-8")

            return {
                "file_path": file_path,
                "content": content,
                "size": content_file.size,
                "branch": tried_branches[0] if tried_branches else "unknown",
            }

        except Exception as e:
            logger.error(f"读取文件 {file_path} 时发生未预期的错误: {e}", exc_info=True)
            return {
                "file_path": file_path,
                "error": f"读取文件时发生错误: {str(e)}",
                "hint": "请检查文件路径是否正确，或基于PR diff进行审查",
            }

    async def _tool_list_directory(
        self, directory: str, repo: Any, pr: Any
    ) -> Dict[str, any]:
        """列出目录内容的工具实现

        Args:
            directory: 目录路径
            repo: GitHub仓库对象
            pr: GitHub PR对象

        Returns:
            目录内容列表
        """
        try:
            # 检查是否应该跳过该路径
            skip_paths = strategy_config.get_file_filters().get("skip_paths", [])
            for skip_path in skip_paths:
                if directory.startswith(skip_path.rstrip("/")):
                    logger.info(f"跳过列出目录（在skip_paths中）: {directory}")
                    return {
                        "directory": directory,
                        "error": "该路径在跳过列表中，无法访问",
                        "items": [],
                        "count": 0,
                    }

            # 智能分支选择：优先尝试PR的HEAD分支
            contents = None
            tried_branches = []

            # 1. 先尝试从PR的HEAD分支读取（包含新增和修改的目录）
            try:
                contents = repo.get_contents(directory, pr.head.sha)
                tried_branches.append("HEAD")
                logger.debug(f"✅ 从PR的HEAD分支列出目录成功: {directory}")
            except Exception as head_error:
                logger.debug(
                    f"⚠️  从PR的HEAD分支列出目录失败: {directory}, 错误: {head_error}"
                )

                # 2. 如果HEAD分支失败，尝试从base分支读取
                try:
                    contents = repo.get_contents(directory, pr.base.sha)
                    tried_branches.append("base")
                    logger.debug(f"✅ 从PR的base分支列出目录成功: {directory}")
                except Exception as base_error:
                    logger.debug(
                        f"⚠️  从PR的base分支列出目录也失败: {directory}, 错误: {base_error}"
                    )

                    # 3. 都失败了，返回友好的错误提示
                    return {
                        "directory": directory,
                        "error": "目录在PR的HEAD和base分支中都不存在",
                        "hint": "这可能是一个新增的目录，请基于PR diff中的patch进行审查",
                        "items": [],
                        "count": 0,
                        "tried_branches": tried_branches,
                    }

            if isinstance(contents, list):
                items = []
                # 过滤掉skip_paths中的项目
                for item in contents:
                    should_skip = False
                    for skip_path in skip_paths:
                        if item.path.startswith(skip_path.rstrip("/")):
                            should_skip = True
                            break

                    if not should_skip:
                        items.append(
                            {
                                "name": item.name,
                                "path": item.path,
                                "type": item.type,
                                "size": item.size if item.type == "file" else None,
                            }
                        )

                return {
                    "directory": directory,
                    "items": items,
                    "count": len(items),
                    "filtered": len(contents) - len(items)
                    if len(items) < len(contents)
                    else 0,
                    "branch": tried_branches[0] if tried_branches else "unknown",
                }
            else:
                # 单个文件 - 也需要检查skip_paths
                for skip_path in skip_paths:
                    if contents.path.startswith(skip_path.rstrip("/")):
                        return {
                            "directory": directory,
                            "error": "该路径在跳过列表中",
                            "items": [],
                            "count": 0,
                            "tried_branches": tried_branches,
                        }

                # 单个文件
                return {
                    "directory": directory,
                    "items": [
                        {
                            "name": contents.name,
                            "path": contents.path,
                            "type": contents.type,
                            "size": contents.size,
                        }
                    ],
                    "count": 1,
                    "branch": tried_branches[0] if tried_branches else "unknown",
                }

        except Exception as e:
            logger.error(f"列出目录 {directory} 时发生未预期的错误: {e}", exc_info=True)
            return {
                "directory": directory,
                "error": f"列出目录时发生错误: {str(e)}",
                "hint": "请检查目录路径是否正确，或基于PR diff进行审查",
                "items": [],
                "count": 0,
            }

    def _build_system_prompt_with_tools(
        self, base_prompt: str, context: Dict[str, any]
    ) -> str:
        """构建包含工具说明的系统提示词"""
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

## 使用建议

- 优先审查PR中变更的文件
- 当需要理解依赖关系时，使用 read_file 查看相关文件
- 当需要了解模块结构时，使用 list_directory 查看目录
- 合理使用工具，避免不必要的文件读取
- 工具调用会消耗额外的token，请按需使用

## ⚠️ 工具错误处理

如果工具返回错误，例如：
- "文件在PR的HEAD和base分支中都不存在"：这可能是文件路径错误或文件已被删除
- "该路径在跳过列表中"：系统配置跳过了该路径（如node_modules、.git等）
- "文件过大"：文件超过100KB限制，请基于diff进行审查

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

        project_structure_str = "\n".join(context.get("project_structure", []))
        tools_instruction += f"""

## 项目结构

以下是项目的完整目录结构，可以帮助你了解项目组织：

```
{project_structure_str}
```
"""

        return base_prompt + tools_instruction

    def _build_user_message_with_tools(
        self, context: Dict[str, any], strategy: str
    ) -> str:
        """构建包含工具说明的用户消息

        优化说明：
        - 复用 _build_user_message 的逻辑，避免重复
        - 移除重复的 patch 截断逻辑
        - 简化代码结构
        """
        # 先复用基础的消息构建逻辑
        base_message = self._build_user_message(context, strategy)

        # 添加工具特定的说明
        tools_instruction = """

## 可用工具

你可以使用以下工具来更好地理解代码：
- `read_file`: 读取任意文件的完整内容
- `list_directory`: 列出目录中的文件

请根据需要使用工具查看相关文件。
"""

        return base_message + tools_instruction

    async def recommend_labels(
        self,
        context: Dict[str, any],
        available_labels: Dict[str, Dict[str, Any]],
        pr_info: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """推荐PR标签

        Args:
            context: 审查上下文
            available_labels: 可用的标签字典
            pr_info: PR信息（包含标题、描述等）

        Returns:
            推荐标签列表，格式：[{"name": str, "confidence": float, "reason": str}]
        """
        try:
            logger.info("开始AI标签推荐分析")

            # 构建系统提示词
            system_prompt = """你是一个专业的代码审查助手，擅长根据代码变更的内容和性质为Pull Request推荐合适的标签。

## 标签推荐原则

1. **准确性**: 仔细分析代码变更的实际内容，不要仅凭文件名或路径判断
2. **多维度**: 可以同时推荐多个标签，覆盖不同维度
3. **置信度**: 为每个标签给出0-1之间的置信度分数
   - 0.8-1.0: 非常确定，明显符合该标签特征
   - 0.6-0.8: 较为确定，很可能符合
   - 0.4-0.6: 可能符合，需要更多信息确认
   - 0.2-0.4: 有一定可能，但不确定
   - 0.0-0.2: 仅作建议参考
4. **理由说明**: 为每个推荐标签提供简洁的理由

## 标签类型参考

- **bug**: 修复错误、缺陷、边界条件问题
- **enhancement**: 新功能、功能增强、新增API
- **refactor**: 代码重构、结构优化（非功能性变更）
- **performance**: 性能优化、缓存改进、算法优化
- **documentation**: 文档更新、README、注释
- **test**: 测试代码、测试用例、测试修复
- **dependencies**: 依赖更新、包管理
- **ci**: CI/CD配置、工作流、自动化
- **style**: 代码风格、格式化、linting
- **build**: 构建配置、编译脚本

## 输出格式

请以JSON格式返回推荐结果：

```json
{
  "labels": [
    {
      "name": "标签名称",
      "confidence": 0.85,
      "reason": "推荐理由说明"
    }
  ]
}
```

**重要输出要求**：
- 请仅输出 JSON 格式结果，不要包含任何解释文字或 Markdown 标记
- 确保以 '{' 开头，以 '}' 结尾
- 不要添加 ```json 或 ``` 等标记
- 只推荐列表中存在的标签
- 最多推荐3-5个标签
- 置信度必须是0-1之间的数字
- 理由说明要简洁具体
"""

            # 构建用户消息
            user_message = self._build_label_recommendation_message(
                context, available_labels, pr_info
            )

            # 调用AI API（带重试）
            response = await self._call_ai_with_retry(
                model=settings.openai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.3,  # 使用较低的温度以获得更一致的结果
            )

            # 提取响应
            recommendation_text = response.choices[0].message.content

            # 记录完整响应用于调试
            logger.debug(f"AI标签推荐完整响应:\n{recommendation_text}")
            logger.info(f"AI标签推荐响应长度: {len(recommendation_text)} 字符")

            # 解析推荐结果
            recommendations = self._parse_label_recommendation(recommendation_text)

            logger.info(f"AI标签推荐完成，共 {len(recommendations)} 个推荐")
            return recommendations

        except Exception as e:
            logger.error(f"AI标签推荐失败: {e}", exc_info=True)
            return []

    def _estimate_messages_tokens(self, messages: List[Dict[str, any]]) -> int:
        """估算消息列表的 token 数量

        Args:
            messages: 消息列表

        Returns:
            估算的 token 数量
        """
        total_tokens = 0

        for message in messages:
            content = message.get("content", "")
            if content:
                # 使用 model_context_mgr 的估算方法
                total_tokens += model_context_mgr.estimate_tokens(content)

            # 估算 tool_calls 的 token
            tool_calls = message.get("tool_calls")
            if tool_calls:
                for tool_call in tool_calls:
                    # 工具调用名称和参数
                    function = tool_call.function
                    total_tokens += model_context_mgr.estimate_tokens(
                        function.name + str(function.arguments)
                    )

        return total_tokens

    async def _compress_conversation_history(
        self, messages: List[Dict[str, any]], system_prompt: str, max_tokens: int
    ) -> List[Dict[str, any]]:
        """智能压缩对话历史，保留工具调用的完整性

        压缩策略：
        1. 识别并保留最近 N 轮完整的工具调用链路
        2. 压缩更早的对话历史为摘要
        3. 确保消息结构完整，兼容所有模型（包括智谱AI）

        Args:
            messages: 当前的消息列表
            system_prompt: 系统提示词
            max_tokens: 压缩后的最大 token 数

        Returns:
            压缩后的消息列表
        """
        try:
            logger.info(
                f"🗜️  开始压缩对话历史，当前大小: {self._estimate_messages_tokens(messages)} tokens"
            )

            # 1. 分离消息：保留最近几轮工具调用，压缩更早的历史
            keep_rounds = self.keep_rounds  # 默认保留最近2轮工具调用
            compressed_messages = []
            
            # 保留 system 消息
            system_msg = messages[0] if messages and messages[0].get("role") == "system" else None
            if system_msg:
                compressed_messages.append(system_msg)
            
            # 2. 从后向前扫描，保留最近 N 轮完整的工具调用
            tool_call_rounds = []
            current_round = []
            
            for msg in reversed(messages[1:]):  # 跳过 system，倒序扫描
                current_round.insert(0, msg)  # 保持原始顺序
                
                # 如果是 assistant 消息且有 tool_calls，说明一轮结束
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    tool_call_rounds.insert(0, current_round)
                    current_round = []
                    
                    # 如果已收集足够的轮次，停止
                    if len(tool_call_rounds) >= keep_rounds:
                        break
            
            # 处理剩余的未闭合消息
            if current_round:
                if tool_call_rounds:
                    tool_call_rounds[0] = current_round + tool_call_rounds[0]
                else:
                    tool_call_rounds.append(current_round)
            
            # 3. 提取需要压缩的早期历史
            early_history = []
            total_kept = sum(len(round) for round in tool_call_rounds)
            
            if total_kept < len(messages) - (1 if system_msg else 0):
                # 有需要压缩的早期历史
                early_end_idx = len(messages) - total_kept - (1 if system_msg else 0)
                if early_end_idx > 0:
                    early_history = messages[1 if system_msg else 0:early_end_idx + 1]
            
            # 4. 如果有早期历史，进行压缩
            if early_history:
                # 构建待压缩的文本
                conversation_text = ""
                for msg in early_history:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    tool_calls = msg.get("tool_calls")
                    
                    if tool_calls:
                        conversation_text += f"\n## {role.upper()} (工具调用)\n"
                        for tc in tool_calls:
                            func = tc.function
                            conversation_text += f"- 调用工具: {func.name}\n"
                            conversation_text += f"- 参数: {func.arguments}\n"
                            # 查找对应的工具结果
                            tool_result = self._find_tool_result(messages, tc.id)
                            if tool_result:
                                conversation_text += f"- 结果: {str(tool_result)[:200]}...\n"
                    elif content:
                        conversation_text += f"\n## {role.upper()}\n{content}\n"
                
                # 构建压缩 prompt
                compress_prompt = f"""请将以下代码审查对话历史压缩为 {max_tokens} tokens 以内的精简摘要。

## 压缩要求

1. **保留关键信息**：
   - 所有已发现的代码问题（按严重程度：critical/major/minor/suggestions）
   - 所有行内评论的位置（文件路径:行号）和内容
   - 重要工具调用的结果（文件内容、目录结构）
   - 当前审查的进度

2. **移除冗余**：
   - 重复的对话轮次
   - 冗余的工具调用详情
   - 已处理完成的问题

3. **保持结构**：
   - 保持与原始 user_message 相同的格式
   - 确保 PR 信息、文件信息等结构完整
   - 行内评论格式：`### 🔴 文件路径:行号`

## 对话历史

{conversation_text}

请输出压缩后的摘要（保持与原始 PR 审查上下文相同的格式）。
"""

                # 创建独立的压缩会话
                compression_client = AsyncOpenAI(
                    base_url=settings.openai_api_base, api_key=settings.openai_api_key
                )

                # 调用 AI 压缩
                response = await compression_client.chat.completions.create(
                    model=settings.openai_model,
                    messages=[
                        {
                            "role": "system",
                            "content": "你是代码审查助手，擅长精简和总结对话历史。",
                        },
                        {"role": "user", "content": compress_prompt},
                    ],
                    temperature=0.3,
                    timeout=60.0,
                    max_tokens=max_tokens,
                )

                compressed_summary = response.choices[0].message.content.strip()
                
                # 5. 构建最终的消息列表：system + 压缩摘要 + 保留的工具调用轮次
                compressed_messages.append({
                    "role": "user",
                    "content": compressed_summary
                })
                
                # 添加保留的工具调用轮次
                for round_msgs in tool_call_rounds:
                    compressed_messages.extend(round_msgs)
                
                logger.info(
                    f"✅ 压缩完成: "
                    f"{self._estimate_messages_tokens(messages)} → "
                    f"{self._estimate_messages_tokens(compressed_messages)} tokens "
                    f"(保留了 {len(tool_call_rounds)} 轮工具调用)"
                )
            else:
                # 没有早期历史需要压缩，直接返回保留的消息
                for round_msgs in tool_call_rounds:
                    compressed_messages.extend(round_msgs)
                
                logger.info(
                    f"ℹ️  无需压缩，仅保留最近 {len(tool_call_rounds)} 轮工具调用"
                )

            return compressed_messages

        except Exception as e:
            logger.error(f"压缩对话历史失败: {e}", exc_info=True)
            # 压缩失败，返回简化的原始消息
            logger.warning("压缩失败，回退到简化模式")
            return self._fallback_simplify_messages_full(messages, system_prompt)
    
    def _find_tool_result(self, messages: List[Dict[str, any]], tool_call_id: str) -> Any:
        """查找工具调用的结果
        
        Args:
            messages: 消息列表
            tool_call_id: 工具调用ID
            
        Returns:
            工具结果内容
        """
        for msg in messages:
            if msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id:
                try:
                    return json.loads(msg.get("content", "{}"))
                except:
                    return msg.get("content", "")
        return None
    
    def _clean_message_for_model(self, message: Dict[str, any]) -> Dict[str, any]:
        """清理消息中当前模型不支持的字段
        
        Args:
            message: 原始消息
            
        Returns:
            清理后的消息
        """
        # 创建消息副本
        cleaned_msg = message.copy()
        
        # 检查当前模型是否支持 reasoning_content
        supports_reasoning = strategy_config.is_model_supports_reasoning_content(
            settings.openai_model
        )
        
        # 如果模型不支持 reasoning_content，移除该字段
        if not supports_reasoning and "reasoning_content" in cleaned_msg:
            del cleaned_msg["reasoning_content"]
            logger.debug("移除不兼容的 reasoning_content 字段")
        
        return cleaned_msg
    
    def _fallback_simplify_messages_full(
        self, messages: List[Dict[str, any]], system_prompt: str
    ) -> List[Dict[str, any]]:
        """压缩失败时的完整简化后备方案
        
        Args:
            messages: 消息列表
            system_prompt: 系统提示词
            
        Returns:
            简化后的消息列表
        """
        logger.warning("使用完整简化模式，仅保留最近2轮工具调用")
        
        # 保留 system 消息
        result = []
        system_msg = messages[0] if messages and messages[0].get("role") == "system" else None
        if system_msg:
            result.append(self._clean_message_for_model(system_msg))
        
        # 从后向前保留最近2轮工具调用
        keep_rounds = 2
        tool_call_rounds = []
        current_round = []
        
        for msg in reversed(messages[1:]):
            current_round.insert(0, msg)
            
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                tool_call_rounds.insert(0, current_round)
                current_round = []
                
                if len(tool_call_rounds) >= keep_rounds:
                    break
        
        if current_round:
            if tool_call_rounds:
                tool_call_rounds[0] = current_round + tool_call_rounds[0]
            else:
                tool_call_rounds.append(current_round)
        
        # 添加保留的工具调用轮次（清理每个消息）
        for round_msgs in tool_call_rounds:
            for msg in round_msgs:
                result.append(self._clean_message_for_model(msg))
        
        return result

    def _fallback_simplify_messages(
        self, messages: List[Dict[str, any]], system_prompt: str
    ) -> str:
        """压缩失败时的简化后备方案

        Args:
            messages: 消息列表
            system_prompt: 系统提示词

        Returns:
            简化后的用户消息
        """
        # 只保留原始用户消息，移除所有对话历史
        for msg in messages:
            if msg.get("role") == "user" and msg.get("content"):
                return msg["content"]

        # 如果找不到用户消息，返回空字符串
        logger.warning("无法找到原始用户消息")
        return ""

    def _build_label_recommendation_message(
        self,
        context: Dict[str, any],
        available_labels: Dict[str, Dict[str, Any]],
        pr_info: Dict[str, Any],
    ) -> str:
        """构建标签推荐的用户消息

        优化说明：
        - 从 context.analysis 获取统计数据，避免重复计算
        - 移除重复的 patch 截断逻辑
        - 简化代码结构
        """
        lines = [
            "## Pull Request 信息",
            f"- 标题: {pr_info.get('title', 'N/A')}",
            f"- 作者: {pr_info.get('author', 'N/A')}",
            f"- 分支: {pr_info.get('branch', 'N/A')} → {pr_info.get('base_branch', 'N/A')}",
            "",
        ]

        # 添加可用标签
        lines.append("## 可用的标签")
        for label_name, label_info in available_labels.items():
            desc = label_info.get("description", "")
            lines.append(f"- **{label_name}**: {desc}")

        # 从 analysis 对象获取统计信息
        analysis = context.get("analysis")
        if analysis:
            file_count = analysis.code_file_count
            total_changes = analysis.code_changes
        else:
            file_count = len(context.get("files", []))
            total_changes = sum(f.get("changes", 0) for f in context.get("files", []))

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
                    # Patch 已在 pr_analyzer 中截断，这里只需进一步简化用于标签推荐
                    if len(patch) > 200:
                        patch = patch[:200] + "\n... (truncated)"
                    lines.append(f"\n```diff\n{patch}\n```")

            if len(files) > 10:
                lines.append(f"\n*还有 {len(files) - 10} 个文件未显示*")

        # 添加统计信息（使用从 analysis 获取的值）
        lines.append("\n## 变更统计")
        lines.append(f"- 文件数: {file_count}")
        lines.append(f"- 总变更行数: {total_changes}")

        lines.append("\n请分析以上信息，推荐最合适的标签。")

        return "\n".join(lines)

    def _parse_label_recommendation(self, response_text: str) -> List[Dict[str, Any]]:
        """解析标签推荐响应"""
        recommendations = []

        try:
            # 检查响应是否为空
            if not response_text or not response_text.strip():
                logger.warning("AI返回空响应")
                return []

            # 清理响应文本
            text = response_text.strip()

            # 尝试提取JSON代码块
            if "```json" in text:
                start = text.find("```json") + 7
                end = text.find("```", start)
                if end > start:
                    json_str = text[start:end].strip()
                    data = json.loads(json_str)
                else:
                    # 没有结束标记，尝试从 ```json 后面全部解析
                    json_str = text[start:].strip()
                    data = json.loads(json_str)
            elif "```" in text:
                # 尝试提取普通代码块
                start = text.find("```") + 3
                end = text.find("```", start)
                if end > start:
                    json_str = text[start:end].strip()
                    data = json.loads(json_str)
                else:
                    # 没有结束标记
                    json_str = text[start:].strip()
                    data = json.loads(json_str)
            else:
                # 直接解析整个响应
                data = json.loads(text)

            # 提取标签列表
            if isinstance(data, dict) and "labels" in data:
                for item in data["labels"]:
                    recommendations.append(
                        {
                            "name": item.get("name", ""),
                            "confidence": float(item.get("confidence", 0.5)),
                            "reason": item.get("reason", ""),
                        }
                    )
            elif isinstance(data, list):
                # 直接是标签列表
                for item in data:
                    recommendations.append(
                        {
                            "name": item.get("name", ""),
                            "confidence": float(item.get("confidence", 0.5)),
                            "reason": item.get("reason", ""),
                        }
                    )

            logger.info(f"成功解析 {len(recommendations)} 个标签推荐")
            return recommendations

        except json.JSONDecodeError as e:
            logger.error(f"JSON解析失败: {e}")
            # 尝试文本解析作为后备
            return self._parse_text_label_recommendation(response_text)
        except Exception as e:
            logger.error(f"解析标签推荐失败: {e}", exc_info=True)
            return []

    def _extract_inline_comments(self, result: Dict[str, any], review_text: str):
        """从审查文本中提取行内评论

        解析格式：
        ### 🔴 文件路径:行号
        ### 🔴 文件路径:起始行-结束行
        ### 🔴 文件路径:行号1, 行号2-行号3, ...
        **问题**: [问题描述]
        **建议**: [修复建议]
        (可能包含代码块)

        Args:
            result: 审查结果字典（将被修改）
            review_text: AI 返回的审查文本
        """
        import re

        # 匹配模式：### emoji 文件路径:行号（支持范围和多行号）
        # 示例：
        # - ### 🔴 backend/services/user.py:45
        # - ### 🔴 config.py:13-14
        # - ### 🔴 config.py:13-14, 21-23, 31, 34-35
        pattern = r"###\s*[🔴🟡💡⚠️]\s+([^\s:]+):([\d\-\s,]+?)\s*\n(.*?)(?=###\s*[🔴🟡💡⚠️]|##|\Z)"

        matches = re.finditer(pattern, review_text, re.MULTILINE | re.DOTALL)

        for match in matches:
            try:
                file_path = match.group(1).strip()
                line_numbers_str = match.group(2).strip()
                content_block = match.group(3).strip()

                # 解析行号（支持范围和多行号）
                # 示例：'28', '22-24', '13-14, 21-23, 31, 34-35'
                line_numbers = self._parse_line_numbers(line_numbers_str)

                if not line_numbers:
                    logger.warning(f"无法解析行号: {line_numbers_str}")
                    continue

                # 灵活的内容提取逻辑
                # 不依赖硬编码标记，适应各种 AI 输出格式
                # 规则：第一行作为标题，剩余内容作为详细说明

                lines = content_block.split("\n", 1)  # 只分割第一个换行符

                if len(lines) == 2:
                    # 有两行或更多：第一行是标题，剩余是详细内容
                    first_line = lines[0].strip()
                    remaining_content = lines[1].strip()

                    # 清理第一行的标记（如 **问题**:、**Issue**: 等）
                    # 移除常见的 Markdown 标记
                    title = first_line
                    for marker in [
                        "**问题**:",
                        "**问题**",
                        "**Issue**:",
                        "**Issue**",
                        "**Description**:",
                        "**Description**",
                        "**建议**:",
                        "**建议**",
                    ]:
                        if title.startswith(marker):
                            title = title[len(marker) :].strip()
                            break

                    if title:
                        body = (
                            f"**{title}**\n\n{remaining_content}"
                            if remaining_content
                            else f"**{title}**"
                        )
                    else:
                        body = remaining_content if remaining_content else first_line
                else:
                    # 只有一行，直接使用
                    body = lines[0].strip()

                # 确定严重程度
                severity = "suggestion"
                full_match_text = match.group(0)
                if "🔴" in full_match_text or "严重" in full_match_text:
                    severity = "critical"
                elif (
                    "🟡" in full_match_text
                    or "重要" in full_match_text
                    or "改进" in full_match_text
                ):
                    severity = "major"
                elif "💡" in full_match_text or "优化" in full_match_text:
                    severity = "suggestion"

                # 为每个行号创建评论（或只使用第一个）
                # 如果有多个行号，我们使用第一个创建评论
                # GitHub 的行内评论 API 一次只能评论一行
                primary_line = line_numbers[0]

                inline_comment = {
                    "file_path": file_path,
                    "line_number": primary_line,
                    "body": body,
                    "severity": severity,
                }

                result["inline_comments"].append(inline_comment)

                # 同时更新问题统计（用于决策引擎）
                if severity in result["issues"]:
                    # 使用简洁的描述作为问题统计
                    issue_summary = f"{file_path}:{primary_line}"
                    result["issues"][severity].append(issue_summary)

                # 记录日志
                if len(line_numbers) > 1:
                    logger.info(
                        f"提取行内评论: {file_path}:{primary_line} - {severity} (共{len(line_numbers)}行，内容长度: {len(body)} 字符)"
                    )
                else:
                    logger.info(
                        f"提取行内评论: {file_path}:{primary_line} - {severity} (内容长度: {len(body)} 字符)"
                    )

            except Exception as e:
                logger.warning(
                    f"解析行内评论失败: {e}, 匹配内容: {match.group(0)[:200]}"
                )
                continue

        logger.info(f"共提取 {len(result['inline_comments'])} 条行内评论")

    def _parse_line_numbers(self, line_numbers_str: str) -> List[int]:
        """解析行号字符串，返回行号列表

        支持格式：
        - '28' -> [28]
        - '22-24' -> [22, 23, 24]
        - '13-14, 21-23, 31, 34-35' -> [13, 14, 21, 22, 23, 31, 34, 35]

        Args:
            line_numbers_str: 行号字符串

        Returns:
            行号列表
        """
        line_numbers = []

        try:
            # 分割逗号
            parts = line_numbers_str.split(",")

            for part in parts:
                part = part.strip()

                if "-" in part:
                    # 范围：起始-结束
                    start, end = part.split("-")
                    start = int(start.strip())
                    end = int(end.strip())
                    line_numbers.extend(range(start, end + 1))
                else:
                    # 单个行号
                    line_numbers.append(int(part))

        except Exception as e:
            logger.warning(f"解析行号字符串失败: {line_numbers_str}, 错误: {e}")
            return []

        return line_numbers

    def _annotate_patch_with_line_numbers(
        self, patch: str, file_path: str, context: Dict[str, any]
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
        import re

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

    def _parse_text_label_recommendation(self, text: str) -> List[Dict[str, Any]]:
        """从文本中解析标签推荐（后备方案）"""
        recommendations = []
        lines = text.split("\n")

        for line in lines:
            line = line.strip()
            # 查找格式：- 标签名 (置信度) - 理由
            if line.startswith("-") or line.startswith("*"):
                parts = line[1:].strip().split("(", 1)
                if len(parts) > 0:
                    label_name = parts[0].strip()

                    confidence = 0.5
                    reason = ""

                    if len(parts) > 1:
                        rest = parts[1]
                        # 提取置信度
                        if ")" in rest:
                            conf_str = rest.split(")")[0].strip()
                            try:
                                # 处理百分比格式
                                if "%" in conf_str:
                                    confidence = (
                                        float(conf_str.replace("%", "").strip()) / 100
                                    )
                                else:
                                    confidence = float(conf_str)
                            except ValueError:
                                pass

                        # 提取理由
                        if "-" in rest:
                            reason_parts = rest.split("-", 1)
                            if len(reason_parts) > 1:
                                reason = reason_parts[1].strip()

                    if label_name:
                        recommendations.append(
                            {
                                "name": label_name,
                                "confidence": min(max(confidence, 0.0), 1.0),
                                "reason": reason,
                            }
                        )

        return recommendations
