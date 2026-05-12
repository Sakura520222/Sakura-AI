"""Agent 专家团队 - PR 创建服务

负责：
1. 将工作区变更 commit 并 push 到 GitHub
2. 通过 GitHub API 创建 Pull Request
3. 处理 commit / push / PR 的完整流程
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from backend.core.config import get_settings
from backend.services.agent_team.shell_executor import AgentTeamShellExecutor
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


@dataclass(frozen=True)
class PRCreationResult:
    """PR 创建结果。"""

    pr_number: int
    pr_url: str
    commit_sha: str
    branch_name: str


class AgentTeamPRService:
    """Agent PR 创建服务。"""

    def __init__(
        self,
        workspace_service: AgentTeamWorkspaceService | None = None,
    ):
        self.workspace_service = workspace_service or AgentTeamWorkspaceService()

    async def commit_and_push(
        self,
        workspace: str,
        branch_name: str,
        commit_message: str,
        repo_owner: str,
        repo_name: str,
    ) -> str:
        """将工作区变更 commit 并 push，返回 commit SHA。"""
        executor = AgentTeamShellExecutor(workspace, self.workspace_service)

        # 确保有 git user identity（容器环境可能缺少全局配置）
        bot_name = get_settings().bot_username or "Sakura Agent"
        await executor.run(f'git config user.name "{bot_name}[bot]"')
        await executor.run(
            f'git config user.email "{bot_name}[bot]+noreply@users.noreply.github.com"'
        )

        # git add 所有修改
        await executor.run("git add -A")

        # 检查是否有变更
        status_result = await executor.run("git status --porcelain")
        if not status_result.stdout.strip():
            logger.info("工作区没有变更，跳过 commit")
            head_result = await executor.run_args(["git", "rev-parse", "HEAD"])
            return head_result.stdout.strip()

        # commit
        await executor.run_args(["git", "commit", "-m", commit_message])

        # push
        await executor.run_args(["git", "push", "-u", "origin", branch_name])

        # 获取 commit SHA
        head_result = await executor.run_args(["git", "rev-parse", "HEAD"])
        sha = head_result.stdout.strip()
        logger.info(
            "Agent 推送成功: {}:{} @ {}",
            repo_owner + "/" + repo_name,
            branch_name,
            sha[:8],
        )
        return sha

    async def create_pull_request(
        self,
        repo_owner: str,
        repo_name: str,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
        draft: bool = False,
    ) -> PRCreationResult:
        """通过 GitHub API 创建 Pull Request。"""
        from backend.core.github_app import GitHubAppClient

        github_app = GitHubAppClient()
        client = github_app.get_repo_client(repo_owner, repo_name)
        if not client:
            raise RuntimeError(f"无法获取 GitHub 客户端: {repo_owner}/{repo_name}")

        repo = client.get_repo(f"{repo_owner}/{repo_name}")

        pr = repo.create_pull(
            title=title,
            body=body,
            head=head_branch,
            base=base_branch,
            draft=draft,
        )

        logger.info(
            "Agent PR 创建成功: #{} {} -> {}",
            pr.number,
            head_branch,
            base_branch,
        )

        return PRCreationResult(
            pr_number=pr.number,
            pr_url=pr.html_url,
            commit_sha="",
            branch_name=head_branch,
        )

    def build_pr_body(
        self,
        task_title: str,
        task_summary: str,
        fullstack_analysis: str,
        fullstack_plan: str,
        review_summary: str,
        iteration_count: int,
        source_type: str,
        source_issue_number: int | None = None,
    ) -> str:
        """构建 PR 描述。"""
        parts = [
            "## 🤖 Sakura Agent 专家团队自动生成的 PR\n",
            f"**任务**: {task_title}\n",
        ]
        if source_issue_number:
            parts.append(f"**关联 Issue**: #{source_issue_number}\n")
        parts.append(f"**来源**: {source_type}\n")
        parts.append(f"**迭代轮次**: {iteration_count}\n")

        parts.append(f"\n### 📋 任务描述\n{task_summary}\n")
        parts.append(f"\n### 🔍 分析\n{fullstack_analysis}\n")
        parts.append(f"\n### 📝 修改计划\n{fullstack_plan}\n")
        parts.append(f"\n### ✅ 内部审查\n{review_summary}\n")

        parts.append(
            "\n---\n"
            "*此 PR 由 Sakura Agent 专家团队自动生成，包含全栈专家的代码修改和专业审查角色的审查。*\n"
            "*请仔细审查后合并。*\n"
        )
        return "\n".join(parts)
