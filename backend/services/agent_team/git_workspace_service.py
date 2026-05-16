"""Agent 专家团队 Git 工作区同步服务"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from backend.core.github_app import GitHubAppClient
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
    ) -> GitWorkspaceInfo:
        """准备仓库工作区并切换到 Agent 分支。"""
        repo_full_name = f"{repo_owner}/{repo_name}"
        workspace = self.workspace_service.ensure_workspace(repo_owner, repo_name)
        default_branch, clone_url = await self._get_repo_info(
            repo_owner, repo_name, repo_full_name
        )
        executor = AgentTeamShellExecutor(workspace, self.workspace_service)

        if not (workspace / ".git").exists():
            await self._run_checked_args(
                executor,
                ["git", "clone", "--branch", default_branch, clone_url, "."],
                "clone repository",
            )
        else:
            await self._run_checked_args(
                executor,
                ["git", "remote", "set-url", "origin", clone_url],
                "set remote url",
            )
            await self._run_checked_args(
                executor, ["git", "fetch", "origin", "--prune"], "fetch repository"
            )
            await self._run_checked_args(
                executor,
                ["git", "checkout", default_branch],
                "checkout default branch",
            )
            await self._run_checked_args(
                executor,
                ["git", "reset", "--hard", f"origin/{default_branch}"],
                "reset default branch",
            )

        branch_name = self.make_branch_name(source_issue_number, source_id)
        await self._run_checked_args(
            executor, ["git", "checkout", "-B", branch_name], "checkout agent branch"
        )
        commit_sha = (
            await self._run_checked_args(
                executor, ["git", "rev-parse", "HEAD"], "read commit sha"
            )
        ).stdout.strip()
        return GitWorkspaceInfo(
            workspace=workspace,
            branch_name=branch_name,
            default_branch=default_branch,
            commit_sha=commit_sha,
        )

    def make_branch_name(
        self,
        source_issue_number: int | None = None,
        source_id: int | None = None,
    ) -> str:
        """生成 Agent 分支名。"""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        if source_issue_number:
            source = f"issue-{source_issue_number}"
        elif source_id:
            source = f"source-{source_id}"
        else:
            source = "manual"
        return f"{self.BRANCH_PREFIX}/{source}-{timestamp}"

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
        expected_workspace = self.workspace_service.get_workspace_path(repo_owner, repo_name)
        workspace = self.workspace_service.ensure_within_base(workspace_path)
        if workspace != expected_workspace:
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
