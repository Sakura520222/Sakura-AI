"""Agent 专家团队 Git 工作区同步服务"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backend.core.github_app import GitHubAppClient
from backend.services.agent_team.shell_executor import AgentTeamShellExecutor, ShellCommandResult
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
        default_branch, clone_url = await self._get_repo_info(repo_owner, repo_name, repo_full_name)
        executor = AgentTeamShellExecutor(workspace, self.workspace_service)

        if not (workspace / ".git").exists():
            await self._run_checked_args(
                executor,
                ["git", "clone", "--branch", default_branch, clone_url, "."],
                "clone repository",
            )
        else:
            await self._run_checked_args(executor, ["git", "remote", "set-url", "origin", clone_url], "set remote url")
            await self._run_checked_args(executor, ["git", "fetch", "origin", "--prune"], "fetch repository")
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
        await self._run_checked_args(executor, ["git", "checkout", "-B", branch_name], "checkout agent branch")
        commit_sha = (
            await self._run_checked_args(executor, ["git", "rev-parse", "HEAD"], "read commit sha")
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
        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        if source_issue_number:
            source = f"issue-{source_issue_number}"
        elif source_id:
            source = f"source-{source_id}"
        else:
            source = "manual"
        return f"{self.BRANCH_PREFIX}/{source}-{timestamp}"

    async def get_diff_summary(self, workspace: str | Path) -> str:
        """读取当前工作区 diff 摘要。"""
        executor = AgentTeamShellExecutor(workspace, self.workspace_service)
        result = await self._run_checked(executor, "git diff --stat && git status --short", "diff summary")
        return result.stdout.strip()

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
            clone_url = clone_url.replace("https://", f"https://x-access-token:{token}@")
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
