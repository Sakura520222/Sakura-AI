"""Agent 专家团队 - PR 创建服务

负责：
1. 将工作区变更 commit 并 push 到 GitHub
2. 通过 GitHub API 创建 Pull Request
3. 处理 commit / push / PR 的完整流程
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from loguru import logger

from backend.core.config import get_settings
from backend.core.github_app import GitHubAppClient
from backend.services.agent_team.shell_executor import AgentTeamShellExecutor
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


def _get_fresh_installation_token(
    github_app: GitHubAppClient,
    repo_owner: str,
    repo_name: str,
) -> str:
    """获取新的 GitHub App installation access token。"""
    try:
        installation = github_app.integration.get_repo_installation(
            owner=repo_owner, repo=repo_name,
        )
        access_token = github_app.integration.get_access_token(installation.id)
        return access_token.token
    except Exception as exc:
        logger.warning("获取 installation token 失败: {}", exc)
        return ""


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
        max_push_retries: int = 2,
    ) -> str:
        """将工作区变更 commit 并 push，返回 commit SHA。

        push 失败时会尝试刷新 GitHub App token 并重试。
        """
        executor = AgentTeamShellExecutor(workspace, self.workspace_service)

        # 确保有 git user identity（容器环境可能缺少全局配置）
        bot_name = get_settings().bot_username or "Sakura Agent"
        await executor.run(f'git config user.name "{bot_name}[bot]"')
        await executor.run(
            f'git config user.email "{bot_name}[bot]+noreply@users.noreply.github.com"'
        )

        # 确保 .gitignore 排除 Agent 工作区不应提交的路径
        await self._ensure_gitignore(executor)

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

        # push（失败时刷新 token 重试）
        last_error: str | None = None
        for attempt in range(max_push_retries):
            push_result = await executor.run_args(
                ["git", "push", "-u", "origin", branch_name],
            )
            if push_result.returncode == 0:
                break

            last_error = (push_result.stderr or push_result.stdout).strip()
            logger.warning(
                "git push 失败 (attempt {}/{}): {}",
                attempt + 1,
                max_push_retries,
                last_error[:300],
            )
            if attempt < max_push_retries - 1:
                await self._refresh_remote_token(executor, repo_owner, repo_name)
                await asyncio.sleep(1)
        else:
            raise RuntimeError(
                f"git push 失败（已重试 {max_push_retries} 次）: "
                f"{last_error[:500] if last_error else 'unknown'}"
            )

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

    async def _refresh_remote_token(
        self,
        executor: AgentTeamShellExecutor,
        repo_owner: str,
        repo_name: str,
    ) -> None:
        """刷新 GitHub App installation token 并更新 remote origin URL。"""
        from backend.core.github_app import GitHubAppClient

        github_app = GitHubAppClient()
        token = _get_fresh_installation_token(github_app, repo_owner, repo_name)
        if not token:
            logger.warning("无法获取新 token，跳过 remote URL 刷新")
            return

        clone_url = (
            f"https://x-access-token:{token}@github.com/{repo_owner}/{repo_name}.git"
        )
        result = await executor.run_args(
            ["git", "remote", "set-url", "origin", clone_url],
        )
        if result.returncode != 0:
            logger.warning("更新 remote URL 失败: {}", result.stderr)
        else:
            logger.info("已刷新 remote origin token，准备重试 push")

    async def _ensure_gitignore(
        self,
        executor: AgentTeamShellExecutor,
    ) -> None:
        """确保 .gitignore 包含 Agent 工作区不应提交的路径。"""
        excludes = [
            ".venv/",
            "__pycache__/",
            "*.pyc",
            ".pytest_cache/",
            ".mypy_cache/",
            "node_modules/",
        ]
        # 追加不重复的条目
        read = await executor.run("cat .gitignore 2>/dev/null || true")
        existing = read.stdout
        missing = [e for e in excludes if e not in existing]
        if missing:
            append_block = ("\n" if existing and not existing.endswith("\n") else "") + "\n".join(missing) + "\n"
            await executor.run(f"printf '%s' {repr(append_block)} >> .gitignore")
            logger.info("已追加 {} 条 .gitignore 规则", len(missing))

    async def create_pull_request(
        self,
        repo_owner: str,
        repo_name: str,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
        draft: bool = False,
        max_retries: int = 3,
    ) -> PRCreationResult:
        """通过 GitHub API 创建 Pull Request，422 时自动重试。"""
        from backend.core.github_app import GitHubAppClient
        from github import GithubException

        github_app = GitHubAppClient()
        client = github_app.get_repo_client(repo_owner, repo_name)
        if not client:
            raise RuntimeError(f"无法获取 GitHub 客户端: {repo_owner}/{repo_name}")

        repo = client.get_repo(f"{repo_owner}/{repo_name}")

        last_error: Exception | None = None
        for attempt in range(max_retries):
            try:
                # 验证 head 分支存在
                try:
                    repo.get_branch(head_branch)
                except GithubException as branch_err:
                    if branch_err.status == 404:
                        logger.warning(
                            "PR 创建前 head 分支不存在 (attempt {}): {} — 等待后重试",
                            attempt + 1,
                            head_branch,
                        )
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2**attempt)
                            continue
                        raise RuntimeError(
                            f"head 分支在 GitHub 上不存在: {head_branch}"
                        ) from branch_err
                    raise

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
            except GithubException as e:
                last_error = e
                if e.status == 422 and attempt < max_retries - 1:
                    logger.warning(
                        "PR 创建 422 (attempt {}/{}): head={}, base={}, errors={}",
                        attempt + 1,
                        max_retries,
                        head_branch,
                        base_branch,
                        e.data.get("errors") if hasattr(e, "data") else str(e),
                    )
                    await asyncio.sleep(2**attempt)
                    continue
                raise

        raise last_error  # type: ignore[misc]

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
            "## Sakura Agent 自动生成的 PR\n",
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
            "*此 PR 由 Sakura Agent 自动生成，包含全栈专家的代码修改和专业审查角色的审查。*\n"
            "*请仔细审查后合并。*\n"
        )
        return "\n".join(parts)

    async def generate_pr_title(
        self,
        task_title: str,
        task_summary: str,
        modified_files: list[str],
        review_verdict: str = "",
        issue_number: int | None = None,
    ) -> str:
        """使用辅助 AI 生成自然风格的 PR 标题。

        生成失败时回退到 task_title 原文。
        """
        if not modified_files:
            return task_title

        try:
            from backend.services.agent_team.ai_client import (
                create_agent_team_summary_client,
            )

            client, model, _config = await create_agent_team_summary_client()

            files_text = ", ".join(modified_files[:20])
            if len(modified_files) > 20:
                files_text += f" ... (共 {len(modified_files)} 个文件)"

            issue_hint = f"\n关联 Issue: #{issue_number}" if issue_number else ""

            system_prompt = (
                "你是一个代码审查助手。根据任务描述和实际修改的文件，"
                "生成一个简洁的 PR 标题。\n\n"
                "要求：\n"
                "- 使用 Conventional Commits 风格：type(scope): description\n"
                "- type 从 feat/fix/refactor/docs/style/test/chore 中选择\n"
                "- scope 可选，表示影响范围\n"
                "- description 用英文，简洁概括实际改动\n"
                "- 不加 emoji，不加句号\n"
                "- 只返回标题文本，不要其他内容"
            )
            user_prompt = (
                f"任务标题: {task_title}\n"
                f"任务描述: {task_summary}\n"
                f"修改文件: {files_text}\n"
                f"审查结论: {review_verdict or 'N/A'}"
                f"{issue_hint}"
            )

            response = await client.call_with_retry(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model=model,
                temperature=0.1,
                max_tokens=100,
                timeout=15.0,
            )

            if not response.choices:
                return task_title

            raw = response.choices[0].message.content.strip()
            # 去除可能的 markdown 代码块包裹
            title = re.sub(r"^```\w*\n?", "", raw)
            title = re.sub(r"\n?```$", "", title)
            title = title.strip().split("\n")[0].strip()

            if not title or len(title) > 200:
                return task_title

            return title

        except Exception as e:
            logger.warning("AI 生成 PR 标题失败，使用原始标题: {}", e)
            return task_title
