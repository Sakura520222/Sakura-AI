"""GitHub 文件写入服务 / GitHub file write service

使用 PyGithub 的 Git Data API 将文件提交回仓库，
用于 .sakura/ 记忆系统等需要写入文件到仓库的场景。

Uses PyGithub's Git Data API to commit files back to repositories,
for scenarios like the .sakura/ memory system that need to write files to repos.
"""

import asyncio
import base64
import threading
from typing import Optional

from github import InputGitAuthor, InputGitTreeElement
from github.GithubException import UnknownObjectException
from loguru import logger


class GitHubWriteService:
    """GitHub 文件写入服务（单例模式）/ GitHub file write service (singleton)"""

    _instance = None
    _lock = None
    _initialized = False

    def __new__(cls):
        """确保只有一个实例 / Ensure singleton"""
        if cls._instance is None:
            cls._lock = threading.Lock()
            with cls._lock:
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
        """通过 Git Data API 提交多个文件到仓库

        Commit multiple files to a repository via the Git Data API.
        Flow: get_git_ref -> get_git_commit -> create_git_tree -> create_git_commit -> ref.edit

        Args:
            repo: PyGithub Repository 对象
            files: {文件路径: 文件内容} 的字典
            message: 提交信息
            branch: 目标分支，默认使用仓库默认分支

        Returns:
            新提交的 SHA

        Raises:
            ValueError: 没有文件需要提交
            Exception: Git Data API 调用失败
        """
        if not files:
            raise ValueError("No files to commit")

        # 确定目标分支 / Determine target branch
        if branch is None:
            branch = await self.get_default_branch(repo)

        ref_path = f"heads/{branch}"

        def _commit_sync() -> str:
            # 1. 获取分支引用 / Get branch reference
            ref = repo.get_git_ref(ref_path)
            commit_sha = ref.object.sha
            logger.debug(f"Got ref {ref_path} -> {commit_sha}")

            # 2. 获取当前提交 / Get current commit
            commit = repo.get_git_commit(commit_sha)

            # 3. 构建 tree 元素列表 / Build tree element list
            tree_elements = []
            for path, content in files.items():
                element = InputGitTreeElement(
                    path=path,
                    mode="100644",
                    type="blob",
                    content=content,
                )
                tree_elements.append(element)

            # 4. 创建新 tree / Create new tree
            new_tree = repo.create_git_tree(tree_elements, commit.tree)

            # 5. 创建新提交 / Create new commit
            author = InputGitAuthor(
                name="Sakura AI Reviewer",
                email="noreply@sakura-ai.dev",
            )
            new_commit = repo.create_git_commit(
                message=message,
                tree=new_tree,
                parents=[commit],
                author=author,
                committer=author,
            )

            # 6. 更新分支引用 / Update branch reference
            ref.edit(new_commit.sha)

            logger.info(
                f"Committed {len(files)} file(s) to {repo.full_name}:{branch} "
                f"-> {new_commit.sha[:8]}"
            )
            return new_commit.sha

        try:
            return await asyncio.to_thread(_commit_sync)
        except Exception as e:
            logger.error(
                f"Failed to commit files to {repo.full_name}:{branch}: {e}",
                exc_info=True,
            )
            raise

    async def read_file(self, repo, path: str, ref: Optional[str] = None) -> Optional[str]:
        """从仓库读取文件内容 / Read file content from repository

        Args:
            repo: PyGithub Repository 对象
            path: 文件路径
            ref: Git 引用（分支名、SHA 等），默认使用默认分支

        Returns:
            文件内容字符串，文件不存在时返回 None
        """
        def _read_sync() -> Optional[str]:
            kwargs = {}
            if ref is not None:
                kwargs["ref"] = ref
            content_file = repo.get_contents(path, **kwargs)
            # get_contents 返回 ContentFile 或列表 / Returns ContentFile or list
            if isinstance(content_file, list):
                # 路径指向目录 / Path is a directory
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
        """检查文件是否存在于仓库中 / Check if a file exists in the repository

        Args:
            repo: PyGithub Repository 对象
            path: 文件路径
            ref: Git 引用，默认使用默认分支

        Returns:
            文件是否存在
        """
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
        """获取仓库默认分支名 / Get the default branch name

        Args:
            repo: PyGithub Repository 对象

        Returns:
            默认分支名称
        """
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
