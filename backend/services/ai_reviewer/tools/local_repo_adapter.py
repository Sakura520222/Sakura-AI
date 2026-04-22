"""本地仓库适配器

基于已 clone 到本地的仓库目录，提供与 PyGithub Repository 兼容的接口，
使扫描工具（FileToolHandler, SearchFilesToolHandler 等）在无法获取
GitHub API client 时仍能正常工作。
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Any, List, Optional

from loguru import logger

# 合法的 git ref 字符：分支名、tag、SHA、相对引用等
_REF_PATTERN = re.compile(r"^[a-zA-Z0-9_./\-~^:@{}]+$")


def _is_safe_path(resolved_path: Path, repo_root: Path) -> bool:
    """检查解析后的路径是否在仓库根目录内"""
    return resolved_path == repo_root or resolved_path.is_relative_to(repo_root)


class _LocalContentFile:
    """模拟 PyGithub ContentFile，基于本地文件系统"""

    def __init__(self, full_path: str, repo_relative_path: str):
        self.path = repo_relative_path.replace("\\", "/")
        self.name = os.path.basename(full_path)
        self.type = "file" if os.path.isfile(full_path) else "dir"
        self.size = os.path.getsize(full_path) if os.path.isfile(full_path) else 0
        self.decoded_content: Optional[bytes] = (
            self._read_file(full_path) if self.type == "file" else None
        )

    @staticmethod
    def _read_file(full_path: str) -> Optional[bytes]:
        try:
            with open(full_path, "rb") as f:
                return f.read()
        except OSError:
            return None


class _LocalGitContentFile:
    """基于 git show 输出的 ContentFile，用于指定 ref 时读取文件"""

    def __init__(self, repo_relative_path: str, content: bytes):
        self.path = repo_relative_path.replace("\\", "/")
        self.name = os.path.basename(repo_relative_path)
        self.type = "file"
        self.size = len(content)
        self.decoded_content: Optional[bytes] = content


class _LocalGitTreeEntry:
    """模拟 PyGithub GitTreeElement"""

    def __init__(self, path: str, entry_type: str):
        self.path = path
        self.type = entry_type  # "blob" or "tree"


class _LocalGitTree:
    """模拟 PyGithub GitTree"""

    def __init__(self, entries: List[_LocalGitTreeEntry]):
        self.tree = entries


def _detect_default_branch(repo_path: str) -> str:
    """从本地 git 仓库检测默认分支名"""
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception as e:
        logger.warning(f"检测默认分支失败，回退到 'main': {e}")
    return "main"


class LocalRepoAdapter:
    """基于本地 clone 的仓库适配器，提供 PyGithub Repository 兼容接口

    注意：不支持 GitHub Search API，调用方应使用 isinstance(repo, Repository)
    判断是否支持 API 搜索，LocalRepoAdapter 实例不支持。
    """

    def __init__(self, repo_path: str, repo_name: str):
        self._repo_path = repo_path
        self._repo_root = Path(repo_path).resolve()
        parts = repo_name.split("/", 1)
        self._owner = parts[0] if len(parts) > 1 else ""
        self._name = parts[1] if len(parts) > 1 else repo_name
        self.default_branch = _detect_default_branch(repo_path)
        self.description = None
        self.language = None
        self.private = False
        self.fork = False

    @property
    def owner(self):
        return type("Owner", (), {"login": self._owner})()

    @property
    def name(self):
        return self._name

    @property
    def full_name(self):
        return f"{self._owner}/{self._name}"

    def get_contents(self, path: str, ref: str = None) -> Any:
        """从本地文件系统读取文件或列出目录

        模拟 PyGithub Repository.get_contents() 的行为：
        - 文件路径 → 返回单个 ContentFile
        - 目录路径 → 返回 ContentFile 列表
        - 不存在 → 抛出异常

        当指定 ref 时，通过 git show 读取对应引用的文件内容，
        确保 detached HEAD 等场景下内容与预期分支一致。
        """
        clean_path = path.lstrip("/").replace("\\", "/")

        # ref 指定且为文件路径：通过 git show 读取指定引用的内容
        if ref:
            if not clean_path:
                raise ValueError("指定 ref 时必须同时指定文件路径")
            if not _REF_PATTERN.match(ref):
                raise ValueError(f"无效的 ref 格式: {ref}")
            result = subprocess.run(
                ["git", "show", f"{ref}:{clean_path}"],
                cwd=self._repo_path,
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                return _LocalGitContentFile(clean_path, result.stdout)
            logger.warning(
                f"git show {ref}:{clean_path} 失败 (exit={result.returncode})，"
                f"fallback 到工作树读取。返回的内容可能与 {ref} 引用不一致！"
            )

        full_path = (self._repo_root / clean_path).resolve()
        if not _is_safe_path(full_path, self._repo_root):
            raise PermissionError(f"路径超出仓库范围: {path}")

        if not full_path.exists():
            raise FileNotFoundError(f"文件不存在: {path}")

        # 统一拦截符号链接，防止指向仓库外的文件
        if full_path.is_symlink():
            raise PermissionError(f"不允许通过符号链接访问: {path}")

        if full_path.is_file():
            return _LocalContentFile(str(full_path), clean_path)

        if full_path.is_dir():
            items = []
            try:
                for entry in sorted(os.listdir(full_path)):
                    entry_path = (full_path / entry).resolve()
                    # 跳过符号链接和逃逸路径
                    if (full_path / entry).is_symlink():
                        continue
                    if not _is_safe_path(entry_path, self._repo_root):
                        logger.warning(f"跳过逃逸路径: {clean_path}/{entry}")
                        continue
                    rel_path = f"{clean_path}/{entry}" if clean_path else entry
                    items.append(_LocalContentFile(str(entry_path), rel_path))
            except OSError as e:
                logger.warning(f"遍历目录 {clean_path} 失败: {e}")
            return items

        raise FileNotFoundError(f"路径类型未知: {path}")

    def get_git_tree(self, sha: str = None, recursive: bool = False) -> _LocalGitTree:
        """基于本地文件系统构建文件树

        模拟 PyGithub Repository.get_git_tree() 的行为，
        用于跨文件搜索工具的文件遍历。

        Note: sha 参数被忽略，始终遍历当前工作树。
        对于 shallow clone 场景（扫描默认使用 --depth 1），
        工作树即为目标分支完整内容，因此该限制可接受。
        """
        if sha is not None:
            logger.warning(
                f"get_git_tree: sha 参数 '{sha}' 被忽略，始终返回当前工作树内容"
            )

        entries: List[_LocalGitTreeEntry] = []
        for root, dirs, files in os.walk(self._repo_path, followlinks=False):
            # 安全校验：确保当前遍历目录在仓库范围内
            resolved_root = Path(root).resolve()
            if not _is_safe_path(resolved_root, self._repo_root):
                dirs.clear()
                continue

            # 跳过 .git 目录
            dirs[:] = [d for d in dirs if d != ".git"]

            rel_root = os.path.relpath(root, self._repo_path).replace("\\", "/")

            if recursive:
                for d in dirs:
                    rel_dir = f"{rel_root}/{d}" if rel_root != "." else d
                    entries.append(_LocalGitTreeEntry(rel_dir, "tree"))

            for f in files:
                rel_file = f"{rel_root}/{f}" if rel_root != "." else f
                entries.append(_LocalGitTreeEntry(rel_file, "blob"))

        return _LocalGitTree(entries)

    # GitToolHandler 需要的方法 - 返回空/默认值
    def get_topics(self) -> list:
        return []

    def get_languages(self) -> dict:
        return {}

    def get_branches(self) -> list:
        return []

    def get_commits(self, **kwargs) -> list:
        return []

    def get_license(self):
        raise NotImplementedError("LocalRepoAdapter does not support get_license")

    def __repr__(self):
        return f"LocalRepoAdapter({self.full_name}, path={self._repo_path})"
