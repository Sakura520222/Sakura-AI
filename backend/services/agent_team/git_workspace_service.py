"""Agent 专家团队 Git 工作区同步服务"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from backend.core.github_app import GitHubAppClient
from backend.core.config import get_dynamic_config, get_settings
from backend.services.agent_team.shell_executor import (
    AgentTeamShellExecutor,
    ShellCommandResult,
)
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


@dataclass(frozen=True)
class GitWorkspaceInfo:
    """Git 工作区同步结果。"""

    workspace: Path
    branch_name: str
    default_branch: str
    commit_sha: str


_repo_locks: dict[str, asyncio.Lock] = {}
_repo_locks_guard = asyncio.Lock()


async def _get_repo_lock(repo_full_name: str) -> asyncio.Lock:
    """获取同仓库 clone/fetch/worktree 操作的进程内锁。"""
    async with _repo_locks_guard:
        lock = _repo_locks.get(repo_full_name)
        if lock is None:
            lock = asyncio.Lock()
            _repo_locks[repo_full_name] = lock
        return lock


class AgentTeamGitWorkspaceService:
    """负责 clone/fetch/checkout Agent 独立工作区。"""

    BRANCH_PREFIX = "sakura-agent"

    def __init__(
        self,
        github_app: GitHubAppClient | None = None,
        workspace_service: AgentTeamWorkspaceService | None = None,
    ):
        self.github_app = github_app or GitHubAppClient()
        self.workspace_service = workspace_service or AgentTeamWorkspaceService()

    async def prepare_workspace(
        self,
        repo_owner: str,
        repo_name: str,
        source_issue_number: int | None = None,
        source_id: int | None = None,
        base_branch: str | None = None,
        task_id: int | None = None,
        source_type: str | None = None,
    ) -> GitWorkspaceInfo:
        """准备仓库 base checkout，并为当前 task 创建独立 worktree。"""
        if task_id is None or task_id <= 0:
            raise RuntimeError("准备 Agent 工作区需要有效的 task_id")

        repo_full_name = f"{repo_owner}/{repo_name}"
        default_branch, clone_url = await self._get_repo_info(
            repo_owner, repo_name, repo_full_name
        )
        resolved_branch = base_branch or default_branch
        branch_name = self.make_branch_name(
            task_id, source_issue_number, source_id, source_type
        )
        base_workspace = self.workspace_service.ensure_base_workspace(
            repo_owner, repo_name
        )
        worktree = self.workspace_service.get_task_worktree_path(
            repo_owner, repo_name, task_id, branch_name
        )

        repo_lock = await _get_repo_lock(repo_full_name)
        async with repo_lock:
            base_executor = AgentTeamShellExecutor(base_workspace, self.workspace_service)
            if not (base_workspace / ".git").exists():
                await self._run_checked_args(
                    base_executor,
                    ["git", "clone", "--branch", resolved_branch, clone_url, "."],
                    "clone repository",
                )
            else:
                await self._run_checked_args(
                    base_executor,
                    ["git", "remote", "set-url", "origin", clone_url],
                    "set remote url",
                )
                await self._run_checked_args(
                    base_executor,
                    ["git", "fetch", "origin", "--prune"],
                    "fetch repository",
                )
                await self._run_checked_args(
                    base_executor,
                    ["git", "checkout", resolved_branch],
                    "checkout base branch",
                )
                await self._run_checked_args(
                    base_executor,
                    ["git", "reset", "--hard", f"origin/{resolved_branch}"],
                    "reset base branch",
                )

            if worktree.exists():
                await self._run_checked_args(
                    base_executor,
                    ["git", "worktree", "remove", "--force", str(worktree)],
                    "remove stale task worktree",
                )
            worktree.parent.mkdir(parents=True, exist_ok=True)
            await self._run_checked_args(
                base_executor,
                [
                    "git",
                    "worktree",
                    "add",
                    "-B",
                    branch_name,
                    str(worktree),
                    f"origin/{resolved_branch}",
                ],
                "create task worktree",
            )

        worktree_executor = AgentTeamShellExecutor(worktree, self.workspace_service)
        commit_sha = (
            await self._run_checked_args(
                worktree_executor, ["git", "rev-parse", "HEAD"], "read commit sha"
            )
        ).stdout.strip()
        await self._install_workspace_dependencies(worktree_executor, worktree)
        return GitWorkspaceInfo(
            workspace=worktree,
            branch_name=branch_name,
            default_branch=resolved_branch,
            commit_sha=commit_sha,
        )

    async def _install_workspace_dependencies(
        self, executor: AgentTeamShellExecutor, workspace: Path
    ) -> None:
        """为工作区安装项目依赖（仅 Python 项目创建 venv）。"""
        value = await get_dynamic_config("agent_team_auto_install_deps")
        settings = get_settings()
        enabled = getattr(settings, "agent_team_auto_install_deps", True)
        if value is not None:
            enabled = str(value).strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            return

        has_pyproject = (workspace / "pyproject.toml").exists()
        has_requirements = (workspace / "requirements.txt").exists()
        if not has_pyproject and not has_requirements:
            return

        venv_dir = workspace / ".venv"
        if not venv_dir.exists():
            from loguru import logger

            logger.info("Agent 工作区创建独立 venv: {}", venv_dir)
            await executor.run("python -m venv .venv", timeout_seconds=settings.agent_team_timeout_seconds)

        pip_cmd = str(venv_dir / ("Scripts" if os.name == "nt" else "bin") / "pip")
        if has_pyproject:
            await executor.run(
                f"{pip_cmd} install -e . --quiet", timeout_seconds=settings.agent_team_timeout_seconds
            )
        elif has_requirements:
            await executor.run(
                f"{pip_cmd} install -r requirements.txt --quiet", timeout_seconds=settings.agent_team_timeout_seconds
            )

    def make_branch_name(
        self,
        task_id: int | None = None,
        source_issue_number: int | None = None,
        source_id: int | None = None,
        source_type: str | None = None,
    ) -> str:
        """生成 Agent 分支名。task_id 保证同仓库并发任务不碰撞。"""
        if task_id and task_id > 0:
            prefix = f"task-{task_id}"
        else:
            prefix = "task-manual"
        if source_issue_number:
            # PR_REVIEW 类型使用 pr- 前缀，其余使用 issue-
            if source_type and source_type.lower() == "pr_review":
                source = f"pr-{source_issue_number}"
            else:
                source = f"issue-{source_issue_number}"
        elif source_id:
            source = f"source-{source_id}"
        else:
            source = "manual"
        return f"{self.BRANCH_PREFIX}/{prefix}-{source}"

    async def resume_workspace(
        self,
        repo_owner: str,
        repo_name: str,
        workspace_path: str,
        branch_name: str,
        base_branch: str | None = None,
        base_commit_sha: str | None = None,
    ) -> GitWorkspaceInfo:
        """恢复既有 Agent 工作区，不重置未提交改动。"""
        workspace = self.workspace_service.ensure_within_base(workspace_path)
        if not self.workspace_service.is_path_inside_repo(repo_owner, repo_name, workspace):
            raise RuntimeError("续跑工作区与任务仓库不匹配")
        if not workspace.exists() or not (workspace / ".git").exists():
            raise RuntimeError("续跑工作区不存在或不是 Git 仓库")

        executor = AgentTeamShellExecutor(workspace, self.workspace_service)
        current_branch = (
            await self._run_checked_args(
                executor, ["git", "branch", "--show-current"], "read current branch"
            )
        ).stdout.strip()
        if current_branch != branch_name:
            raise RuntimeError(
                f"续跑分支不匹配: 当前 {current_branch or '(detached)'}，期望 {branch_name}"
            )

        remote_url = (
            await self._run_checked_args(
                executor, ["git", "remote", "get-url", "origin"], "read remote url"
            )
        ).stdout.strip()
        if f"/{repo_owner}/{repo_name}" not in remote_url and f"{repo_owner}/{repo_name}.git" not in remote_url:
            raise RuntimeError("续跑工作区 remote 与任务仓库不匹配")

        if base_commit_sha:
            await self._run_checked_args(
                executor,
                ["git", "cat-file", "-e", f"{base_commit_sha}^{{commit}}"],
                "verify base commit",
            )
        commit_sha = (
            await self._run_checked_args(
                executor, ["git", "rev-parse", "HEAD"], "read commit sha"
            )
        ).stdout.strip()
        return GitWorkspaceInfo(
            workspace=workspace,
            branch_name=branch_name,
            default_branch=base_branch or "main",
            commit_sha=commit_sha,
        )

    async def get_diff_summary(self, workspace: str | Path) -> str:
        """读取当前工作区 diff 摘要。"""
        executor = AgentTeamShellExecutor(workspace, self.workspace_service)
        result = await self._run_checked(
            executor, "git diff --stat && git status --short", "diff summary"
        )
        return result.stdout.strip()

    async def get_changed_file_stats(self, workspace: str | Path) -> dict[str, dict]:
        """读取当前工作区未提交变更的逐文件行数统计。"""
        executor = AgentTeamShellExecutor(workspace, self.workspace_service)
        numstat = await self._run_checked_args(
            executor, ["git", "diff", "--numstat", "HEAD"], "diff numstat"
        )
        status = await self._run_checked_args(
            executor, ["git", "status", "--short"], "status short"
        )
        return self.parse_changed_file_stats(numstat.stdout, status.stdout)

    @staticmethod
    def parse_changed_file_stats(
        numstat_output: str, status_output: str
    ) -> dict[str, dict]:
        """解析 git numstat 和 status 输出为 UI 可展示的变更统计。"""
        stats: dict[str, dict] = {}
        for line in numstat_output.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            additions_raw, deletions_raw, file_path = parts[0], parts[1], parts[2]
            normalized_path = _normalize_git_path(file_path)
            stats[normalized_path] = {
                "additions": _parse_numstat_count(additions_raw),
                "deletions": _parse_numstat_count(deletions_raw),
                "change_type": "modify",
            }

        for line in status_output.splitlines():
            if len(line) < 4:
                continue
            status_code = line[:2]
            raw_path = line[3:].strip()
            file_path = _normalize_git_path(raw_path.split(" -> ")[-1])
            item = stats.setdefault(file_path, {"additions": 0, "deletions": 0})
            item["change_type"] = _map_git_status_to_change_type(status_code)
        return stats

    async def _get_repo_info(
        self, repo_owner: str, repo_name: str, repo_full_name: str
    ) -> tuple[str, str]:
        client = self.github_app.get_repo_client(repo_owner, repo_name)
        if not client:
            raise RuntimeError(f"无法获取 GitHub 仓库客户端: {repo_full_name}")
        repo = client.get_repo(repo_full_name)
        default_branch = repo.default_branch or "main"
        clone_url = repo.clone_url
        token = self._get_installation_token(repo_owner, repo_name)
        if token:
            clone_url = clone_url.replace(
                "https://", f"https://x-access-token:{token}@"
            )
        return default_branch, clone_url

    def _get_installation_token(self, repo_owner: str, repo_name: str) -> str:
        try:
            installation = self.github_app.integration.get_installation(
                owner=repo_owner,
                repo=repo_name,
            )
            access_token = self.github_app.integration.get_access_token(installation.id)
            return access_token.token
        except Exception:
            return ""

    async def _run_checked(
        self,
        executor: AgentTeamShellExecutor,
        command: str,
        action: str,
    ) -> ShellCommandResult:
        result = await executor.run(command)
        if result.returncode != 0:
            raise RuntimeError(
                f"Git 工作区操作失败 ({action}): {result.stderr or result.stdout}"
            )
        return result

    async def _run_checked_args(
        self,
        executor: AgentTeamShellExecutor,
        args: list[str],
        action: str,
    ) -> ShellCommandResult:
        result = await executor.run_args(args)
        if result.returncode != 0:
            raise RuntimeError(
                f"Git 工作区操作失败 ({action}): {result.stderr or result.stdout}"
            )
        return result

    def _quote(self, value: str) -> str:
        safe = value.replace("'", "'\"'\"'")
        return f"'{safe}'"


def _parse_numstat_count(value: str) -> int:
    """解析 git numstat 行数；二进制文件用 '-'，按 0 处理。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_git_path(value: str) -> str:
    """归一化 Git 输出路径，兼容 ./ 前缀和 Windows 分隔符。"""
    normalized = value.strip().replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _map_git_status_to_change_type(status_code: str) -> str:
    """将 git status --short 状态映射为展示用变更类型。"""
    if "R" in status_code:
        return "rename"
    if "A" in status_code or "?" in status_code:
        return "add"
    if "D" in status_code:
        return "delete"
    if "M" in status_code:
        return "modify"
    return "change"
