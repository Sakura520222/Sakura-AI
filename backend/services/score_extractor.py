"""评分提取服务 - 从AI审查结果中提取和验证评分

提供多层fallback机制确保评分的准确性和一致性：
1. 直接使用 overall_score 字段（如果有效）
2. 从 summary 文本中提取评分
3. 从批次评分列表计算平均值
4. 基于问题统计估算评分
"""

import re
from typing import Dict, Any, Optional, List
from loguru import logger


class ScoreExtractor:
    """评分提取器 - 支持多种评分格式和fallback机制"""

    # 编译后的正则表达式模式（提高性能）
    PATTERNS = [
        re.compile(r"评分[：:]\s*(\d+)"),  # "评分：8" 或 "评分: 8"
        re.compile(r"得分[：:]\s*(\d+)"),  # "得分：8"
        re.compile(r"分数[：:]\s*(\d+)"),  # "分数：8"
        re.compile(r"overall_score[：:]\s*(\d+)"),  # "overall_score: 8"
        re.compile(r"quality_score[：:]\s*(\d+)"),  # "quality_score: 8"
        re.compile(r"整体?评?[分][：:]\s*(\d+)"),  # "评分：8" 或 "整体评分：8"
        re.compile(r"代码质量[评]?分[：:]\s*(\d+)"),  # "代码质量评分：8"
    ]

    # 评分范围
    MIN_SCORE = 1
    MAX_SCORE = 10

    def __init__(self):
        """初始化评分提取器"""
        self.extraction_stats = {
            "direct_field": 0,  # 直接使用overall_score字段
            "from_summary": 0,  # 从summary文本提取
            "from_batch_avg": 0,  # 从批次平均计算
            "from_issues": 0,  # 从问题统计估算
            "fallback_zero": 0,  # 使用默认值0
        }

    def validate_score(self, score: int) -> bool:
        """验证评分范围是否在1-10之间

        Args:
            score: 待验证的评分

        Returns:
            True如果评分有效，False otherwise
        """
        return self.MIN_SCORE <= score <= self.MAX_SCORE

    def extract_from_text(self, text: str) -> Optional[int]:
        """从文本中提取评分（支持多种格式）

        Args:
            text: 包含评分信息的文本

        Returns:
            提取到的评分（1-10），如果无法提取则返回None
        """
        if not text or not isinstance(text, str):
            return None

        # 尝试所有模式
        for pattern in self.PATTERNS:
            match = pattern.search(text)
            if match:
                try:
                    score = int(match.group(1))
                    if self.validate_score(score):
                        logger.debug(
                            f"✅ 从文本提取评分: {score}/10 (模式: {pattern.pattern})"
                        )
                        return score
                    else:
                        logger.warning(f"⚠️ 提取到无效评分 {score}/10 (不在1-10范围)")
                except (ValueError, IndexError) as e:
                    logger.debug(f"评分提取失败: {e}")

        logger.debug("⚠️ 未能从文本中提取有效评分")
        return None

    def estimate_from_issues(self, issues: Dict[str, Any]) -> Optional[int]:
        """基于问题统计估算评分（最后的fallback）

        估算逻辑：
        - 0个问题 → 10分
        - 只有suggestions → 8分
        - 有minor → 7分
        - 有major → 5分
        - 有critical → 2分

        Args:
            issues: 问题统计字典，包含 critical, major, minor, suggestions

        Returns:
            估算的评分（1-10），如果无法估算则返回None
        """
        if not issues or not isinstance(issues, dict):
            return None

        try:
            critical_count = len(issues.get("critical", []))
            major_count = len(issues.get("major", []))
            minor_count = len(issues.get("minor", []))
            suggestion_count = len(issues.get("suggestions", []))

            total_issues = critical_count + major_count + minor_count + suggestion_count

            if total_issues == 0:
                # 没有任何问题
                estimated_score = 10
            elif critical_count > 0:
                # 有严重问题，低分
                estimated_score = max(2, 5 - critical_count)
            elif major_count > 0:
                # 有重要问题
                estimated_score = max(4, 7 - major_count)
            elif minor_count > 0:
                # 有次要问题
                estimated_score = max(6, 8 - minor_count // 2)
            elif suggestion_count > 0:
                # 只有建议
                estimated_score = 8
            else:
                return None

            logger.info(
                f"📊 基于问题统计估算评分: {estimated_score}/10 "
                f"(C:{critical_count}, M:{major_count}, m:{minor_count}, S:{suggestion_count})"
            )
            return estimated_score

        except Exception as e:
            logger.error(f"估算评分时出错: {e}")
            return None

    def calculate_batch_average(self, batch_scores: List[int]) -> Optional[int]:
        """计算批次评分的平均值（用于多批次场景）

        Args:
            batch_scores: 批次评分列表

        Returns:
            平均评分（1-10），如果列表为空则返回None
        """
        if not batch_scores:
            return None

        # 过滤掉None和无效评分
        valid_scores = [
            s for s in batch_scores if s is not None and self.validate_score(s)
        ]

        if not valid_scores:
            logger.warning("⚠️ 没有有效的批次评分可用于计算平均值")
            return None

        average = int(sum(valid_scores) / len(valid_scores))
        logger.info(
            f"📊 计算批次平均评分: {average}/10 "
            f"(基于 {len(valid_scores)} 个批次: {valid_scores})"
        )
        return average

    def extract_score(self, review_result: Dict[str, Any]) -> Optional[int]:
        """从审查结果中提取评分，支持多种fallback机制

        Fallback链：
        1. 直接使用 overall_score 字段（如果有效）
        2. 从 summary 文本中提取评分
        3. 从批次评分列表计算平均值
        4. 基于问题统计估算评分

        Args:
            review_result: 审查结果字典，可能包含：
                - overall_score: 直接评分
                - summary: 总结文本（可能包含评分）
                - batch_scores: 批次评分列表（多批次场景）
                - issues: 问题统计字典

        Returns:
            提取到的评分（1-10），如果所有方法都失败则返回None
        """
        if not review_result or not isinstance(review_result, dict):
            logger.warning("⚠️ review_result为空或不是字典")
            return None

        # 方法1: 直接使用 overall_score 字段
        score = review_result.get("overall_score")
        if score is not None:
            if self.validate_score(score):
                logger.info(f"✅ 使用overall_score字段: {score}/10")
                self.extraction_stats["direct_field"] += 1
                return score
            else:
                logger.warning(f"⚠️ overall_score无效 ({score}/10)，尝试fallback")

        # 方法2: 从 summary 文本中提取评分
        summary = review_result.get("summary", "")
        if summary:
            score = self.extract_from_text(summary)
            if score is not None:
                logger.info(f"✅ 从summary文本提取评分: {score}/10")
                self.extraction_stats["from_summary"] += 1
                return score

        # 方法3: 从批次评分列表计算平均值
        batch_scores = review_result.get("batch_scores", [])
        if batch_scores:
            score = self.calculate_batch_average(batch_scores)
            if score is not None:
                logger.info(f"✅ 使用批次平均评分: {score}/10")
                self.extraction_stats["from_batch_avg"] += 1
                return score

        # 方法4: 基于问题统计估算评分
        issues = review_result.get("issues", {})
        if issues:
            score = self.estimate_from_issues(issues)
            if score is not None:
                logger.info(f"✅ 使用问题统计估算评分: {score}/10")
                self.extraction_stats["from_issues"] += 1
                return score

        # 所有方法都失败
        logger.warning("❌ 无法从任何来源提取评分")
        self.extraction_stats["fallback_zero"] += 1
        return None

    def get_extraction_stats(self) -> Dict[str, int]:
        """获取评分提取统计信息

        Returns:
            统计字典，包含各方法的调用次数
        """
        return self.extraction_stats.copy()

    def reset_stats(self):
        """重置统计信息"""
        self.extraction_stats = {
            "direct_field": 0,
            "from_summary": 0,
            "from_batch_avg": 0,
            "from_issues": 0,
            "fallback_zero": 0,
        }


# 全局单例实例
_score_extractor = None


def get_score_extractor() -> ScoreExtractor:
    """获取评分提取器单例实例

    Returns:
        ScoreExtractor实例
    """
    global _score_extractor
    if _score_extractor is None:
        _score_extractor = ScoreExtractor()
        logger.info("✅ ScoreExtractor单例已初始化")
    return _score_extractor


# 便捷访问点
score_extractor = get_score_extractor()
