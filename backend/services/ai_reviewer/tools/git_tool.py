"""Git 信息工具处理器

为 AI 审查员提供获取仓库基本信息和提交历史的能力：
- get_git_info: 获取仓库描述、默认分支、语言统计、分支列表等
- list_commits: 查看指定分支的提交历史记录
"""

from typing import Any

from loguru import logger

from backend.core.config import get_strategy_config


class GitToolHandler:
    """Git 信息工具处理器

    负责处理仓库信息和提交历史的查询。
    """

    def _get_config(self) -> dict:
        """从策略配置读取 git_info 相关配置

        Returns:
            包含默认参数的配置字典
        """
        ce = get_strategy_config().get_context_enhancement_config()
        git_config = ce.get("git_tools", {})
        return {
            "default_branch_count": int(git_config.get("default_branch_count", 20)),
            "default_commit_count": int(git_config.get("default_commit_count", 10)),
        }

    async def get_git_info(
        self,
        repo: Any,
        pr: Any,
        branch_count: int | None = None,
    ) -> dict[str, Any]:
        """获取仓库基本信息

        包括描述、默认分支、语言统计、主题、许可证和分支列表。

        Args:
            repo: GitHub 仓库对象
            pr: GitHub PR 对象（可选，用于日志）
            branch_count: 返回的分支数量

        Returns:
            仓库信息字典
        """
        try:
            config = self._get_config()
            effective_branch_count = (
                branch_count
                if branch_count is not None
                else config["default_branch_count"]
            )

            # 获取仓库基本信息 / Get basic repo info
            info: dict[str, Any] = {
                "full_name": repo.full_name,
                "description": repo.description,
                "default_branch": repo.default_branch,
                "language": repo.language,
                "topics": repo.get_topics() if hasattr(repo, "get_topics") else [],
                "private": repo.private,
                "fork": repo.fork,
            }

            # 获取语言统计 / Get language statistics
            try:
                languages = repo.get_languages()
                info["languages"] = languages
            except Exception as e:
                logger.debug(f"获取仓库语言统计失败: {e}")
                info["languages"] = {}

            # 获取许可证信息 / Get license info
            try:
                license_info = repo.get_license()
                if license_info:
                    info["license"] = {
                        "name": license_info.license.name
                        if license_info.license
                        else None,
                        "spdx_id": license_info.license.spdx_id
                        if license_info.license
                        else None,
                    }
            except Exception as e:
                logger.debug(f"获取仓库许可证信息失败: {e}")
                info["license"] = None

            # 获取分支列表 / Get branch list
            try:
                branches = repo.get_branches()
                branch_list: list[dict[str, str]] = []
                for branch in branches[:effective_branch_count]:
                    branch_list.append(
                        {
                            "name": branch.name,
                            "sha": branch.commit.sha,
                        }
                    )

                # PyGithub PaginatedList 无法直接获取 total_count，
                # 如果返回数量等于请求数量，说明可能还有更多
                info["branches"] = branch_list
                info["returned_branch_count"] = len(branch_list)
                if len(branch_list) >= effective_branch_count:
                    info["branches_hint"] = (
                        f"仅显示前 {effective_branch_count} 个分支，"
                        "可通过 branch_count 参数获取更多"
                    )
            except Exception as e:
                logger.debug(f"获取仓库分支列表失败: {e}")
                info["branches"] = []
                info["returned_branch_count"] = 0

            return info

        except Exception as e:
            logger.error(f"获取仓库信息时发生错误: {e}", exc_info=True)
            return {
                "error": f"获取仓库信息失败: {e}",
            }

    async def list_commits(
        self,
        repo: Any,
        pr: Any,
        branch: str | None = None,
        per_page: int | None = None,
    ) -> dict[str, Any]:
        """查看指定分支的提交历史记录

        优先级：参数指定 > PR 的 HEAD 分支 > 仓库默认分支。

        Args:
            repo: GitHub 仓库对象
            pr: GitHub PR 对象（可选）
            branch: 分支名
            per_page: 返回的提交数量

        Returns:
            提交历史字典
        """
        try:
            config = self._get_config()
            effective_per_page = (
                per_page if per_page is not None else config["default_commit_count"]
            )

            # 确定分支 / Determine branch
            ref = branch
            if not ref:
                if pr is not None:
                    ref = pr.head.ref
                else:
                    ref = repo.default_branch

            # 获取提交列表 / Get commit list
            commits = repo.get_commits(sha=ref)

            # PyGithub 返回 PaginatedList，使用切片获取指定数量
            # PyGithub returns a PaginatedList, use slicing for specified count
            commit_list: list[dict[str, Any]] = []
            for commit in commits[:effective_per_page]:
                commit_info: dict[str, Any] = {
                    "sha": commit.sha,
                    "message": commit.commit.message,
                    "date": str(commit.commit.author.date),
                    "author": {
                        "name": commit.commit.author.name,
                        "email": commit.commit.author.email,
                    },
                    "url": commit.html_url,
                }

                # 添加验证状态 / Add verification status if available
                if commit.commit.verification:
                    commit_info["verified"] = commit.commit.verification.verified

                commit_list.append(commit_info)

            return {
                "branch": ref,
                "commits": commit_list,
                "returned_count": len(commit_list),
            }

        except Exception as e:
            logger.error(f"获取提交历史时发生错误: {e}", exc_info=True)
            return {
                "branch": branch or "unknown",
                "error": f"获取提交历史失败: {e}",
                "commits": [],
                "returned_count": 0,
            }
