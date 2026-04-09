"""批处理逻辑

从原 ai_reviewer.py 迁移的批处理相关方法：
- _split_files_into_batches (527-569行)
- _review_batch (571-640行)
- _ai_reduce_results (642-879行)
- _format_batch_results_for_summary (881-948行)
- _merge_batch_results (950-1060行)
"""

import asyncio
import json
import random
from typing import Any, Dict, List

from loguru import logger

from backend.core.config import get_settings, get_strategy_config
from backend.services.ai_reviewer.constants import (
    BATCH_CONCURRENCY,
    BATCH_JITTER_SECONDS,
    MAX_FILES_PER_BATCH,
    MAX_LINES_PER_BATCH,
    MAX_TOOL_ITERATIONS,
    SUMMARY_MAX_TOKENS,
    SUMMARY_TIMEOUT,
)
from backend.services.ai_reviewer.token_tracker import TokenTracker


class BatchProcessor:
    """批处理器

    负责处理大型 PR 的分批审查，包括：
    - 文件分批
    - 批次并行审查
    - 结果合并和总结
    """

    def __init__(self, api_client, prompt_builder, result_parser):
        """初始化批处理器

        Args:
            api_client: AI API 客户端
            prompt_builder: 提示词构建器
            result_parser: 结果解析器
        """
        self.api_client = api_client
        self.prompt_builder = prompt_builder
        self.result_parser = result_parser

    def split_files_into_batches(
        self,
        files: List[Dict[str, Any]],
        max_files: int = MAX_FILES_PER_BATCH,
        max_lines: int = MAX_LINES_PER_BATCH,
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

    async def review_batch(
        self,
        batch_files: List[Dict[str, Any]],
        batch_idx: int,
        total_batches: int,
        context: Dict[str, Any],
        strategy: str,
        repo: Any,
        pr: Any,
        use_tools: bool = False,
        tool_handler=None,
        tool_manager=None,
    ) -> Dict[str, Any]:
        """审查单个批次

        Args:
            batch_files: 该批次的文件列表
            batch_idx: 批次索引（从0开始）
            total_batches: 总批次数
            context: 审查上下文
            strategy: 审查策略
            repo: GitHub仓库对象
            pr: GitHub PR对象
            use_tools: 是否使用AI工具
            tool_handler: 工具处理器
            tool_manager: 工具管理器

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
                # 使用AI工具
                logger.info("批次审查使用AI工具增强模式")
                result = await self._review_with_tools(
                    batch_context, strategy, repo, pr, tool_handler, tool_manager
                )
            else:
                # 不使用AI工具
                logger.info("批次审查使用标准模式（禁用AI工具，基于patch审查）")
                result = await self._review_standard(batch_context, strategy)

            logger.info(
                f"批次 {batch_idx + 1}/{total_batches} 审查完成: "
                f"{len(result.get('comments', []))} 条评论, "
                f"{len(result.get('inline_comments', []))} 条行内评论"
            )

            return result

        except Exception as e:
            logger.error(f"批次 {batch_idx + 1}/{total_batches} 审查失败: {e}")
            # 返回一个空结果，避免中断整个审查流程
            return self._empty_batch_result(batch_idx + 1, str(e))

    def _empty_batch_result(self, batch_idx: int, error: str) -> Dict[str, Any]:
        """创建空的批次结果

        Args:
            batch_idx: 批次索引
            error: 错误信息

        Returns:
            空结果字典
        """
        return {
            "summary": f"批次 {batch_idx} 审查失败: {error}",
            "comments": [],
            "inline_comments": [],
            "overall_score": None,
            "issues": {"critical": [], "major": [], "minor": [], "suggestions": []},
        }

    async def _review_standard(
        self, context: Dict[str, Any], strategy: str
    ) -> Dict[str, Any]:
        """标准审查模式（不使用工具）

        Args:
            context: 审查上下文
            strategy: 审查策略

        Returns:
            审查结果
        """
        settings = get_settings()
        strategy_config_data = get_strategy_config().get_strategy(strategy)
        system_prompt = strategy_config_data.get("prompt", "")

        # 构建用户消息
        user_message = self.prompt_builder.build_user_message(
            context, strategy, include_tools=False
        )

        # 调用AI API
        response = await self.api_client.call_with_retry(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=settings.openai_temperature,
        )

        # 解析结果
        review_text = response.choices[0].message.content
        result = self.result_parser.parse_review_result(review_text, strategy)

        # 记录 token 消耗
        tracker = TokenTracker()
        tracker.accumulate(response)
        result["token_usage"] = tracker.to_dict()

        return result

    async def _review_with_tools(
        self,
        context: Dict[str, Any],
        strategy: str,
        repo: Any,
        pr: Any,
        tool_handler,
        tool_manager,
    ) -> Dict[str, Any]:
        """带工具的审查模式

        Args:
            context: 审查上下文
            strategy: 审查策略
            repo: GitHub仓库对象
            pr: GitHub PR对象
            tool_handler: 工具处理器
            tool_manager: 工具管理器

        Returns:
            审查结果
        """
        settings = get_settings()
        strategy_config_data = get_strategy_config().get_strategy(strategy)
        system_prompt = self.prompt_builder.build_system_prompt(
            strategy_config_data.get("prompt", ""), context, include_tools=True
        )
        tracker = TokenTracker()

        # 构建用户消息
        user_message = self.prompt_builder.build_user_message(
            context, strategy, include_tools=True
        )

        # 初始化消息列表
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        # 获取启用的工具
        repo_full_name = (
            f"{repo.owner.login}/{repo.name}" if repo and repo.owner else None
        )
        enabled_tools = await tool_manager.get_enabled_tools(repo_full_name)

        # 多轮对话循环
        max_iterations = get_strategy_config().get_context_enhancement_config().get(
            "max_tool_iterations", MAX_TOOL_ITERATIONS
        )
        iteration = 0

        while iteration < max_iterations:
            iteration += 1

            # 调用AI API
            response = await self.api_client.call_with_retry(
                model=settings.openai_model,
                messages=messages,
                tools=enabled_tools,
                tool_choice="auto",
                temperature=settings.openai_temperature,
            )
            tracker.accumulate(response)

            # 检查是否有工具调用
            tool_calls = response.choices[0].message.tool_calls

            if not tool_calls:
                # AI完成了审查，返回结果
                review_text = response.choices[0].message.content
                result = self.result_parser.parse_review_result(review_text, strategy)
                result["token_usage"] = tracker.to_dict()
                return result

            # 处理工具调用
            assistant_message = response.choices[0].message
            assistant_msg_dict = {
                "role": "assistant",
                "content": assistant_message.content,
                "tool_calls": tool_calls,
            }

            # DeepSeek-R1 特有：必须包含 reasoning_content
            strategy_config = get_strategy_config()
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
                    result = await tool_handler.handle_tool_call(tool_call, repo, pr)
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

        # 达到最大迭代次数，引导 AI 基于已有信息交付最终审查结果
        logger.warning(
            f"达到最大工具调用次数 ({max_iterations})，引导 AI 交付最终审查结果"
        )
        messages.append(
            {
                "role": "user",
                "content": (
                    "已达到最大工具调用次数，请基于你当前已掌握的所有信息，"
                    "立即返回最终的代码审查结果。"
                ),
            }
        )
        last_response = await self.api_client.call_with_retry(
            model=settings.openai_model,
            messages=messages,
            temperature=settings.openai_temperature,
        )
        tracker.accumulate(last_response)
        review_text = last_response.choices[0].message.content
        result = self.result_parser.parse_review_result(review_text, strategy)
        result["token_usage"] = tracker.to_dict()
        return result

    async def review_batches_parallel(
        self,
        batches: List[List[Dict[str, Any]]],
        context: Dict[str, Any],
        strategy: str,
        repo: Any,
        pr: Any,
        use_tools: bool,
        tool_handler,
        tool_manager,
    ) -> List[Any]:
        """并行审查所有批次

        Args:
            batches: 批次列表
            context: 审查上下文
            strategy: 审查策略
            repo: GitHub仓库对象
            pr: GitHub PR对象
            use_tools: 是否使用工具
            tool_handler: 工具处理器
            tool_manager: 工具管理器

        Returns:
            批次结果列表
        """
        semaphore = asyncio.Semaphore(BATCH_CONCURRENCY)

        async def review_batch_with_semaphore(batch, idx):
            async with semaphore:
                # 添加微小随机抖动，避免同时触发API
                await asyncio.sleep(random.random() * BATCH_JITTER_SECONDS)
                return await self.review_batch(
                    batch,
                    idx,
                    len(batches),
                    context,
                    strategy,
                    repo,
                    pr,
                    use_tools,
                    tool_handler,
                    tool_manager,
                )

        # 并行执行所有批次（受信号量限制）
        batch_results = await asyncio.gather(
            *[
                review_batch_with_semaphore(batch, idx)
                for idx, batch in enumerate(batches)
            ],
            return_exceptions=True,
        )

        logger.info(f"✅ 所有批次审查完成：{len(batches)} 个批次结果已收集")
        return batch_results

    async def ai_reduce_results(
        self,
        batch_results: List[Dict[str, Any]],
        strategy: str,
        context: Dict[str, Any],
        pr: Any,
    ) -> Dict[str, Any]:
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
                return self.merge_batch_results(batch_results, strategy)

            # 2. 构建精简的批次摘要
            batch_summaries = self._format_batch_results_for_summary(valid_results)

            # 3. 构建总结prompt
            summary_prompt = self._build_summary_prompt(
                context, pr, strategy, valid_results, batch_summaries
            )

            logger.info("🧠 调用AI进行智能总结...")

            # 4. 调用AI总结
            response = await self.api_client.call_with_retry(
                model=get_settings().openai_model,
                messages=[
                    {
                        "role": "system",
                        "content": "你是资深代码审查专家，擅长分析和汇总代码审查结果。",
                    },
                    {"role": "user", "content": summary_prompt},
                ],
                temperature=0.3,
                timeout=SUMMARY_TIMEOUT,
                max_tokens=SUMMARY_MAX_TOKENS,
            )

            # 记录 Reduce 阶段的 token 消耗
            reduce_tracker = TokenTracker()
            reduce_tracker.accumulate(response)

            # 5. 解析AI总结结果
            summary_text = response.choices[0].message.content.strip()
            logger.info(f"✅ AI总结完成，响应长度: {len(summary_text)} 字符")

            # 6. 解析JSON并构建最终结果
            final_result = self._build_final_result_from_summary(
                summary_text, valid_results, batch_results, context, strategy
            )
            final_result["token_usage"] = reduce_tracker.to_dict()
            return final_result

        except json.JSONDecodeError as e:
            logger.error(f"AI总结JSON解析失败: {e}")
            logger.warning("回退到机械合并模式")
            return self.merge_batch_results(batch_results, strategy)

        except Exception as e:
            logger.error(f"AI智能总结失败: {e}", exc_info=True)
            logger.warning("回退到机械合并模式")
            return self.merge_batch_results(batch_results, strategy)

    def _build_summary_prompt(
        self,
        context: Dict[str, Any],
        pr: Any,
        strategy: str,
        valid_results: List[Dict],
        batch_summaries: str,
    ) -> str:
        """构建总结提示词

        Args:
            context: 审查上下文
            pr: GitHub PR对象
            strategy: 审查策略
            valid_results: 有效结果列表
            batch_summaries: 批次摘要

        Returns:
            总结提示词
        """
        return f"""你是一个资深代码审查专家，需要智能汇总多个批次的审查结果，生成一份连贯的整体报告。

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

## JSON输出格式（必须严格遵守）

⚠️ **关键要求**：
- `overall_score`字段**必须**包含在JSON中
- 评分必须是1-10之间的整数
- 不要仅在summary文本中写评分，必须在JSON字段中返回

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
- **overall_score必须存在且有效**
- top_issues最多5个最严重的问题
"""

    def _build_final_result_from_summary(
        self,
        summary_text: str,
        valid_results: List[Dict],
        batch_results: List[Dict],
        context: Dict[str, Any],
        strategy: str,
    ) -> Dict[str, Any]:
        """从AI总结构建最终结果

        Args:
            summary_text: AI总结文本
            valid_results: 有效结果列表
            batch_results: 所有批次结果
            context: 审查上下文
            strategy: 审查策略

        Returns:
            最终结果
        """
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

        # 获取PR统计数据
        analysis = context.get("analysis")
        if analysis:
            file_count = analysis.code_file_count
            total_changes = analysis.code_changes
        else:
            file_count = len(context.get("files", []))
            total_changes = sum(f.get("changes", 0) for f in context.get("files", []))

        # 机械合并所有批次的详细数据
        mechanical_result = self.merge_batch_results(batch_results, strategy)

        # 收集所有行内评论
        all_inline_comments = []
        for result in valid_results:
            inline_comments = result.get("inline_comments", [])
            all_inline_comments.extend(inline_comments)

        # 收集问题统计
        issue_stats = {
            "critical": 0,
            "major": 0,
            "minor": 0,
            "suggestions": 0,
        }
        for result in valid_results:
            issues = result.get("issues", {})
            for severity in ["critical", "major", "minor", "suggestions"]:
                issue_stats[severity] += len(issues.get(severity, []))

        # 构建统计看板
        stats_table = f"""
| 文件数 | 总行数 | Critical | Major | Minor | Suggestion |
| :---: | :---: | :---: | :---: | :---: | :---: |
| {file_count} | {total_changes} | {issue_stats["critical"]} | {issue_stats["major"]} | {issue_stats["minor"]} | {issue_stats["suggestions"]} |
"""

        # 构建最终的混合报告
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

        # 使用ScoreExtractor提取评分
        from backend.services.score_extractor import score_extractor

        batch_scores = [
            r.get("overall_score")
            for r in valid_results
            if r.get("overall_score") is not None
        ]

        extracted_score = score_extractor.extract_score(
            {
                "overall_score": ai_summary.get("overall_score"),
                "summary": ai_summary.get("summary", ""),
                "issues": mechanical_result.get("issues", {}),
                "batch_scores": batch_scores,
            }
        )

        final_result = {
            "summary": combined_summary,
            "overall_score": extracted_score,
            "comments": mechanical_result.get("comments", []),
            "inline_comments": all_inline_comments,
            "issues": mechanical_result.get("issues", {}),
            "ai_summary": ai_summary,
        }

        logger.info(
            f"🎉 混合报告生成完成: "
            f"评分={final_result['overall_score']}/10, "
            f"整体评论={len(final_result['comments'])}条, "
            f"行内评论={len(all_inline_comments)}条"
        )

        return final_result

    def _format_batch_results_for_summary(
        self, batch_results: List[Dict[str, Any]]
    ) -> str:
        """格式化批次结果用于AI总结

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
            summary_parts.append(f"- 整体评论: {len(result.get('comments', []))} 条")
            summary_parts.append(
                f"- 行内评论: {len(result.get('inline_comments', []))} 条"
            )

            # 提取关键问题
            if critical_count > 0:
                critical_issues = issues.get("critical", [])[:3]
                summary_parts.append("\n**严重问题**:")
                for issue in critical_issues:
                    issue_str = issue[:150] + "..." if len(issue) > 150 else issue
                    summary_parts.append(f"  - {issue_str}")

            if major_count > 0:
                major_issues = issues.get("major", [])[:3]
                summary_parts.append("\n**重要问题**:")
                for issue in major_issues[:2]:
                    issue_str = issue[:150] + "..." if len(issue) > 150 else issue
                    summary_parts.append(f"  - {issue_str}")

            # 批次摘要
            batch_summary = result.get("summary", "")
            if batch_summary:
                batch_summary_clean = batch_summary.replace("```", "").replace("#", "")
                if len(batch_summary_clean) > 200:
                    batch_summary_clean = batch_summary_clean[:200] + "..."
                summary_parts.append(f"\n**摘要**: {batch_summary_clean}")

        return "\n".join(summary_parts)

    def merge_batch_results(
        self, batch_results: List[Dict[str, Any]], strategy: str
    ) -> Dict[str, Any]:
        """合并多个批次的审查结果（机械合并）

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

        # 合并问题统计（过滤空字符串、无效内容和去重）
        for severity in ["critical", "major", "minor", "suggestions"]:
            seen_issues = set()
            for result in batch_results:
                if isinstance(result, Exception):
                    continue
                issues = result.get("issues", {}).get(severity, [])
                for issue in issues:
                    if (
                        not issue
                        or not isinstance(issue, str)
                        or len(issue.strip()) < 3
                    ):
                        continue
                    issue_normalized = issue.strip().lower()
                    if issue_normalized not in seen_issues:
                        seen_issues.add(issue_normalized)
                        merged_result["issues"][severity].append(issue)

        # 计算平均评分
        from backend.services.score_extractor import score_extractor

        scores = []
        for idx, result in enumerate(batch_results):
            if isinstance(result, Exception):
                continue

            score = result.get("overall_score")
            if score is None:
                summary = result.get("summary", "")
                if summary:
                    score = score_extractor.extract_from_text(summary)
                    if score is not None:
                        logger.info(
                            f"✅ 从批次 {idx + 1} 的summary提取评分: {score}/10"
                        )

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
