"""结果解析器

从原 ai_reviewer.py 迁移的结果解析相关方法：
- _parse_review_result (355-410行)
- _add_comment_from_section (412-487行)
- _extract_inline_comments (2400-2551行)
- _parse_line_numbers (2553-2590行)
- _parse_label_recommendation (2329-2398行)
- _parse_text_label_recommendation (2648-2693行)
"""

import json
import re
from typing import Any, Dict, List

from loguru import logger

from .constants import (
    EMOJI_TO_SEVERITY,
    INLINE_COMMENT_PATTERN,
    JSON_BLOCK_END_MARKER,
    JSON_BLOCK_START_MARKER,
    JSON_SCHEMA_VERSION,
    SEVERITY_TO_ISSUES_KEY,
    VALID_DECISIONS,
    VALID_SEVERITIES,
)


class ReviewResultParser:
    """审查结果解析器

    负责解析 AI 返回的审查文本，提取：
    - 整体评论（按严重程度分类）
    - 行内评论（带文件路径和行号）
    - 评分信息
    - 标签推荐
    """

    def parse_review_result(self, review_text: str, strategy: str) -> Dict[str, Any]:
        """解析审查结果

        Args:
            review_text: AI 返回的审查文本
            strategy: 审查策略名称

        Returns:
            解析后的结果字典，包含：
            - summary: 摘要
            - comments: 整体评论列表
            - inline_comments: 行内评论列表
            - overall_score: 总体评分
            - issues: 按严重程度分类的问题
            - parse_source: 解析来源 ("json" / "emoji" / "fallback")
            - ai_decision: AI 建议的决策 (仅 JSON 模式)
            - ai_decision_reason: AI 决策理由 (仅 JSON 模式)
        """
        result = {
            "summary": review_text,
            "comments": [],
            "inline_comments": [],
            "overall_score": None,
            "issues": {"critical": [], "major": [], "minor": [], "suggestions": []},
            "parse_source": "fallback",
            "ai_decision": None,
            "ai_decision_reason": None,
        }

        try:
            # 优先尝试结构化 JSON 提取
            json_data = self._extract_structured_json(review_text)
            if json_data and self._apply_json_result(result, json_data):
                result["parse_source"] = "json"

                # 用去掉 JSON 块的完整 Markdown 作为摘要，保留 AI 的详细审查正文
                result["summary"] = self._strip_json_block(review_text)

                # JSON 提取 score/decision，但 Markdown 行内评论仍需提取
                # 传已去除 JSON 块的文本，避免 JSON 内容被当作章节解析
                markdown_text = result["summary"]
                json_inline_count = len(result["inline_comments"])
                self.extract_inline_comments(result, markdown_text)
                self._dedup_inline_comments(result)
                markdown_inline_count = len(result["inline_comments"]) - json_inline_count
                self._parse_structured_comments(result, markdown_text)

                logger.info(
                    f"✅ 结构化 JSON 解析成功 (策略: {strategy}, "
                    f"decision: {result.get('ai_decision')}, "
                    f"JSON 行内: {json_inline_count}, Markdown 行内: {markdown_inline_count}, "
                    f"整体评论: {len(result['comments'])})"
                )
                return result

            # Fallback: emoji 解析
            from backend.services.score_extractor import score_extractor

            extracted_score = score_extractor.extract_from_text(review_text)
            if extracted_score is not None:
                result["overall_score"] = extracted_score
                logger.info(
                    f"✅ 成功提取评分: {result['overall_score']}/10 (策略: {strategy})"
                )
            else:
                logger.debug(f"⚠️ 未在审查结果中找到评分 (策略: {strategy})")

            # 提取行内评论
            self.extract_inline_comments(result, review_text)
            self._dedup_inline_comments(result)

            # 提取结构化评论
            self._parse_structured_comments(result, review_text)

            # 如果没有提取到结构化评论，将整个文本作为摘要
            if not result["comments"]:
                result["summary"] = review_text

            result["parse_source"] = "emoji"

        except Exception as e:
            logger.warning(f"解析审查结果时出错: {e}")
            result["summary"] = review_text
            result["parse_source"] = "error"

        return result

    def _parse_structured_comments(
        self, result: Dict[str, Any], review_text: str
    ) -> None:
        """解析结构化评论（按章节组织）

        Args:
            result: 结果字典（将被修改）
            review_text: 审查文本
        """
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

    def _add_comment_from_section(
        self, result: Dict[str, Any], section: str, content: List[str]
    ) -> None:
        """从章节中添加评论

        Args:
            result: 结果字典（将被修改）
            section: 章节标题
            content: 章节内容列表
        """
        content_text = "\n".join(content).strip()
        if not content_text:
            return

        # 跳过行内评论格式的章节
        inline_comment_pattern = r"###\s*[🔴🟡💡⚠️]\s+[^\s:]+:[\d\-\s,]+"
        if re.search(inline_comment_pattern, section):
            return

        # 跳过正面反馈
        if "做得好" in section or "✅" in section:
            return

        # 确定严重程度
        severity = self._determine_severity(section)
        issues_key = SEVERITY_TO_ISSUES_KEY.get(severity, "suggestions")

        # 提取列表项
        items = re.split(r"^[\-\*]\s*", content_text, flags=re.MULTILINE)

        for item in items:
            item = item.strip()
            if item and len(item) > 10:  # 忽略太短的项
                result["comments"].append(
                    {"content": item, "severity": severity, "type": "overall"}
                )
                if issues_key in result["issues"]:
                    result["issues"][issues_key].append(item)

    def _determine_severity(self, section: str) -> str:
        """根据章节标题确定严重程度

        Args:
            section: 章节标题

        Returns:
            严重程度 (critical/major/minor/suggestion)
        """
        section_lower = section.lower()

        # 首先检查emoji
        for emoji, severity in EMOJI_TO_SEVERITY.items():
            if emoji in section:
                return severity

        # 然后检查关键词
        if "严重" in section or "critical" in section_lower:
            return "critical"
        elif "重要" in section or "major" in section_lower:
            return "major"
        elif "优化" in section or "suggestion" in section_lower:
            return "suggestion"

        return "suggestion"  # 默认

    def extract_inline_comments(self, result: Dict[str, Any], review_text: str) -> None:
        """从审查文本中提取行内评论

        解析格式：
        ### 🔴 文件路径:行号
        ### 🔴 文件路径:起始行-结束行
        ### 🔴 文件路径:行号1, 行号2-行号3, ...
        **问题**: [问题描述]
        **建议**: [修复建议]

        Args:
            result: 审查结果字典（将被修改）
            review_text: AI 返回的审查文本
        """
        pattern = re.compile(INLINE_COMMENT_PATTERN, re.MULTILINE | re.DOTALL)
        matches = pattern.finditer(review_text)

        for match in matches:
            try:
                file_path = match.group(1).strip()
                line_numbers_str = match.group(2).strip()
                content_block = match.group(3).strip()

                # 解析行号
                line_numbers = self.parse_line_numbers(line_numbers_str)
                if not line_numbers:
                    logger.warning(f"无法解析行号: {line_numbers_str}")
                    continue

                # 提取内容
                body = self._extract_inline_body(content_block)

                # 提取修复建议（**建议**: xxx）
                suggestion = self._extract_suggestion_from_body(content_block)

                # 提取修复代码（**修复**: ```code block```）
                fix_code = self._extract_fix_from_body(content_block)

                # 提取置信度（**置信度**: 0.85）
                fix_confidence = self._extract_confidence_from_body(content_block)

                # 识别严重程度
                severity = self._extract_inline_severity(match.group(0))
                issues_key = SEVERITY_TO_ISSUES_KEY.get(severity, "suggestions")

                # 创建行内评论
                start_line = line_numbers[0]
                end_line = line_numbers[-1]

                inline_comment = {
                    "file_path": file_path,
                    "line_number": end_line,
                    "start_line": start_line,
                    "body": body,
                    "severity": severity,
                }

                # 附加建议文本（如果有）
                if suggestion:
                    inline_comment["suggestion"] = suggestion

                # 附加修复代码和置信度（优先使用 Markdown 格式，JSON 作为补充）
                if fix_code:
                    inline_comment["fix_suggestion"] = fix_code
                if fix_confidence is not None:
                    inline_comment["fix_confidence"] = fix_confidence

                result["inline_comments"].append(inline_comment)

                # 更新问题统计
                if issues_key in result["issues"]:
                    if len(line_numbers) > 1:
                        issue_summary = f"{file_path}:{start_line}-{end_line}"
                    else:
                        issue_summary = f"{file_path}:{start_line}"
                    result["issues"][issues_key].append(issue_summary)

                # 记录日志
                if len(line_numbers) > 1:
                    logger.info(
                        f"提取行内评论: {file_path}:{start_line}-{end_line} - {severity}"
                    )
                else:
                    logger.info(f"提取行内评论: {file_path}:{start_line} - {severity}")

            except Exception as e:
                logger.warning(
                    f"解析行内评论失败: {e}, 匹配内容: {match.group(0)[:200]}"
                )
                continue

        logger.info(f"共提取 {len(result['inline_comments'])} 条行内评论")

    def _extract_inline_body(self, content_block: str) -> str:
        """从内容块中提取行内评论主体

        Args:
            content_block: 内容块文本

        Returns:
            处理后的主体文本
        """
        lines = content_block.split("\n", 1)

        if len(lines) == 2:
            first_line = lines[0].strip()
            remaining_content = lines[1].strip()

            # 清理第一行的标记
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
            body = lines[0].strip()

        return body

    def _extract_suggestion_from_body(self, content_block: str) -> str | None:
        """从行内评论内容块中提取建议文本

        解析 **建议**: xxx 格式，提取建议内容。

        Args:
            content_block: 内容块文本

        Returns:
            建议文本，如果没有则返回 None
        """
        # 匹配 **建议**: 后面的内容（到下一个 **xxx**: 标记或内容结尾）
        suggestion_pattern = re.compile(
            r"\*\*建议\*\*\s*[:：]\s*(.+?)(?=\n\*\*|$)",
            re.DOTALL,
        )
        match = suggestion_pattern.search(content_block)
        if match:
            return match.group(1).strip()
        return None

    def _extract_fix_from_body(self, content_block: str) -> str | None:
        """从行内评论内容块中提取修复代码

        解析 **修复**: 后面紧跟的代码块（```lang ... ```），提取修复代码内容。

        Args:
            content_block: 内容块文本

        Returns:
            修复代码文本，如果没有则返回 None
        """
        # 匹配 **修复** 后面紧跟的代码块 ```lang\n...\n```
        fix_pattern = re.compile(
            r"\*\*修复\*\*\s*[:：]?\s*\n```[\w]*\n(.*?)\n```",
            re.DOTALL,
        )
        match = fix_pattern.search(content_block)
        if match:
            return match.group(1).strip()
        return None

    def _extract_confidence_from_body(self, content_block: str) -> float | None:
        """从行内评论内容块中提取置信度

        解析 **置信度**: 0.85 格式，提取置信度数值。

        Args:
            content_block: 内容块文本

        Returns:
            置信度数值 (0.0-1.0)，如果没有或无效则返回 None
        """
        confidence_pattern = re.compile(
            r"\*\*置信度\*\*\s*[:：]\s*([0-9]*\.?[0-9]+)",
        )
        match = confidence_pattern.search(content_block)
        if match:
            try:
                val = float(match.group(1))
                # clamp 到 [0.0, 1.0]
                return max(0.0, min(1.0, val))
            except (ValueError, TypeError):
                return None
        return None

    def _extract_inline_severity(self, match_text: str) -> str:
        """从匹配文本中提取严重程度

        Args:
            match_text: 匹配的完整文本

        Returns:
            严重程度
        """
        for emoji, severity in EMOJI_TO_SEVERITY.items():
            if emoji in match_text:
                return severity
        return "suggestion"

    def parse_line_numbers(self, line_numbers_str: str) -> List[int]:
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

    def _dedup_inline_comments(self, result: dict[str, Any]) -> None:
        """去除重复的行内评论（基于 file_path + line_number）

        当 JSON 和 Markdown 提取到同一位置的评论时，保留先到的那条，
        并用后到的评论中非空字段补充缺失的数据（如 fix_suggestion、fix_confidence）。
        """
        seen: dict[tuple[str, int], dict[str, Any]] = {}
        deduped: list[dict[str, Any]] = []
        for comment in result["inline_comments"]:
            key = (comment.get("file_path", ""), comment.get("line_number", 0))
            if key not in seen:
                seen[key] = comment
                deduped.append(comment)
            else:
                # 补充已有评论中缺失的字段
                existing = seen[key]
                for field in ("fix_suggestion", "fix_confidence", "suggestion"):
                    if field not in existing or existing[field] is None:
                        if field in comment and comment[field] is not None:
                            existing[field] = comment[field]
                logger.debug(f"去重合并行内评论: {key}")
                logger.debug(f"去重丢弃行内评论: {key}")
        result["inline_comments"] = deduped

    def _strip_json_block(self, review_text: str) -> str:
        """去除审查文本中的 JSON 块，保留其前后的 Markdown 正文"""
        start_idx = review_text.find(JSON_BLOCK_START_MARKER)
        if start_idx == -1:
            return review_text
        end_idx = review_text.find(JSON_BLOCK_END_MARKER, start_idx)
        if end_idx == -1:
            return review_text
        after_end = end_idx + len(JSON_BLOCK_END_MARKER)
        return (review_text[:start_idx] + review_text[after_end:]).strip()

    def _extract_structured_json(self, review_text: str) -> dict | None:
        """从审查文本中提取结构化 JSON 块

        Args:
            review_text: AI 审查文本

        Returns:
            解析后的 JSON 字典，失败返回 None
        """
        if not review_text:
            return None

        start_idx = review_text.find(JSON_BLOCK_START_MARKER)
        end_idx = review_text.find(JSON_BLOCK_END_MARKER, start_idx) if start_idx != -1 else -1

        if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
            return None

        # 提取 markers 之间的内容
        json_content = review_text[
            start_idx + len(JSON_BLOCK_START_MARKER) : end_idx
        ].strip()

        # 去除可能的 ```json``` 围栏
        if json_content.startswith("```json"):
            json_content = json_content[7:]
        elif json_content.startswith("```"):
            json_content = json_content[3:]
        if json_content.endswith("```"):
            json_content = json_content[:-3]
        json_content = json_content.strip()

        try:
            data = json.loads(json_content)
        except json.JSONDecodeError as e:
            logger.warning(f"结构化 JSON 解析失败: {e}")
            return None

        if not isinstance(data, dict):
            logger.warning("结构化 JSON 不是字典类型")
            return None

        # 校验 schema_version
        if data.get("schema_version") != JSON_SCHEMA_VERSION:
            logger.warning(
                f"JSON schema_version 不匹配: {data.get('schema_version')} != {JSON_SCHEMA_VERSION}"
            )
            return None

        # 校验必要字段
        if "issues" not in data or "overall_score" not in data:
            logger.warning("JSON 缺少 issues 或 overall_score 字段")
            return None

        # 校验 severity 值
        for issue in data.get("issues", []):
            severity = issue.get("severity", "")
            if severity not in VALID_SEVERITIES:
                logger.warning(f"JSON issue 包含非法 severity: {severity}")
                return None

        # 校验 decision 值（如果存在）
        if "decision" in data and data["decision"] not in VALID_DECISIONS:
            logger.warning(f"JSON 包含非法 decision: {data['decision']}")
            return None

        return data

    def _apply_json_result(
        self, result: dict[str, Any], json_data: dict[str, Any]
    ) -> bool:
        """将结构化 JSON 数据应用到结果字典

        Args:
            result: 结果字典（将被修改）
            json_data: 解析后的 JSON 数据

        Returns:
            是否成功应用
        """
        try:
            # 提取评分
            score = json_data.get("overall_score")
            if isinstance(score, (int, float)):
                result["overall_score"] = int(score)

            # JSON summary 仅作为辅助字段存储，不覆盖原始 Markdown 审查正文
            # result["summary"] 保持为完整 review_text（由调用方去除 JSON 块）

            # 提取决策
            if json_data.get("decision"):
                result["ai_decision"] = json_data["decision"]
            if json_data.get("decision_reason"):
                result["ai_decision_reason"] = json_data["decision_reason"]

            # 分类 issues
            for issue in json_data.get("issues", []):
                try:
                    severity = issue.get("severity", "suggestion")
                    issues_key = SEVERITY_TO_ISSUES_KEY.get(severity, "suggestions")

                    file_path = issue.get("file_path")
                    line_number = issue.get("line_number")

                    if file_path and line_number is not None:
                        line_number = int(line_number)
                        # 行内评论
                        inline_comment = {
                            "file_path": file_path,
                            "line_number": line_number,
                            "body": issue.get("description", ""),
                            "severity": severity,
                        }
                        if issue.get("end_line"):
                            inline_comment["start_line"] = line_number
                            inline_comment["line_number"] = int(issue["end_line"])

                        # 提取修复建议字段
                        fix_suggestion = issue.get("fix_suggestion")
                        fix_confidence = issue.get("fix_confidence")
                        suggestion_text = issue.get("suggestion")

                        if fix_suggestion:
                            inline_comment["fix_suggestion"] = fix_suggestion
                        if fix_confidence is not None:
                            try:
                                val = float(fix_confidence)
                                if not (0.0 <= val <= 1.0):
                                    logger.debug(
                                        f"fix_confidence 超出范围 {val}，已 clamp 到 [0.0, 1.0]"
                                    )
                                    val = max(0.0, min(1.0, val))
                                inline_comment["fix_confidence"] = val
                            except (ValueError, TypeError):
                                logger.debug(
                                    f"忽略无效的 fix_confidence 值: {fix_confidence}"
                                )
                        if suggestion_text:
                            inline_comment["suggestion"] = suggestion_text

                        result["inline_comments"].append(inline_comment)
                    else:
                        # 整体评论
                        content = issue.get("description", issue.get("title", ""))
                        if content:
                            result["comments"].append(
                                {
                                    "content": content,
                                    "severity": severity,
                                    "type": "overall",
                                }
                            )

                    # 按 severity 分组
                    title_or_desc = issue.get("title") or issue.get("description", "")
                    if title_or_desc and issues_key in result["issues"]:
                        result["issues"][issues_key].append(title_or_desc)

                except (ValueError, TypeError):
                    logger.warning(f"跳过格式异常的 issue: {issue.get('title', '?')}")
                    continue

            return True

        except Exception as e:
            logger.warning(f"应用 JSON 结果失败: {e}")
            return False

    def parse_label_recommendation(self, response_text: str) -> List[Dict[str, Any]]:
        """解析标签推荐响应

        Args:
            response_text: AI 返回的标签推荐文本

        Returns:
            推荐标签列表，格式：[{"name": str, "confidence": float, "reason": str}]
        """
        recommendations = []

        try:
            if not response_text or not response_text.strip():
                logger.warning("AI返回空响应")
                return []

            text = response_text.strip()

            # 尝试提取JSON代码块
            json_data = self._extract_json_from_response(text)
            if json_data:
                recommendations = self._parse_label_json(json_data)
            else:
                # JSON解析失败，尝试文本解析
                logger.warning("JSON解析失败，尝试文本解析")
                return self._parse_text_label_recommendation(response_text)

            logger.info(f"成功解析 {len(recommendations)} 个标签推荐")
            return recommendations

        except Exception as e:
            logger.error(f"解析标签推荐失败: {e}", exc_info=True)
            return []

    def _extract_json_from_response(self, text: str) -> Any:
        """从响应文本中提取JSON数据

        Args:
            text: 响应文本

        Returns:
            解析后的JSON数据，失败返回None
        """
        try:
            if "```json" in text:
                start = text.find("```json") + 7
                end = text.find("```", start)
                if end > start:
                    json_str = text[start:end].strip()
                else:
                    json_str = text[start:].strip()
                return json.loads(json_str)
            elif "```" in text:
                start = text.find("```") + 3
                end = text.find("```", start)
                if end > start:
                    json_str = text[start:end].strip()
                else:
                    json_str = text[start:].strip()
                return json.loads(json_str)
            else:
                return json.loads(text)
        except json.JSONDecodeError:
            return None

    def _parse_label_json(self, data: Any) -> List[Dict[str, Any]]:
        """解析标签JSON数据

        Args:
            data: JSON数据

        Returns:
            标签列表
        """
        recommendations = []

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
            for item in data:
                recommendations.append(
                    {
                        "name": item.get("name", ""),
                        "confidence": float(item.get("confidence", 0.5)),
                        "reason": item.get("reason", ""),
                    }
                )

        return recommendations

    def _parse_text_label_recommendation(self, text: str) -> List[Dict[str, Any]]:
        """从文本中解析标签推荐（后备方案）

        Args:
            text: 响应文本

        Returns:
            标签列表
        """
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
