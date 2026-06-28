"""Agent 专家团队独立工作区服务"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from backend.core.config import get_settings


_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_WORKTREE_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class WorkspaceSecurityError(ValueError):
    """工作区安全校验失败。"""


@dataclass(frozen=True)
class WorktreeInfo:
    """单个 worktree 目录信息。"""

    dir_name: str
    path: Path
    task_id: int | None
    branch_slug: str
    exists: bool
    file_count: int
    total_size_bytes: int
    modified_at: float | None
    has_git: bool


@dataclass(frozen=True)
class AgentWorkspaceInfo:
    """Agent 仓库工作区概要信息。"""

    repo_owner: str
    repo_name: str
    path: Path
    exists: bool
    file_count: int
    total_size_bytes: int
    modified_at: float | None
    has_git: bool
    worktree_count: int = 0


class AgentTeamWorkspaceService:
    """管理 Agent 专家团队独立工作区。

    逻辑仓库工作区固定为：./workplace/<GitHub用户名>/<仓库名>/。
    新任务使用仓库目录下的独立 worktree：
    ./workplace/<GitHub用户名>/<仓库名>/worktrees/<task_id>-<branch_slug>/。
    所有路径访问必须限制在 Agent 工作区根目录内。
    """

    BASE_DIR_NAME = "base"
    WORKTREES_DIR_NAME = "worktrees"

    def __init__(self, base_dir: str | Path | None = None):
        if base_dir is None:
            base_dir = get_settings().agent_team_workspace_root or "./workplace"
        self.base_dir = Path(base_dir).resolve()

    def get_workspace_path(self, repo_owner: str, repo_name: str) -> Path:
        """获取逻辑仓库工作区路径，不自动创建。"""
        return self.get_repo_root_path(repo_owner, repo_name)

    def get_repo_root_path(self, repo_owner: str, repo_name: str) -> Path:
        """获取逻辑仓库工作区根目录，不自动创建。"""
        owner = self._safe_segment(repo_owner, "repo_owner")
        name = self._safe_segment(repo_name, "repo_name")
        repo_root = (self.base_dir / owner / name).resolve()
        self.ensure_within_base(repo_root)
        return repo_root

    def ensure_workspace(self, repo_owner: str, repo_name: str) -> Path:
        """确保逻辑仓库工作区存在。"""
        return self.ensure_repo_root(repo_owner, repo_name)

    def ensure_repo_root(self, repo_owner: str, repo_name: str) -> Path:
        """确保逻辑仓库工作区根目录存在。"""
        repo_root = self.get_repo_root_path(repo_owner, repo_name)
        repo_root.mkdir(parents=True, exist_ok=True)
        return repo_root

    def get_base_workspace_path(self, repo_owner: str, repo_name: str) -> Path:
        """获取仓库 base checkout/cache 路径，不自动创建。"""
        base_workspace = (
            self.get_repo_root_path(repo_owner, repo_name) / self.BASE_DIR_NAME
        ).resolve()
        self.ensure_within_base(base_workspace)
        return base_workspace

    def ensure_base_workspace(self, repo_owner: str, repo_name: str) -> Path:
        """确保仓库 base checkout/cache 路径存在。"""
        base_workspace = self.get_base_workspace_path(repo_owner, repo_name)
        base_workspace.mkdir(parents=True, exist_ok=True)
        return base_workspace

    def get_worktrees_root_path(self, repo_owner: str, repo_name: str) -> Path:
        """获取仓库 task worktrees 根目录，不自动创建。"""
        worktrees_root = (
            self.get_repo_root_path(repo_owner, repo_name) / self.WORKTREES_DIR_NAME
        ).resolve()
        self.ensure_within_base(worktrees_root)
        return worktrees_root

    def ensure_worktrees_root(self, repo_owner: str, repo_name: str) -> Path:
        """确保仓库 task worktrees 根目录存在。"""
        worktrees_root = self.get_worktrees_root_path(repo_owner, repo_name)
        worktrees_root.mkdir(parents=True, exist_ok=True)
        return worktrees_root

    def get_task_worktree_path(
        self,
        repo_owner: str,
        repo_name: str,
        task_id: int,
        branch_name: str,
    ) -> Path:
        """获取单个 Agent task 的 worktree 路径，不自动创建。"""
        if task_id <= 0:
            raise WorkspaceSecurityError("task_id 必须为正整数")
        branch_slug = self.make_branch_slug(branch_name)
        worktree = (
            self.get_worktrees_root_path(repo_owner, repo_name)
            / f"{task_id}-{branch_slug}"
        ).resolve()
        # 双重校验：1) 确保在 Agent 工作区根目录内；2) 确保在对应仓库目录内
        # 第二重防止 base_dir 下不同仓库之间的路径穿越
        self.ensure_within_base(worktree)
        repo_root = self.get_repo_root_path(repo_owner, repo_name)
        try:
            worktree.relative_to(repo_root)
        except ValueError as exc:
            raise WorkspaceSecurityError(
                f"worktree 不在仓库工作区内: {worktree}"
            ) from exc
        return worktree

    def is_path_inside_repo(
        self, repo_owner: str, repo_name: str, path: str | Path
    ) -> bool:
        """判断路径是否属于指定逻辑仓库工作区。"""
        repo_root = self.get_repo_root_path(repo_owner, repo_name)
        candidate = self.ensure_within_base(path)
        try:
            candidate.relative_to(repo_root)
        except ValueError:
            return False
        return True

    def list_workspaces(self) -> list[AgentWorkspaceInfo]:
        """列出工作区根目录下的仓库工作区。"""
        if not self.base_dir.exists():
            return []

        workspaces = []
        for owner_dir in self.base_dir.iterdir():
            if not owner_dir.is_dir() or not self._is_safe_existing_segment(
                owner_dir.name
            ):
                continue
            for repo_dir in owner_dir.iterdir():
                if not repo_dir.is_dir() or not self._is_safe_existing_segment(
                    repo_dir.name
                ):
                    continue
                workspaces.append(
                    self.get_workspace_info(owner_dir.name, repo_dir.name)
                )
        return sorted(workspaces, key=lambda item: item.modified_at or 0, reverse=True)

    def get_workspace_info(self, repo_owner: str, repo_name: str) -> AgentWorkspaceInfo:
        """获取单个逻辑仓库工作区概要信息。"""
        workspace = self.get_repo_root_path(repo_owner, repo_name)
        if not workspace.exists():
            return AgentWorkspaceInfo(
                repo_owner=repo_owner,
                repo_name=repo_name,
                path=workspace,
                exists=False,
                file_count=0,
                total_size_bytes=0,
                modified_at=None,
                has_git=False,
                worktree_count=0,
            )

        file_count = 0
        total_size = 0
        modified_at = workspace.stat().st_mtime
        for item in workspace.rglob("*"):
            try:
                stat = item.stat()
            except OSError:
                continue
            modified_at = max(modified_at, stat.st_mtime)
            if item.is_file():
                file_count += 1
                total_size += stat.st_size

        worktrees_root = workspace / self.WORKTREES_DIR_NAME
        worktree_count = 0
        if worktrees_root.exists():
            worktree_count = sum(
                1 for item in worktrees_root.iterdir() if item.is_dir()
            )

        has_git = (workspace / ".git").exists() or (
            workspace / self.BASE_DIR_NAME / ".git"
        ).exists()
        return AgentWorkspaceInfo(
            repo_owner=repo_owner,
            repo_name=repo_name,
            path=workspace,
            exists=True,
            file_count=file_count,
            total_size_bytes=total_size,
            modified_at=modified_at,
            has_git=has_git,
            worktree_count=worktree_count,
        )

    def delete_workspace(self, repo_owner: str, repo_name: str) -> Path:
        """删除逻辑仓库工作区目录。"""
        workspace = self.get_repo_root_path(repo_owner, repo_name)
        self.ensure_within_base(workspace)
        if workspace.exists():
            shutil.rmtree(workspace)
        return workspace

    # ── Worktree 管理 ──────────────────────────────────────

    _WT_DIR_RE = re.compile(r"^(\d+)-(.+)$")

    def list_worktrees(self, repo_owner: str, repo_name: str) -> list[WorktreeInfo]:
        """列出仓库下所有 worktree 的详细信息。"""
        worktrees_root = self.get_worktrees_root_path(repo_owner, repo_name)
        if not worktrees_root.exists():
            return []

        result: list[WorktreeInfo] = []
        for item in worktrees_root.iterdir():
            if not item.is_dir():
                continue
            info = self._build_worktree_info(item)
            if info is not None:
                result.append(info)
        return sorted(result, key=lambda w: w.task_id or 0)

    def get_worktree(
        self, repo_owner: str, repo_name: str, dir_name: str
    ) -> WorktreeInfo | None:
        """获取单个 worktree 信息（按目录名）。"""
        self._validate_worktree_dir_name(dir_name)
        worktrees_root = self.get_worktrees_root_path(repo_owner, repo_name)
        target = (worktrees_root / dir_name).resolve()
        self.ensure_within_base(target)
        repo_root = self.get_repo_root_path(repo_owner, repo_name)
        try:
            target.relative_to(repo_root)
        except ValueError as exc:
            raise WorkspaceSecurityError(
                f"worktree 不在仓库工作区内: {target}"
            ) from exc
        if not target.exists():
            return None
        return self._build_worktree_info(target)

    def delete_worktree(self, repo_owner: str, repo_name: str, dir_name: str) -> Path:
        """删除单个 worktree 目录。"""
        self._validate_worktree_dir_name(dir_name)
        worktrees_root = self.get_worktrees_root_path(repo_owner, repo_name)
        target = (worktrees_root / dir_name).resolve()
        self.ensure_within_base(target)
        repo_root = self.get_repo_root_path(repo_owner, repo_name)
        try:
            target.relative_to(repo_root)
        except ValueError as exc:
            raise WorkspaceSecurityError(
                f"worktree 不在仓库工作区内: {target}"
            ) from exc
        if target.exists():
            shutil.rmtree(target)
        return target

    def _build_worktree_info(self, worktree_dir: Path) -> WorktreeInfo | None:
        """从 worktree 目录构建 WorktreeInfo。"""
        match = self._WT_DIR_RE.match(worktree_dir.name)
        if not match:
            return None
        task_id = int(match.group(1))
        branch_slug = match.group(2)

        file_count = 0
        total_size = 0
        modified_at: float | None = None
        try:
            modified_at = worktree_dir.stat().st_mtime
        except OSError:
            pass
        # 排除 .git 目录（worktree 的 .git 是指向主仓库的指针文件/目录），
        # 减少不必要的文件系统遍历
        for item in worktree_dir.rglob("*"):
            try:
                if ".git" in item.parts:
                    continue
                stat = item.stat()
            except OSError:
                continue
            if modified_at is not None:
                modified_at = max(modified_at, stat.st_mtime)
            if item.is_file():
                file_count += 1
                total_size += stat.st_size

        return WorktreeInfo(
            dir_name=worktree_dir.name,
            path=worktree_dir,
            task_id=task_id,
            branch_slug=branch_slug,
            exists=True,
            file_count=file_count,
            total_size_bytes=total_size,
            modified_at=modified_at,
            has_git=(worktree_dir / ".git").exists(),
        )

    @staticmethod
    def _validate_worktree_dir_name(dir_name: str) -> None:
        """校验 worktree 目录名格式。"""
        if not dir_name or not _SAFE_SEGMENT_RE.match(dir_name):
            raise WorkspaceSecurityError(f"无效的 worktree 目录名: {dir_name}")

    def ensure_within_base(self, path: str | Path) -> Path:
        """确保路径位于 Agent 工作区根目录内。"""
        resolved = Path(path).resolve()
        try:
            resolved.relative_to(self.base_dir)
        except ValueError as exc:
            raise WorkspaceSecurityError(
                f"路径不在 Agent 工作区内: {resolved}"
            ) from exc
        return resolved

    def resolve_inside_workspace(
        self, workspace: str | Path, relative_path: str | Path = "."
    ) -> Path:
        """解析工作区内相对路径并进行越界保护。"""
        workspace_path = Path(workspace).resolve()
        self.ensure_within_base(workspace_path)
        if Path(relative_path).is_absolute():
            candidate = Path(relative_path).resolve()
        else:
            candidate = (workspace_path / relative_path).resolve()
        try:
            candidate.relative_to(workspace_path)
        except ValueError as exc:
            raise WorkspaceSecurityError(
                f"路径跳出仓库工作区: candidate={candidate}, "
                f"workspace={workspace_path}, base_dir={self.base_dir}"
            ) from exc
        return candidate

    def make_branch_slug(self, branch_name: str) -> str:
        """将 Git 分支名转换为安全目录名。"""
        slug = _WORKTREE_SLUG_RE.sub("-", (branch_name or "").strip()).strip("-.")
        return slug or "branch"

    def _safe_segment(self, value: str, field_name: str) -> str:
        """校验路径片段，避免路径穿越与特殊路径。"""
        value = (value or "").strip()
        if not value or value in {".", ".."}:
            raise WorkspaceSecurityError(f"{field_name} 不能为空或特殊路径")
        if "/" in value or "\\" in value or ":" in value:
            raise WorkspaceSecurityError(f"{field_name} 不能包含路径分隔符")
        if not _SAFE_SEGMENT_RE.match(value):
            raise WorkspaceSecurityError(f"{field_name} 包含非法字符: {value}")
        return value

    def _is_safe_existing_segment(self, value: str) -> bool:
        """判断已存在目录名是否为安全路径片段。"""
        return bool(
            value and value not in {".", ".."} and _SAFE_SEGMENT_RE.match(value)
        )
