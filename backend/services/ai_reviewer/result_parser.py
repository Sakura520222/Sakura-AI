"""Parsers for PR review results and label recommendations."""

import json
from typing import Any

from loguru import logger

from .review_protocol import TaggedReviewParser, to_review_result


class ReviewResultParser:
    """Parse strict PR reviews while preserving the label recommendation API."""

    def parse_review_result(self, review_text: str, strategy: str) -> dict[str, Any]:
        """Parse a strict tagged PR review response."""
        result = to_review_result(TaggedReviewParser().parse(review_text))
        logger.info(
            "结构化标签解析成功 (策略: {}, decision: {}, findings: {})",
            strategy,
            result["ai_decision"],
            len(result["comments"]) + len(result["inline_comments"]),
        )
        return result

    def parse_label_recommendation(self, response_text: str) -> list[dict[str, Any]]:
        """解析标签推荐响应。"""
        recommendations = []

        try:
            if not response_text or not response_text.strip():
                logger.warning("AI返回空响应")
                return []

            text = response_text.strip()
            json_data = self._extract_json_from_response(text)
            if json_data:
                recommendations = self._parse_label_json(json_data)
            else:
                logger.warning("JSON解析失败，尝试文本解析")
                return self._parse_text_label_recommendation(response_text)

            logger.info("成功解析 {} 个标签推荐", len(recommendations))
            return recommendations
        except Exception as exc:
            logger.error("解析标签推荐失败: {}", exc, exc_info=True)
            return []

    def _extract_json_from_response(self, text: str) -> Any:
        """从标签推荐响应中提取 JSON 数据。"""
        try:
            if "```json" in text:
                start = text.find("```json") + 7
                end = text.find("```", start)
                json_str = (
                    text[start:end].strip() if end > start else text[start:].strip()
                )
                return json.loads(json_str)
            if "```" in text:
                start = text.find("```") + 3
                end = text.find("```", start)
                json_str = (
                    text[start:end].strip() if end > start else text[start:].strip()
                )
                return json.loads(json_str)
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def _parse_label_json(self, data: Any) -> list[dict[str, Any]]:
        """解析标签推荐 JSON。"""
        recommendations = []
        items = data.get("labels", []) if isinstance(data, dict) else data
        if not isinstance(items, list):
            return recommendations

        for item in items:
            if not isinstance(item, dict):
                continue
            recommendations.append(
                {
                    "name": item.get("name", ""),
                    "confidence": float(item.get("confidence", 0.5)),
                    "reason": item.get("reason", ""),
                }
            )
        return recommendations

    def _parse_text_label_recommendation(self, text: str) -> list[dict[str, Any]]:
        """从文本中解析标签推荐（后备方案）。"""
        recommendations = []

        for line in text.split("\n"):
            line = line.strip()
            if not (line.startswith(("-", "*"))):
                continue

            parts = line[1:].strip().split("(", 1)
            label_name = parts[0].strip()
            confidence = 0.5
            reason = ""

            if len(parts) > 1:
                rest = parts[1]
                if ")" in rest:
                    conf_str = rest.split(")")[0].strip()
                    try:
                        if "%" in conf_str:
                            confidence = float(conf_str.replace("%", "").strip()) / 100
                        else:
                            confidence = float(conf_str)
                    except ValueError:
                        pass

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
