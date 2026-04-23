"""GitHub 文件写入服务 / GitHub file write service

使用 PyGithub Contents API 将文件提交回仓库，
用于 .sakura/ 记忆系统等需要写入文件到仓库的场景。
当仓库分支保护禁止直接提交时，自动回退为创建分支 + PR + 尝试合并。

Uses PyGithub's Contents API to commit files back to repositories,
for scenarios like the .sakura/ memory system. Falls back to creating
a branch + PR + auto-merge when branch protection blocks direct commits.
"""

import asyncio
import base64
from datetime import datetime
from typing import Optional

from github import InputGitAuthor
from github.GithubException import GithubException, UnknownObjectException
from loguru import logger


class GitHubWriteService:
    """GitHub 文件写入服务 / GitHub file write service"""

    DEFAULT_AUTHOR_NAME = "Sakura AI Reviewer"
    DEFAULT_AUTHOR_EMAIL = "Sakura520222@163.com"
    SAKURA_BRANCH_PREFIX = "sakura-memory"

    def __init__(self):
        """初始化 / Initialize"""
        logger.info("GitHubWriteService initialized")

    async def commit_files(
        self,
        repo,
        files: dict,
        message: str,
        branch: Optional[str] = None,
    ) -> str:
        """通过 Contents API 提交多个文件到仓库

        先尝试直接提交到目标分支；若遇到分支保护规则（409），
        自动回退为创建新分支 + PR + 尝试合并。

        Args:
            repo: PyGithub Repository 对象
            files: {文件路径: 文件内容} 的字典
            message: 提交信息
            branch: 目标分支，默认使用仓库默认分支

        Returns:
            最后一个提交的 SHA
        """
        if not files:
            raise ValueError("No files to commit")

        if branch is None:
            branch = await self.get_default_branch(repo)

        author = InputGitAuthor(
            name=self.DEFAULT_AUTHOR_NAME,
            email=self.DEFAULT_AUTHOR_EMAIL,
        )

        # 尝试直接提交 / Try direct commit first
        try:
            last_sha = None
            for path, content in files.items():
                last_sha = await self._commit_single_file(
                    repo, path, content, message, branch, author,
                )
            logger.info(
                "Committed {} file(s) directly to {}:{} -> {}",
                len(files), repo.full_name, branch,
                last_sha[:8] if last_sha else "unknown",
            )
            return last_sha or ""
        except GithubException as e:
            if e.status != 409:
                raise

        # 409: 分支保护 → 回退到 PR / Branch protection → fallback to PR
        logger.info(
            "Branch protection (409) on {}:{}, falling back to PR",
            repo.full_name, branch,
        )
        return await self._commit_via_pr(repo, files, message, branch, author)

    async def _commit_single_file(
        self,
        repo,
        path: str,
        content: str,
        message: str,
        branch: str,
        author: InputGitAuthor,
    ) -> str:
        """提交单个文件（自动判断创建或更新）/ Commit a single file"""

        def _sync() -> str:
            try:
                existing = repo.get_contents(path, ref=branch)
                if isinstance(existing, list):
                    raise ValueError(f"Path {path} is a directory")
                result = repo.update_file(
                    path=path, message=message, content=content,
                    sha=existing.sha, branch=branch, author=author,
                )
            except UnknownObjectException:
                result = repo.create_file(
                    path=path, message=message, content=content,
                    branch=branch, author=author,
                )

            commit_obj = result.get("commit") if isinstance(result, dict) else None
            if commit_obj is None:
                raise RuntimeError(f"create_file/update_file returned no commit for {path}")
            return commit_obj.sha

        sha = await asyncio.to_thread(_sync)
        logger.info("Committed {} -> {}", path, sha[:8])
        return sha

    async def _commit_to_branch(
        self, repo, files: dict, message: str, branch: str, author: InputGitAuthor,
    ) -> str:
        """提交文件到指定分支（不创建 PR）/ Commit files to an existing branch"""

        def _sync() -> str:
            last_sha = None
            for path, content in files.items():
                try:
                    existing = repo.get_contents(path, ref=branch)
                    if isinstance(existing, list):
                        raise ValueError(f"Path {path} is a directory")
                    result = repo.update_file(
                        path=path, message=message, content=content,
                        sha=existing.sha, branch=branch, author=author,
                    )
                except UnknownObjectException:
                    result = repo.create_file(
                        path=path, message=message, content=content,
                        branch=branch, author=author,
                    )
                commit_obj = result.get("commit") if isinstance(result, dict) else None
                if commit_obj:
                    last_sha = commit_obj.sha
            return last_sha or ""

        sha = await asyncio.to_thread(_sync)
        logger.info(
            "Appended {} file(s) to {}:{} -> {}",
            len(files), repo.full_name, branch,
            sha[:8] if sha else "unknown",
        )
        return sha

    async def _commit_via_pr(
        self, repo, files: dict, message: str, base_branch: str, author: InputGitAuthor,
    ) -> str:
        """通过创建分支 + PR + 尝试合并来提交文件 / Commit via branch + PR + auto-merge"""

        # 查找已有未合并分支 / Check for existing open sakura PR branch
        existing_branch = await self._find_open_sakura_branch(repo)

        if existing_branch:
            logger.info(
                "Found existing open branch {} for {}, appending commits",
                existing_branch, repo.full_name,
            )
            return await self._commit_to_branch(
                repo, files, message, existing_branch, author,
            )

        # 无已有分支 → 创建新分支 + PR / No existing branch → create new
        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        new_branch = f"{self.SAKURA_BRANCH_PREFIX}/{timestamp}"

        def _sync() -> str:
            # 1. 从 base branch 创建新分支 / Create branch from base
            ref = repo.get_git_ref(f"heads/{base_branch}")
            base_sha = ref.object.sha
            repo.create_git_ref(ref=f"refs/heads/{new_branch}", sha=base_sha)
            logger.info(
                "Created branch {} from {}:{}", new_branch, repo.full_name, base_branch,
            )

            # 2. 提交文件到新分支 / Commit files to new branch
            last_sha = None
            for path, content in files.items():
                try:
                    existing = repo.get_contents(path, ref=new_branch)
                    if isinstance(existing, list):
                        raise ValueError(f"Path {path} is a directory")
                    result = repo.update_file(
                        path=path, message=message, content=content,
                        sha=existing.sha, branch=new_branch, author=author,
                    )
                except UnknownObjectException:
                    result = repo.create_file(
                        path=path, message=message, content=content,
                        branch=new_branch, author=author,
                    )
                commit_obj = result.get("commit") if isinstance(result, dict) else None
                if commit_obj:
                    last_sha = commit_obj.sha

            # 3. 创建 PR / Create pull request
            pr_title = message[:72]
            pr_body = (
                "Automated commit by **Sakura AI Reviewer**.\n\n"
                f"Files: {', '.join(f'`{p}`' for p in files.keys())}"
            )
            pr = repo.create_pull(
                title=pr_title,
                body=pr_body,
                head=new_branch,
                base=base_branch,
            )
            logger.info(
                "Created PR #{}: {} -> {}",
                pr.number, new_branch, base_branch,
            )

            # 4. 尝试自动合并 / Try auto-merge
            try:
                merge_result = pr.merge(merge_method="merge")
                if merge_result.merged:
                    logger.info(
                        "Auto-merged PR #{} for {}", pr.number, repo.full_name,
                    )
                    # 5. 合并成功后清理分支 / Cleanup branch after merge
                    try:
                        ref = repo.get_git_ref(f"heads/{new_branch}")
                        ref.delete()
                        logger.info("Deleted branch {}", new_branch)
                    except Exception as cleanup_err:
                        logger.debug("Branch cleanup skipped: {}", cleanup_err)
                else:
                    logger.warning(
                        "Auto-merge returned not-merged for PR #{}: {}",
                        pr.number, merge_result.message,
                    )
            except Exception as merge_err:
                logger.warning(
                    "Auto-merge failed for PR #{}, files will be available after manual merge: {}",
                    pr.number, merge_err,
                )

            return last_sha or ""

        try:
            return await asyncio.to_thread(_sync)
        except Exception as e:
            logger.error(
                "Failed to commit via PR to {}:{}: [{}] {}",
                repo.full_name, base_branch, type(e).__name__, e,
            )
            raise

    async def read_file(self, repo, path: str, ref: Optional[str] = None) -> Optional[str]:
        """从仓库读取文件内容 / Read file content from repository"""

        def _sync() -> Optional[str]:
            kwargs = {}
            if ref is not None:
                kwargs["ref"] = ref
            content_file = repo.get_contents(path, **kwargs)
            if isinstance(content_file, list):
                logger.warning("Path {} is a directory, not a file", path)
                return None
            decoded = base64.b64decode(content_file.content)
            return decoded.decode("utf-8")

        try:
            return await asyncio.to_thread(_sync)
        except UnknownObjectException:
            logger.debug("File not found: {}", path)
            return None
        except Exception as e:
            logger.error("Failed to read file {}: {}", path, e)
            return None

    async def file_exists(self, repo, path: str, ref: Optional[str] = None) -> bool:
        """检查文件是否存在于仓库中 / Check if a file exists in the repository"""

        def _sync() -> bool:
            kwargs = {}
            if ref is not None:
                kwargs["ref"] = ref
            repo.get_contents(path, **kwargs)
            return True

        try:
            return await asyncio.to_thread(_sync)
        except UnknownObjectException:
            return False
        except Exception as e:
            logger.error("Failed to check file existence {}: {}", path, e)
            return False

    async def get_default_branch(self, repo) -> str:
        """获取仓库默认分支名 / Get the default branch name"""
        try:
            return await asyncio.to_thread(lambda: repo.default_branch)
        except Exception as e:
            logger.warning(
                "Failed to get default branch for {}: {}, falling back to 'main'",
                repo.full_name, e,
            )
            return "main"

    async def _find_open_sakura_branch(self, repo) -> Optional[str]:
        """查找已有的未合并 sakura 分支 / Find existing open sakura PR branch"""

        def _sync() -> Optional[str]:
            pulls = repo.get_pulls(state="open")
            for pr in pulls:
                if pr.head.ref.startswith(f"{self.SAKURA_BRANCH_PREFIX}/"):
                    return pr.head.ref
            return None

        try:
            return await asyncio.to_thread(_sync)
        except Exception as e:
            logger.debug("Failed to search open PRs: {}", e)
            return None

    async def get_sakura_branch(self, repo) -> Optional[str]:
        """获取当前未合并的 sakura 分支名（供读取用）

        Get the open sakura branch name for reading files before merge.
        Returns None if no open sakura PR exists (files are on main).
        """
        return await self._find_open_sakura_branch(repo)


# 模块级单例访问 / Module-level singleton accessor
_github_write_service: Optional[GitHubWriteService] = None


def get_github_write_service() -> GitHubWriteService:
    """获取 GitHubWriteService 单例 / Get GitHubWriteService singleton"""
    global _github_write_service
    if _github_write_service is None:
        _github_write_service = GitHubWriteService()
    return _github_write_service
