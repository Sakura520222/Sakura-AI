"""Lifecycle for Agent dependency virtual environments.

The two directories managed here are internal, disposable Agent paths inside a
task worktree: ``.venv/local`` and ``.venv/sandbox``. They are never treated as
user data. Backend switches remove the inactive directory before runner
admission so sandboxd never scans host-venv symlinks and a host runner never
reuses container-specific launchers.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys
from pathlib import Path

from backend.services.agent_team.execution import ExecutionError

_AGENT_VENV_BACKENDS = frozenset({"local", "sandbox"})
_AGENT_VENV_TREE_NODE_LIMIT = 100_000


class DependencyVenvLifecycleMixin:
    """Manage the two reserved dependency-venv directories."""

    async def prepare_workspace_for_execution_backend(
        self,
        workspace: str | Path,
        backend: str,
    ) -> None:
        """Prepare a worktree before the selected runner can execute.

        Preparation is unconditional because sandboxd scans the whole worktree
        even when dependency installation is disabled or no manifest exists.
        """

        normalized_backend = self._normalize_dependency_backend(backend)
        safe_workspace = self._safe_workspace_path(workspace)
        await asyncio.to_thread(
            self._prepare_workspace_for_execution_backend,
            safe_workspace,
            normalized_backend,
        )

    def _prepare_workspace_for_execution_backend(
        self,
        workspace: Path,
        backend: str,
    ) -> None:
        inactive_backend = "sandbox" if backend == "local" else "local"
        self._remove_agent_dependency_venv(workspace, inactive_backend)

        active_venv = self._agent_dependency_venv_path(workspace, backend)
        if os.path.lexists(active_venv) and not self._dependency_venv_has_launchers(
            active_venv,
            backend,
        ):
            self._remove_agent_dependency_venv(workspace, backend)

    @staticmethod
    def _normalize_dependency_backend(backend: str) -> str:
        normalized = str(backend).strip().lower()
        if normalized not in _AGENT_VENV_BACKENDS:
            raise ExecutionError("Agent 依赖环境 backend 必须为 local 或 sandbox")
        return normalized

    @staticmethod
    def _lexical_absolute(path: str | Path) -> Path:
        try:
            return Path(os.path.abspath(os.fspath(path)))
        except (OSError, TypeError, ValueError) as exc:
            raise ExecutionError(f"Agent 依赖 venv 路径无效: {path}") from exc

    @staticmethod
    def _is_reparse_or_symlink(
        node: Path,
        node_stat: os.stat_result | None = None,
    ) -> bool:
        try:
            checked = node_stat or os.lstat(node)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ExecutionError(f"Agent 依赖 venv 无法检查路径: {node}") from exc
        reparse_flag = 0x400 if os.name == "nt" else 0
        return stat.S_ISLNK(checked.st_mode) or bool(
            getattr(checked, "st_file_attributes", 0) & reparse_flag
        )

    def _safe_workspace_path(self, workspace: str | Path) -> Path:
        """Validate the lexical worktree without accepting path aliases."""

        candidate = self._lexical_absolute(workspace)
        base_dir = self._lexical_absolute(self.workspace_service.base_dir)
        try:
            candidate.relative_to(base_dir)
        except ValueError as exc:
            raise ExecutionError(
                f"Agent 工作区不在 Agent workspace root 内: {candidate}"
            ) from exc
        try:
            if candidate.resolve(strict=True) != candidate:
                raise ExecutionError("Agent 工作区路径不得包含 symlink/reparse")
        except OSError as exc:
            raise ExecutionError(f"Agent 工作区无法检查: {candidate}") from exc
        if not candidate.is_dir():
            raise ExecutionError(f"Agent 工作区不是目录: {candidate}")
        return candidate

    def _agent_dependency_venv_path(
        self,
        workspace: Path,
        backend: str,
    ) -> Path:
        """Return one exact reserved path without resolving its final node."""

        normalized_backend = self._normalize_dependency_backend(backend)
        safe_workspace = self._safe_workspace_path(workspace)
        venv_root = safe_workspace / ".venv"
        if os.path.lexists(venv_root):
            try:
                root_stat = os.lstat(venv_root)
            except OSError as exc:
                raise ExecutionError("Agent 依赖 venv 根目录无法检查") from exc
            if self._is_reparse_or_symlink(venv_root, root_stat):
                raise ExecutionError("Agent 依赖 venv 根目录不得为 symlink/reparse")
            if not stat.S_ISDIR(root_stat.st_mode):
                raise ExecutionError("Agent 依赖 venv 根路径不是目录")
        return venv_root / normalized_backend

    @staticmethod
    def _dependency_venv_layout(backend: str) -> tuple[str, str, str]:
        if backend == "sandbox":
            return "bin", "python", "pip"
        if os.name == "nt":
            return "Scripts", "python.exe", "pip.exe"
        return "bin", "python", "pip"

    def _dependency_venv_has_launchers(self, venv_dir: Path, backend: str) -> bool:
        normalized_backend = self._normalize_dependency_backend(backend)
        try:
            venv_stat = os.lstat(venv_dir)
            if not stat.S_ISDIR(venv_stat.st_mode) or self._is_reparse_or_symlink(
                venv_dir,
                venv_stat,
            ):
                return False
            scripts_name, python_name, pip_name = self._dependency_venv_layout(
                normalized_backend
            )
            scripts = venv_dir / scripts_name
            scripts_stat = os.lstat(scripts)
            if not stat.S_ISDIR(scripts_stat.st_mode) or self._is_reparse_or_symlink(
                scripts,
                scripts_stat,
            ):
                return False
            launcher = scripts / python_name
            pip_launcher = scripts / pip_name
            launcher_stat = os.lstat(launcher)
            pip_stat = os.lstat(pip_launcher)
            if not stat.S_ISREG(pip_stat.st_mode) or self._is_reparse_or_symlink(
                pip_launcher,
                pip_stat,
            ):
                return False
            if normalized_backend == "sandbox":
                config = venv_dir / "pyvenv.cfg"
                config_stat = os.lstat(config)
                if (
                    not stat.S_ISREG(launcher_stat.st_mode)
                    or self._is_reparse_or_symlink(launcher, launcher_stat)
                    or not stat.S_ISREG(config_stat.st_mode)
                    or self._is_reparse_or_symlink(config, config_stat)
                ):
                    return False
                return self._dependency_venv_tree_has_no_links(venv_dir)

            # A normal POSIX host venv uses bin/python -> sys.executable.
            # Treat any other external target as an invalid disposable venv so
            # admission rebuilds it instead of failing later during execution.
            if stat.S_ISLNK(launcher_stat.st_mode):
                try:
                    return launcher.resolve(strict=True) == Path(
                        sys.executable
                    ).resolve(strict=True)
                except OSError:
                    return False
            if os.name == "nt" and self._is_reparse_or_symlink(
                launcher,
                launcher_stat,
            ):
                return False
            return stat.S_ISREG(launcher_stat.st_mode) or stat.S_ISLNK(
                launcher_stat.st_mode
            )
        except FileNotFoundError, OSError:
            return False

    def _dependency_venv_tree_has_no_links(self, venv_dir: Path) -> bool:
        pending = [venv_dir]
        visited = 0
        while pending:
            current = pending.pop()
            visited += 1
            if visited > _AGENT_VENV_TREE_NODE_LIMIT:
                raise ExecutionError("Agent sandbox 依赖 venv 节点数量超过安全上限")
            try:
                node_stat = os.lstat(current)
            except OSError:
                return False
            if self._is_reparse_or_symlink(current, node_stat):
                return False
            if stat.S_ISDIR(node_stat.st_mode):
                try:
                    pending.extend(Path(entry.path) for entry in os.scandir(current))
                except OSError:
                    return False
            elif not stat.S_ISREG(node_stat.st_mode):
                return False
        return True

    def _create_dependency_venv_directory(
        self,
        workspace: Path,
        backend: str,
    ) -> Path:
        """Create an empty reserved directory for one bootstrap attempt."""

        normalized_backend = self._normalize_dependency_backend(backend)
        venv_dir = self._agent_dependency_venv_path(workspace, normalized_backend)
        venv_root = venv_dir.parent
        try:
            venv_root.mkdir(exist_ok=True)
            venv_dir.mkdir(exist_ok=True)
            if normalized_backend == "sandbox":
                # Some Python builds otherwise create lib64 -> lib even with
                # --copies, which sandboxd correctly refuses to hand off.
                (venv_dir / "lib64").mkdir(exist_ok=True)
        except OSError as exc:
            raise ExecutionError(f"Agent 依赖 venv 目录无法创建: {venv_dir}") from exc
        checked = self._agent_dependency_venv_path(workspace, normalized_backend)
        try:
            checked_stat = os.lstat(checked)
        except OSError as exc:
            raise ExecutionError("Agent 依赖 venv 目录无法检查") from exc
        if not stat.S_ISDIR(checked_stat.st_mode) or self._is_reparse_or_symlink(
            checked,
            checked_stat,
        ):
            raise ExecutionError("Agent 依赖 venv backend 路径不是普通目录")
        return checked

    def _remove_agent_dependency_venv(
        self,
        workspace: Path,
        backend: str,
    ) -> None:
        """Remove one exact disposable venv tree without following links."""

        normalized_backend = self._normalize_dependency_backend(backend)
        venv_dir = self._agent_dependency_venv_path(workspace, normalized_backend)
        if not os.path.lexists(venv_dir):
            return
        try:
            if os.name == "nt":
                self._remove_agent_dependency_venv_windows(venv_dir)
            else:
                self._remove_agent_dependency_venv_posix(venv_dir)
        except ExecutionError:
            raise
        except OSError as exc:
            raise ExecutionError(f"Agent 依赖 venv 无法清理: {venv_dir}") from exc

    def _remove_agent_dependency_venv_posix(self, venv_dir: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow or not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
            raise ExecutionError("当前平台缺少安全的 Agent venv 清理能力")
        parent_fd = os.open(venv_dir.parent, flags | nofollow)
        try:
            try:
                node_stat = os.stat(
                    venv_dir.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            if stat.S_ISLNK(node_stat.st_mode) or not stat.S_ISDIR(node_stat.st_mode):
                os.unlink(venv_dir.name, dir_fd=parent_fd)
                return
            shutil.rmtree(venv_dir.name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)

    def _remove_agent_dependency_venv_windows(self, venv_dir: Path) -> None:
        node_stat = os.lstat(venv_dir)
        if self._is_reparse_or_symlink(venv_dir, node_stat):
            if stat.S_ISDIR(node_stat.st_mode):
                os.rmdir(venv_dir)
            else:
                os.unlink(venv_dir)
            return
        if not stat.S_ISDIR(node_stat.st_mode):
            os.unlink(venv_dir)
            return
        shutil.rmtree(venv_dir)
