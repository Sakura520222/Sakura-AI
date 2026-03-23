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
    SEVERITY_TO_ISSUES_KEY,
)


class ReviewResultParser:
    """审查结果解析器

    负责解析 AI 返回的审查文本，提取：
    - 整体评论（按严重程度分类）
    - 行内评论（带文件路径和行号）
    - 评分信息
    - 标签推荐
    """

    def parse_review_result(
        self, review_text: str, strategy: str
    ) -> Dict[str, Any]:
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
        """
        result = {
            "summary": review_text,
            "comments": [],
            "inline_comments": [],
            "overall_score": None,
            "issues": {"critical": [], "major": [], "minor": [], "suggestions": []},
        }

        try:
            # 使用ScoreExtractor提取评分
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

            # 提取结构化评论
            self._parse_structured_comments(result, review_text)

            # 如果没有提取到结构化评论，将整个文本作为摘要
            if not result["comments"]:
                result["summary"] = review_text

        except Exception as e:
            logger.warning(f"解析审查结果时出错: {e}")
            result["summary"] = review_text

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

    def extract_inline_comments(
        self, result: Dict[str, Any], review_text: str
    ) -> None:
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
