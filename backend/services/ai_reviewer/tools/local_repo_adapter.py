"""本地仓库适配器

基于已 clone 到本地的仓库目录，提供与 PyGithub Repository 兼容的接口，
使扫描工具（FileToolHandler, SearchFilesToolHandler 等）在无法获取
GitHub API client 时仍能正常工作。
"""

import os
import subprocess
from typing import Any, List, Optional


class _LocalContentFile:
    """模拟 PyGithub ContentFile，基于本地文件系统"""

    def __init__(self, full_path: str, repo_relative_path: str):
        self.path = repo_relative_path.replace("\\", "/")
        self.name = os.path.basename(full_path)
        self.type = "file" if os.path.isfile(full_path) else "dir"
        self.size = os.path.getsize(full_path) if os.path.isfile(full_path) else 0
        self._full_path = full_path

    @property
    def decoded_content(self) -> Optional[bytes]:
        if self.type != "file":
            return None
        try:
            with open(self._full_path, "rb") as f:
                return f.read()
        except OSError:
            return None


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
    except Exception:
        pass
    return "main"


class LocalRepoAdapter:
    """基于本地 clone 的仓库适配器，提供 PyGithub Repository 兼容接口"""

    def __init__(self, repo_path: str, repo_name: str):
        self._repo_path = repo_path
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
        """
        clean_path = path.lstrip("/").replace("\\", "/")
        full_path = os.path.join(self._repo_path, clean_path)

        if not os.path.exists(full_path):
            raise FileNotFoundError(f"文件不存在: {path}")

        if os.path.isfile(full_path):
            return _LocalContentFile(full_path, clean_path)

        if os.path.isdir(full_path):
            items = []
            try:
                for entry in sorted(os.listdir(full_path)):
                    entry_path = os.path.join(full_path, entry)
                    rel_path = f"{clean_path}/{entry}" if clean_path else entry
                    items.append(_LocalContentFile(entry_path, rel_path))
            except OSError:
                pass
            return items

        raise FileNotFoundError(f"路径类型未知: {path}")

    def get_git_tree(self, sha: str = None, recursive: bool = False) -> _LocalGitTree:
        """基于本地文件系统构建文件树

        模拟 PyGithub Repository.get_git_tree() 的行为，
        用于跨文件搜索工具的文件遍历。
        """
        entries: List[_LocalGitTreeEntry] = []
        for root, dirs, files in os.walk(self._repo_path):
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
