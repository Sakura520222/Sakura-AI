"""评论服务 - 负责将审查结果发布到GitHub PR"""

from typing import Dict, Any, Optional
from loguru import logger

from backend.core.config import get_strategy_config
from backend.services.label_service import label_service


class CommentService:
    """评论服务"""

    def __init__(self):
        pass

    async def create_placeholder_comment(self, pr: Any, strategy: str) -> Any:
        """创建占位评论（使用 Issue Comment）

        使用 Issue Comment 而不是 PR Review，因为 COMMENT 类型的 review 无法被 dismiss。

        Args:
            pr: GitHub PR 对象
            strategy: 审查策略名称

        Returns:
            GitHub Issue Comment 对象，用于后续删除
        """
        try:
            # 获取策略名称
            strategy_info = get_strategy_config().get_strategy(strategy)
            strategy_name = strategy_info.get("name", "代码审查")

            # 构建占位消息
            placeholder_body = f"""# 🔄 正在审查中...

**Sakura AI** 正在使用 **{strategy_name}** 策略分析此 PR，请稍候...

---

*⏳ Sakura 正在思考中，这可能需要几十秒到几分钟，取决于代码量和复杂度*
"""

            # 使用 Issue Comment（可编辑、可删除）
            comment = pr.create_issue_comment(placeholder_body)

            logger.info(
                f"✓ 已创建占位评论到PR: {pr.base.repo.full_name}#{pr.number} "
                f"(Comment ID: {comment.id})"
            )

            return comment

        except Exception as e:
            logger.error(f"创建占位评论时出错: {e}", exc_info=True)
            raise

    async def delete_placeholder_comment(self, comment: Any):
        """删除占位评论

        Args:
            comment: GitHub Issue Comment 对象（占位评论）
        """
        try:
            comment.delete()
            logger.info(f"✓ 已删除占位评论 (Comment ID: {comment.id})")
        except Exception as e:
            logger.error(f"删除占位评论时出错: {e}", exc_info=True)
            # 不抛出异常，因为占位评论删除失败不应影响主流程

    async def update_review(
        self,
        comment: Any,
        review_result: Dict[str, Any],
        strategy: str,
        pr: Any = None,
        label_results: Optional[Dict[str, Any]] = None,
        analysis: Any = None,
    ):
        """更新已有的审查评论

        使用删除占位 comment + 创建正式 PR Review 的策略。
        如果有行内评论，会使用批量创建行内评论的方法。

        Args:
            comment: GitHub Issue Comment 对象（占位评论）
            review_result: AI 审查结果
            strategy: 审查策略名称
            pr: GitHub PR 对象
            label_results: 标签应用结果（可选）
            analysis: PR 分析结果（包含 diff 安全区）
        """
        try:
            # 检查是否有行内评论
            inline_comments = review_result.get("inline_comments", [])

            if pr:
                try:
                    # 1. 删除占位 comment
                    comment.delete()
                    logger.info(f"✓ 已删除占位评论 (Comment ID: {comment.id})")

                    # 2. 构建整体评论内容
                    review_body = self._format_comment(
                        review_result, strategy, label_results
                    )

                    # 3. 根据是否有行内评论选择不同的创建方式
                    if inline_comments:
                        # 使用批量创建行内评论的方法
                        logger.info(
                            f"发现 {len(inline_comments)} 条行内评论，使用批量创建"
                        )

                        # 验证和过滤行内评论（使用 Diff 安全区）
                        validated_comments = self._validate_inline_comments(
                            inline_comments, analysis
                        )

                        if len(validated_comments) < len(inline_comments):
                            filtered_count = len(inline_comments) - len(
                                validated_comments
                            )
                            logger.info(f"过滤掉 {filtered_count} 条无效的行内评论")

                        # 只有当有有效评论时才创建 review
                        if validated_comments:
                            review = await self.create_batch_inline_comments(
                                pr, validated_comments, overall_body=review_body
                            )
                            logger.info(
                                f"✓ 已创建带行内评论的审查 (Review ID: {review.id})"
                            )
                        else:
                            # 所有评论都被过滤，降级为普通 review
                            logger.info("所有行内评论都被过滤，创建普通 review")
                            review = pr.create_review(
                                body=review_body, event="COMMENT", comments=[]
                            )
                            logger.info(f"✓ 已创建普通审查 (Review ID: {review.id})")
                    else:
                        # 没有行内评论，创建普通 review
                        review = pr.create_review(
                            body=review_body, event="COMMENT", comments=[]
                        )
                        logger.info(f"✓ 已创建正式审查评论 (Review ID: {review.id})")

                except Exception as e:
                    logger.error(
                        "删除评论并创建 review 失败: {}", str(e), exc_info=True
                    )
                    # 降级方案：创建新的普通评论
                    review_body = self._format_comment(
                        review_result, strategy, label_results
                    )
                    pr.create_issue_comment(
                        "⚠️ 审查评论更新失败，已降级为普通评论\n\n" + review_body
                    )
                    logger.info("✓ 已降级为普通评论")
            else:
                logger.warning("无法更新评论：没有 PR 对象")

        except Exception as e:
            logger.error("更新评论时出错: {}", str(e), exc_info=True)
            raise

    async def update_review_with_error(
        self, comment: Any, error_message: str, pr: Any = None
    ):
        """将占位评论更新为错误消息

        使用删除占位 comment + 创建错误评论的策略。

        Args:
            comment: GitHub Issue Comment 对象（占位评论）
            error_message: 错误信息
            pr: GitHub PR 对象
        """
        try:
            error_body = f"""# ❌ 审查失败

抱歉，Sakura 在审查过程中出现错误：

```
{error_message}
```

请检查系统日志或联系管理员。

---

*此评论由 Sakura AI Reviewer 自动生成*
"""

            if pr:
                try:
                    # 1. 删除占位 comment
                    comment.delete()
                    logger.info(f"✓ 已删除占位评论 (Comment ID: {comment.id})")

                    # 2. 创建错误评论
                    pr.create_issue_comment(error_body)
                    logger.info("✓ 已创建错误评论")

                except Exception as e:
                    logger.error(
                        "删除评论并创建错误评论失败: {}", str(e), exc_info=True
                    )
                    # 降级方案：直接创建新的错误评论
                    pr.create_issue_comment(error_body)
                    logger.info("✓ 已降级为直接创建错误评论")
            else:
                logger.warning("无法更新错误评论：没有 PR 对象")

        except Exception as e:
            logger.error("更新错误消息时出错: {}", str(e), exc_info=True)
            # 如果更新失败，尝试记录日志但不中断流程

    async def post_review_comment(
        self, pr: Any, review_result: Dict[str, Any], strategy: str
    ):
        """发布审查评论到PR（使用create_review一次性发布）

        注意：此方法保留用于向后兼容，新代码应使用 create_placeholder_review + update_review
        """
        try:
            # 构建评论内容
            comment_body = self._format_comment(review_result, strategy)

            # 使用 create_review 一次性发布所有评论
            # 这样可以避免触发 GitHub API 频率限制
            pr.create_review(
                body=comment_body,
                event="COMMENT",  # COMMENT, APPROVE, REQUEST_CHANGES
                comments=[],  # 整体评论，不需要行内评论
            )

            logger.info(f"✓ 成功发布审查评论到PR: {pr.base.repo.full_name}#{pr.number}")

        except Exception as e:
            logger.error(f"发布评论时出错: {e}", exc_info=True)
            raise

    def _format_comment(
        self,
        review_result: Dict[str, Any],
        strategy: str,
        label_results: Optional[Dict[str, Any]] = None,
    ) -> str:
        """格式化评论内容"""
        lines = []

        # 添加标题
        strategy_info = get_strategy_config().get_strategy(strategy)
        strategy_name = strategy_info.get("name", "代码审查")
        lines.append(f"# 🌸 Sakura AI 审查报告 - {strategy_name}\n")

        # 添加评分（如果有）
        overall_score = review_result.get("overall_score")
        if overall_score:
            lines.append(f"## 📊 整体评分: {overall_score}/10\n")

        # 添加 AI 生成的完整审查内容（已包含所有格式化的问题分类）
        summary = review_result.get("summary", "")
        if summary:
            lines.append(summary)
            lines.append("")

        # 添加标签建议（如果有）
        if label_results:
            label_section = label_service.format_label_results(label_results)
            lines.append(label_section)

        # 添加页脚
        lines.append("\n---\n")
        lines.append("*此评论由 Sakura AI Reviewer 自动生成*")

        return "\n".join(lines)

    async def create_review_comment(self, pr: Any, body: str, event: str = "COMMENT"):
        """创建PR审查评论"""
        try:
            review = pr.create_review(body=body, event=event, comments=[])
            return review
        except Exception as e:
            logger.error(f"创建审查评论时出错: {e}")
            raise

    async def create_inline_comment(
        self, pr: Any, file_path: str, line_number: int, body: str
    ):
        """创建行内评论（单个）"""
        try:
            # 获取commit
            commit = pr.head.repo.get_commit(pr.head.sha)

            # 创建行内评论
            comment = pr.create_review_comment(
                body=body, path=file_path, line=line_number, commit_id=commit.sha
            )
            return comment
        except Exception as e:
            logger.error(f"创建行内评论时出错: {e}")
            raise

    async def create_batch_inline_comments(
        self, pr: Any, inline_comments: list, overall_body: str = ""
    ):
        """批量创建行内评论

        使用 GitHub PR Review API 一次性创建多个行内评论

        Args:
            pr: GitHub PR 对象
            inline_comments: 行内评论列表，格式：
                [
                    {
                        "file_path": str,
                        "line_number": int,
                        "body": str,
                        "severity": str
                    }
                ]
            overall_body: 整体评论内容（可选）

        Returns:
            GitHub Review 对象
        """
        try:
            if not inline_comments:
                logger.info("没有行内评论需要创建")
                return None

            logger.info(f"开始批量创建 {len(inline_comments)} 条行内评论")

            # 构建 GitHub API 需要的评论格式
            comments = []
            for comment_data in inline_comments:
                try:
                    # 构建评论内容，包含严重程度标记
                    body = comment_data["body"]
                    severity = comment_data.get("severity", "suggestion")

                    # 添加严重程度标记
                    severity_emoji = {
                        "critical": "🔴",
                        "major": "🟡",
                        "suggestion": "💡",
                        "minor": "🔵",
                    }.get(severity, "💡")

                    formatted_body = f"{severity_emoji} {body}"

                    # 构建评论字典
                    comment_dict = {
                        "path": comment_data["file_path"],
                        "line": comment_data["line_number"],
                        "body": formatted_body,
                    }

                    # 如果有起始行号，添加范围支持（跨多行评论）
                    if "start_line" in comment_data:
                        comment_dict["start_line"] = comment_data["start_line"]

                    comments.append(comment_dict)

                except Exception as e:
                    logger.warning(f"跳过无效的行内评论数据: {comment_data}, 错误: {e}")
                    continue

            if not comments:
                logger.warning("没有有效的行内评论可创建")
                return None

            # 使用 create_review 一次性创建所有评论
            review = pr.create_review(
                body=overall_body or "🌸 Sakura AI 代码审查",
                event="COMMENT",
                comments=comments,
            )

            logger.info(f"✓ 成功批量创建 {len(comments)} 条行内评论")
            return review

        except Exception as e:
            # "剥茧抽丝"式错误日志
            logger.error(f"批量创建行内评论时出错: {e}")

            # 尝试捕获 GithubException 的详细信息
            try:
                if hasattr(e, "status") and hasattr(e, "data"):
                    logger.error("GitHub API 错误详情:")
                    logger.error(f"  - Status: {e.status}")
                    logger.error(f"  - Data: {e.data}")
                    if isinstance(e.data, dict):
                        logger.error(f"  - Message: {e.data.get('message', 'N/A')}")
                        logger.error(f"  - Errors: {e.data.get('errors', 'N/A')}")
                else:
                    logger.error(f"异常类型: {type(e).__name__}")
                    logger.error(f"异常信息: {str(e)}")
            except Exception as log_error:
                logger.error(f"无法解析详细错误信息: {log_error}")

            # 打印评论数据用于调试
            logger.error(f"失败的评论数量: {len(inline_comments)}")
            for i, comment in enumerate(inline_comments[:3], 1):  # 只打印前3条
                logger.error(
                    f"  评论 {i}: {comment['file_path']}:{comment['line_number']}"
                )

            raise

    def _validate_inline_comments(self, inline_comments: list, analysis: Any) -> list:
        """验证行内评论，过滤掉无效的评论

        使用 Diff 安全区白名单验证行号，并智能匹配文件路径。

        Args:
            inline_comments: AI 给出的行内评论列表
            analysis: PR 分析结果（包含 changed_lines_map）

        Returns:
            验证通过的行内评论列表
        """
        if not analysis or not analysis.changed_lines_map:
            logger.warning("没有 Diff 安全区数据，跳过验证")
            return inline_comments

        validated = []
        changed_lines_map = analysis.changed_lines_map

        # 构建 PR 中的文件路径集合（用于智能匹配）
        pr_files = set(changed_lines_map.keys())

        for comment in inline_comments:
            file_path = comment.get("file_path", "")
            line_number = comment.get("line_number")

            # 1. 智能路径匹配
            matched_path = self._match_file_path(file_path, pr_files)

            if not matched_path:
                logger.warning(f"文件路径无法匹配: {file_path}，跳过该评论")
                continue

            # 2. 验证行号是否在 Diff 安全区内
            allowed_lines = changed_lines_map.get(matched_path, set())

            if line_number not in allowed_lines:
                logger.warning(
                    f"行号 {line_number} 不在 Diff 安全区内 "
                    f"(文件: {matched_path}, 允许的行号: {sorted(list(allowed_lines))[:5]}...)"
                )
                continue

            # 3. 验证 start_line（多行评论的起始行）
            start_line = comment.get("start_line")
            if start_line is not None:
                if start_line == line_number:
                    # 单行评论不需要 start_line，移除避免 API 问题
                    start_line = None
                elif start_line not in allowed_lines:
                    logger.warning(
                        f"start_line {start_line} 不在 Diff 安全区内 "
                        f"(文件: {matched_path})，降级为单行评论 (行号 {line_number})"
                    )
                    start_line = None
                # start_line 和 line_number 必须在同一 hunk 内
                elif (
                    analysis.hunk_boundaries
                    and matched_path in analysis.hunk_boundaries
                ):
                    same_hunk = False
                    for hunk_start, hunk_end in analysis.hunk_boundaries[
                        matched_path
                    ]:
                        if (
                            hunk_start <= start_line <= hunk_end
                            and hunk_start <= line_number <= hunk_end
                        ):
                            same_hunk = True
                            break
                    if not same_hunk:
                        logger.warning(
                            f"跨 hunk 多行评论: {matched_path}:{start_line}-{line_number}，"
                            f"降级为单行评论 (行号 {line_number})"
                        )
                        start_line = None

            # 4. 构建验证通过的评论副本（不修改原始数据）
            validated_comment = {
                "file_path": matched_path,
                "line_number": line_number,
                "body": comment.get("body", ""),
                "severity": comment.get("severity", "suggestion"),
            }
            if start_line:
                validated_comment["start_line"] = start_line
            validated.append(validated_comment)
            logger.debug(f"✓ 验证通过: {matched_path}:{line_number}")

        return validated

    def _match_file_path(self, ai_path: str, pr_files: set) -> Optional[str]:
        """智能匹配文件路径

        处理 AI 可能给出的路径与 PR 中实际路径不一致的情况。

        Args:
            ai_path: AI 给出的文件路径
            pr_files: PR 中的文件路径集合

        Returns:
            匹配的文件路径，如果找不到则返回 None
        """
        # 1. 完全匹配
        if ai_path in pr_files:
            return ai_path

        # 2. 尝试去掉前缀（如 backend/）
        # 处理 AI 输出 "backend/config.py" 但 PR 中只有 "config.py" 的情况
        parts = ai_path.split("/")
        for i in range(len(parts)):
            test_path = "/".join(parts[i:])
            if test_path in pr_files:
                logger.debug(f"路径匹配: {ai_path} -> {test_path}")
                return test_path

        # 3. 尝试添加常见前缀
        common_prefixes = ["backend/", "src/", "app/", "lib/"]
        for prefix in common_prefixes:
            test_path = prefix + ai_path
            if test_path in pr_files:
                logger.debug(f"路径匹配: {ai_path} -> {test_path}")
                return test_path

        # 4. 文件名匹配（最后手段）
        filename = ai_path.split("/")[-1]
        matches = [f for f in pr_files if f.endswith(filename)]

        if len(matches) == 1:
            logger.debug(f"路径匹配: {ai_path} -> {matches[0]} (通过文件名)")
            return matches[0]
        elif len(matches) > 1:
            logger.warning(f"文件名 {filename} 匹配到多个文件: {matches}，跳过")

        logger.warning(f"无法匹配文件路径: {ai_path}")
        return None

    def format_file_review(
        self, file_path: str, review_text: str, line_number: int = None
    ) -> str:
        """格式化文件审查评论"""
        comment = f"## 📄 {file_path}\n\n"
        if line_number:
            comment += f"**行号**: {line_number}\n\n"
        comment += review_text
        return comment
