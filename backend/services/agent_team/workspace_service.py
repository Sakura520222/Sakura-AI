"""Agent 专家团队独立工作区服务"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from backend.core.config import get_settings


_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class WorkspaceSecurityError(ValueError):
    """工作区安全校验失败。"""


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


class AgentTeamWorkspaceService:
    """管理 Agent 专家团队独立工作区。

    工作区固定为：./workplace/<GitHub用户名>/<仓库名>/
    所有路径访问必须限制在对应仓库工作区内。
    """

    def __init__(self, base_dir: str | Path | None = None):
        if base_dir is None:
            base_dir = get_settings().agent_team_workspace_root or "./workplace"
        self.base_dir = Path(base_dir).resolve()

    def get_workspace_path(self, repo_owner: str, repo_name: str) -> Path:
        """获取仓库工作区路径，不自动创建。"""
        owner = self._safe_segment(repo_owner, "repo_owner")
        name = self._safe_segment(repo_name, "repo_name")
        workspace = (self.base_dir / owner / name).resolve()
        self.ensure_within_base(workspace)
        return workspace

    def ensure_workspace(self, repo_owner: str, repo_name: str) -> Path:
        """确保仓库工作区存在。"""
        workspace = self.get_workspace_path(repo_owner, repo_name)
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

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
        """获取单个仓库工作区概要信息。"""
        workspace = self.get_workspace_path(repo_owner, repo_name)
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

        return AgentWorkspaceInfo(
            repo_owner=repo_owner,
            repo_name=repo_name,
            path=workspace,
            exists=True,
            file_count=file_count,
            total_size_bytes=total_size,
            modified_at=modified_at,
            has_git=(workspace / ".git").exists(),
        )

    def delete_workspace(self, repo_owner: str, repo_name: str) -> Path:
        """删除仓库工作区目录。"""
        workspace = self.get_workspace_path(repo_owner, repo_name)
        self.ensure_within_base(workspace)
        if workspace.exists():
            shutil.rmtree(workspace)
        return workspace

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
            raise WorkspaceSecurityError(f"路径跳出仓库工作区: {candidate}") from exc
        return candidate

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
