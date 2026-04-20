"""GitHub 文件写入服务 / GitHub file write service

使用 PyGithub Contents API 将文件提交回仓库，
用于 .sakura/ 记忆系统等需要写入文件到仓库的场景。

Uses PyGithub's Contents API to commit files back to repositories,
for scenarios like the .sakura/ memory system that need to write files to repos.
"""

import asyncio
import base64
from typing import Optional

from github import InputGitAuthor
from github.GithubException import UnknownObjectException
from loguru import logger


class GitHubWriteService:
    """GitHub 文件写入服务（单例模式）/ GitHub file write service (singleton)"""

    DEFAULT_AUTHOR_NAME = "Sakura AI Reviewer"
    DEFAULT_AUTHOR_EMAIL = "Sakura520222@outlook.com"

    _instance = None
    _initialized = False

    def __new__(cls):
        """确保只有一个实例 / Ensure singleton"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """初始化（只执行一次）/ Initialize (only once)"""
        if not self._initialized:
            self.__class__._initialized = True
            logger.info("GitHubWriteService singleton initialized")

    async def commit_files(
        self,
        repo,
        files: dict,
        message: str,
        branch: Optional[str] = None,
    ) -> str:
        """通过 Contents API 提交多个文件到仓库

        使用 create_file / update_file 逐个提交文件。
        Each file gets its own commit via the Contents API.

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
        last_sha = None

        for path, content in files.items():
            last_sha = await self._commit_single_file(
                repo, path, content, message, branch, author
            )

        logger.info(
            f"Committed {len(files)} file(s) to {repo.full_name}:{branch} "
            f"-> {last_sha[:8] if last_sha else 'unknown'}"
        )
        return last_sha or ""

    async def _commit_single_file(
        self,
        repo,
        path: str,
        content: str,
        message: str,
        branch: str,
        author: InputGitAuthor,
    ) -> str:
        """提交单个文件（自动判断创建或更新）/ Commit a single file (auto create/update)"""

        def _commit_sync() -> str:
            try:
                # 尝试获取已有文件（用于更新）/ Try to get existing file for update
                existing = repo.get_contents(path, ref=branch)
                if isinstance(existing, list):
                    raise ValueError(f"Path {path} is a directory")
                # 更新已有文件 / Update existing file
                logger.info(f"Updating existing file: {path}")
                result = repo.update_file(
                    path=path,
                    message=message,
                    content=content,
                    sha=existing.sha,
                    branch=branch,
                    author=author,
                )
            except UnknownObjectException:
                # 文件不存在，创建新文件 / File doesn't exist, create new
                logger.info(f"Creating new file: {path}")
                result = repo.create_file(
                    path=path,
                    message=message,
                    content=content,
                    branch=branch,
                    author=author,
                )

            logger.info(f"create_file/update_file returned type={type(result).__name__}")
            if isinstance(result, dict):
                logger.info(f"result keys={list(result.keys())}")

            # PyGithub create_file/update_file 返回 dict
            # Return format: {"content": ContentFile, "commit": Commit}
            if not isinstance(result, dict):
                raise RuntimeError(
                    f"GitHub API returned unexpected type {type(result).__name__} "
                    f"for {path}. This usually means the GitHub App lacks "
                    f"'contents:write' permission. Result: {result!r}"
                )

            commit_obj = result.get("commit")
            if commit_obj is None:
                # Check if the response contains an error message instead
                error_msg = result.get("message", "unknown error")
                raise RuntimeError(
                    f"GitHub API returned no commit for {path}. "
                    f"Response: message={error_msg}, keys={list(result.keys())}. "
                    f"Ensure the GitHub App has 'contents:write' permission."
                )
            return commit_obj.sha

        try:
            sha = await asyncio.to_thread(_commit_sync)
            logger.info(f"Committed {path} -> {sha[:8]}")
            return sha
        except KeyError as e:
            import traceback
            logger.error(
                f"KeyError {e} while committing {path} to {repo.full_name}:{branch}. "
                f"Full traceback:\n{traceback.format_exc()}"
            )
            raise RuntimeError(
                f"GitHub API returned unexpected response for {path} (KeyError: {e}). "
                f"Most likely cause: GitHub App does not have 'contents:write' permission."
            ) from e
        except Exception as e:
            import traceback
            logger.error(
                f"Failed to commit {path} to {repo.full_name}:{branch}: "
                f"[{type(e).__name__}] {e}\n{traceback.format_exc()}"
            )
            raise

    async def read_file(self, repo, path: str, ref: Optional[str] = None) -> Optional[str]:
        """从仓库读取文件内容 / Read file content from repository"""

        def _read_sync() -> Optional[str]:
            kwargs = {}
            if ref is not None:
                kwargs["ref"] = ref
            content_file = repo.get_contents(path, **kwargs)
            if isinstance(content_file, list):
                logger.warning(f"Path {path} is a directory, not a file")
                return None
            decoded = base64.b64decode(content_file.content)
            return decoded.decode("utf-8")

        try:
            return await asyncio.to_thread(_read_sync)
        except UnknownObjectException:
            logger.debug(f"File not found: {path}")
            return None
        except Exception as e:
            logger.error(f"Failed to read file {path}: {e}", exc_info=True)
            return None

    async def file_exists(self, repo, path: str, ref: Optional[str] = None) -> bool:
        """检查文件是否存在于仓库中 / Check if a file exists in the repository"""

        def _exists_sync() -> bool:
            kwargs = {}
            if ref is not None:
                kwargs["ref"] = ref
            repo.get_contents(path, **kwargs)
            return True

        try:
            return await asyncio.to_thread(_exists_sync)
        except UnknownObjectException:
            return False
        except Exception as e:
            logger.error(f"Failed to check file existence {path}: {e}", exc_info=True)
            return False

    async def get_default_branch(self, repo) -> str:
        """获取仓库默认分支名 / Get the default branch name"""
        try:
            return await asyncio.to_thread(lambda: repo.default_branch)
        except Exception as e:
            logger.warning(
                f"Failed to get default branch for {repo.full_name}: {e}, "
                f"falling back to 'main'"
            )
            return "main"


# 模块级单例访问 / Module-level singleton accessor
_github_write_service: Optional[GitHubWriteService] = None


def get_github_write_service() -> GitHubWriteService:
    """获取 GitHubWriteService 单例 / Get GitHubWriteService singleton"""
    global _github_write_service
    if _github_write_service is None:
        _github_write_service = GitHubWriteService()
    return _github_write_service
