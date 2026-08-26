"""Agent 专家团队 - PR 创建服务

负责：
1. 将工作区变更 commit 并 push 到 GitHub
2. 通过 GitHub API 创建 Pull Request
3. 处理 commit / push / PR 的完整流程
"""

from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import dataclass

from loguru import logger

from backend.core.branding import SAKURA_AI_REPO_URL
from backend.core.github_app import GitHubAppClient
from backend.models.agent_team_models import AgentTeamSourceType
from backend.services.agent_team.execution import TrustedGitRunner
from backend.services.agent_team.git_workspace_service import _strip_git_credentials
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


def _normalize_git_path(raw_path: str) -> str:
    """归一化 git 输出中的文件路径。"""
    path = raw_path.strip()
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    return re.sub(r"/+", "/", path.replace("\\", "/"))


def _decode_git_path(raw_path: str) -> str:
    """解析 git 输出中的文件路径。"""
    path = raw_path.strip()
    if " -> " in path:
        path = path.split(" -> ")[-1].strip()
    return _normalize_git_path(path)


def _decode_git_rename_paths(raw_path: str) -> tuple[str, str] | None:
    """解析 git rename 输出中的旧路径和新路径。"""
    path = raw_path.strip()
    if " -> " not in path:
        return None
    old_path, new_path = path.split(" -> ", 1)
    return _normalize_git_path(old_path), _normalize_git_path(new_path)


@dataclass(frozen=True)
class PRCreationResult:
    """PR 创建结果。"""

    pr_number: int
    pr_url: str
    commit_sha: str
    branch_name: str
    head_sha: str = ""


@dataclass(frozen=True)
class _ApiCommitChange:
    """通过 GitHub API 提交的单个文件变更。"""

    path: str
    mode: str
    content: bytes | None = None
    delete: bool = False


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
        """将工作区变更通过 GitHub API 提交到远端分支，返回 commit SHA。

        通过 GitHub App installation token 创建提交，保持与记忆系统一致的 bot 身份。
        """
        executor = TrustedGitRunner(workspace, self.workspace_service)

        # 确保 .gitignore 排除 Agent 工作区不应提交的路径
        await self._ensure_gitignore(executor)

        # 检查是否有变更
        status_result = await executor.run_args(["git", "status", "--porcelain"])
        if not status_result.stdout.strip():
            logger.info("工作区没有变更，跳过 commit")
            head_result = await executor.run_args(["git", "rev-parse", "HEAD"])
            return head_result.stdout.strip()

        changes = await self._collect_changes_for_api_commit(executor)
        if not changes:
            logger.info("工作区没有可通过 API 提交的文件，跳过 commit")
            head_result = await executor.run_args(["git", "rev-parse", "HEAD"])
            return head_result.stdout.strip()

        base_sha_result = await executor.run_args(["git", "rev-parse", "HEAD"])
        base_sha = base_sha_result.stdout.strip()

        github_app = GitHubAppClient()
        client = github_app.get_repo_client(repo_owner, repo_name)
        if not client:
            raise RuntimeError(f"无法获取 GitHub 客户端: {repo_owner}/{repo_name}")
        repo = client.get_repo(f"{repo_owner}/{repo_name}")

        last_error: str | None = None
        for attempt in range(max_push_retries):
            try:
                sha = await self._commit_changes_via_api(
                    repo,
                    changes,
                    branch_name,
                    commit_message,
                    base_sha,
                )
                break
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "GitHub API 提交失败 (attempt {}/{}): {}",
                    attempt + 1,
                    max_push_retries,
                    last_error[:300],
                )
                if attempt >= max_push_retries - 1:
                    raise RuntimeError(
                        f"GitHub API 提交失败（已重试 {max_push_retries} 次）: "
                        f"{last_error[:500] if last_error else 'unknown'}"
                    ) from exc
                await asyncio.sleep(1)

        await self._sync_local_branch_to_commit(
            executor,
            branch_name,
            sha,
            credential_token=self._get_installation_token(
                github_app, repo_owner, repo_name
            ),
            trusted_expected_remote=_strip_git_credentials(repo.clone_url),
        )
        logger.info(
            "Agent API 提交成功: {}:{} @ {}",
            repo_owner + "/" + repo_name,
            branch_name,
            sha[:8],
        )
        return sha

    async def _collect_changes_for_api_commit(
        self,
        executor: TrustedGitRunner,
    ) -> list[_ApiCommitChange]:
        """收集工作区中可通过 GitHub API 提交的文件变更。"""
        status_result = await executor.run_args(["git", "status", "--porcelain"])
        changes: list[_ApiCommitChange] = []
        for line in status_result.stdout.splitlines():
            if len(line) < 4:
                continue
            status_code = line[:2]
            raw_path = line[3:]
            rename_paths = _decode_git_rename_paths(raw_path)
            if "R" in status_code and rename_paths:
                old_path, file_path = rename_paths
                changes.append(
                    _ApiCommitChange(path=old_path, mode="100644", delete=True)
                )
            else:
                file_path = _decode_git_path(raw_path)
            if "D" in status_code:
                changes.append(
                    _ApiCommitChange(path=file_path, mode="100644", delete=True)
                )
                continue
            mode = await self._get_git_file_mode(executor, file_path)
            absolute_path = self.workspace_service.resolve_inside_workspace(
                executor.workspace,
                file_path,
            )
            if not absolute_path.is_file():
                continue
            changes.append(
                _ApiCommitChange(
                    path=file_path,
                    mode=mode,
                    content=absolute_path.read_bytes(),
                )
            )
        return changes

    async def _get_git_file_mode(
        self,
        executor: TrustedGitRunner,
        file_path: str,
    ) -> str:
        """读取 Git 索引中的文件模式，新增文件默认按普通文件处理。"""
        result = await executor.run_args(["git", "ls-files", "-s", "--", file_path])
        line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        mode = line.split()[0] if line else "100644"
        return mode if mode in {"100644", "100755", "120000"} else "100644"

    async def _commit_changes_via_api(
        self,
        repo,
        changes: list[_ApiCommitChange],
        branch_name: str,
        commit_message: str,
        base_sha: str,
    ) -> str:
        """使用 GitHub Git Data API 创建提交，身份由 installation token 决定。"""
        from github import GithubException
        from github.InputGitTreeElement import InputGitTreeElement

        def _sync() -> str:
            try:
                ref = repo.get_git_ref(f"heads/{branch_name}")
            except GithubException as exc:
                if exc.status != 404:
                    raise
                ref = repo.create_git_ref(
                    ref=f"refs/heads/{branch_name}",
                    sha=base_sha,
                )

            parent = repo.get_git_commit(ref.object.sha)
            tree_elements = []
            for change in changes:
                if change.delete:
                    tree_elements.append(
                        InputGitTreeElement(
                            path=change.path,
                            mode=change.mode,
                            type="blob",
                            sha=None,
                        )
                    )
                    continue

                content = change.content or b""
                encoded = base64.b64encode(content).decode("ascii")
                blob = repo.create_git_blob(encoded, "base64")
                tree_elements.append(
                    InputGitTreeElement(
                        path=change.path,
                        mode=change.mode,
                        type="blob",
                        sha=blob.sha,
                    )
                )

            tree = repo.create_git_tree(tree_elements, parent.tree)
            commit = repo.create_git_commit(commit_message, tree, [parent])
            ref.edit(commit.sha)
            return commit.sha

        return await asyncio.to_thread(_sync)

    async def _sync_local_branch_to_commit(
        self,
        executor: TrustedGitRunner,
        branch_name: str,
        commit_sha: str,
        credential_token: str | None = None,
        trusted_expected_remote: str | None = None,
    ) -> None:
        """同步本地工作区到 API 创建的远端提交。"""
        fetch_result = await executor.run_args(
            ["git", "fetch", "origin", branch_name],
            credential_token=credential_token,
            trusted_expected_remote=trusted_expected_remote,
        )
        if fetch_result.returncode != 0:
            logger.warning("同步 Agent 远端分支失败: {}", fetch_result.stderr)
            return
        reset_result = await executor.run_args(["git", "reset", "--hard", commit_sha])
        if reset_result.returncode != 0:
            logger.warning("重置 Agent 工作区到 API 提交失败: {}", reset_result.stderr)

    @staticmethod
    def _get_installation_token(
        github_app: GitHubAppClient,
        repo_owner: str,
        repo_name: str,
    ) -> str:
        """读取单次 Git 操作所需 token；不把它写入 remote URL。"""
        try:
            installation = github_app.integration.get_installation(
                owner=repo_owner,
                repo=repo_name,
            )
            access_token = github_app.integration.get_access_token(installation.id)
            return access_token.token
        except Exception:
            return ""

    async def _ensure_gitignore(
        self,
        executor: TrustedGitRunner,
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
        gitignore_path = executor.workspace / ".gitignore"
        existing = (
            gitignore_path.read_text(encoding="utf-8")
            if gitignore_path.exists()
            else ""
        )
        existing_rules = {line.strip() for line in existing.splitlines()}
        missing = [rule for rule in excludes if rule not in existing_rules]
        if missing:
            append_block = (
                ("\n" if existing and not existing.endswith("\n") else "")
                + "\n".join(missing)
                + "\n"
            )
            gitignore_path.write_text(existing + append_block, encoding="utf-8")
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
                    head_sha=getattr(getattr(pr, "head", None), "sha", "") or "",
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

    async def update_pull_request_body(
        self,
        repo_owner: str,
        repo_name: str,
        pr_number: int,
        body: str,
    ) -> None:
        """通过 GitHub API 更新 Pull Request 描述。"""
        github_app = GitHubAppClient()
        client = github_app.get_repo_client(repo_owner, repo_name)
        if not client:
            raise RuntimeError(f"无法获取 GitHub 客户端: {repo_owner}/{repo_name}")

        repo = client.get_repo(f"{repo_owner}/{repo_name}")

        def _sync() -> None:
            pr = repo.get_pull(pr_number)
            pr.edit(body=body)

        await asyncio.to_thread(_sync)

    def _source_ref_label(self, source_type: str) -> str:
        """根据 source_type 返回关联引用的标签文本。"""
        if source_type == AgentTeamSourceType.PR_REVIEW.value:
            return "修复来源 PR"
        return "关联 Issue"

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
            f"## [Sakura Agent]({SAKURA_AI_REPO_URL}) 自动生成的 PR\n",
            f"**任务**: {task_title}\n",
        ]
        if source_issue_number:
            ref_label = self._source_ref_label(source_type)
            parts.append(f"**{ref_label}**: #{source_issue_number}\n")
        parts.append(f"**来源**: {source_type}\n")
        parts.append(f"**迭代轮次**: {iteration_count}\n")

        parts.append(f"\n### 📋 任务描述\n{task_summary}\n")
        parts.append(f"\n### 🔍 分析\n{fullstack_analysis}\n")
        parts.append(f"\n### 📝 修改计划\n{fullstack_plan}\n")
        parts.append(f"\n### ✅ 内部审查\n{review_summary}\n")

        parts.append(
            f"\n---\n"
            f"*此 PR 由 [Sakura Agent]({SAKURA_AI_REPO_URL}) 自动生成，"
            f"包含全栈专家的代码修改和专业审查角色的审查。*\n"
            "*请仔细审查后合并。*\n"
        )
        return "\n".join(parts)

    def _build_metadata_header(
        self,
        source_type: str,
        source_issue_number: int | None,
        iteration_count: int,
    ) -> str:
        """构建 PR body 头部的元数据引用块。"""
        parts = [f"> **Auto-generated by [Sakura Agent]({SAKURA_AI_REPO_URL})**\n"]
        if source_issue_number:
            ref_label = self._source_ref_label(source_type)
            parts.append(f"> **{ref_label}**: #{source_issue_number}\n")
        parts.append(f"> **来源**: {source_type}\n")
        parts.append(f"> **迭代轮次**: {iteration_count}\n")
        return "\n".join(parts)

    async def generate_pr_body(
        self,
        task_title: str,
        task_summary: str,
        fullstack_analysis: str,
        review_summary: str,
        review_verdict: str,
        review_score: int,
        review_findings: list[dict],
        modified_files: list[str],
        iteration_count: int,
        source_type: str,
        source_issue_number: int | None = None,
        diff_summary: str = "",
        fallback_body: str = "",
    ) -> str:
        """使用辅助 AI 生成结构化的 PR 描述。

        生成失败时回退到 fallback_body（硬编码模板）。
        """
        if not modified_files:
            return fallback_body or self.build_pr_body(
                task_title=task_title,
                task_summary=task_summary,
                fullstack_analysis=fullstack_analysis,
                fullstack_plan="",
                review_summary=review_summary,
                iteration_count=iteration_count,
                source_type=source_type,
                source_issue_number=source_issue_number,
            )

        try:
            from backend.services.agent_team.ai_client import (
                create_agent_team_summary_client,
            )

            client, _summary_role, _config = await create_agent_team_summary_client()

            files_text = ", ".join(modified_files)
            findings_text = "\n".join(
                f"[{f.get('severity', 'info')}] {f.get('file', '')}: {f.get('message', '')}"
                for f in review_findings
            )

            system_prompt = (
                "你是一个代码变更总结助手。根据任务描述、代码分析、审查结果和 git diff 统计，"
                "生成一个结构化的 Pull Request 描述。\n\n"
                "要求：\n"
                "- 使用英文撰写主体内容\n"
                "- 生成以下标准段落（使用 ## 标题）：\n"
                "  1. Summary - 用 2-3 句话概括本 PR 的目的和实现方式\n"
                "  2. Changes - 按文件或功能模块列出主要改动，使用无序列表\n"
                "  3. Review Assessment - 概述内部审查结论和关键发现\n"
                "  4. Test plan - 描述如何验证这些更改\n"
                "- 不要生成元数据信息（关联 Issue、来源、迭代轮次等），这些会自动添加\n"
                "- 不要使用 emoji\n"
                "- 内容应基于提供的实际数据，不要编造信息\n"
                "- 只返回 markdown 正文，不要用代码块包裹"
            )
            user_prompt = (
                f"任务标题: {task_title}\n"
                f"任务描述: {task_summary}\n"
                f"全栈分析摘要: {fullstack_analysis}\n"
                f"修改文件列表: {files_text}\n"
            )
            if diff_summary:
                user_prompt += f"Git diff 统计:\n{diff_summary}\n"
            user_prompt += (
                f"\n审查结论: {review_verdict or 'N/A'}\n"
                f"审查分数: {review_score}/10\n"
                f"审查摘要: {review_summary}\n"
            )
            if findings_text:
                user_prompt += f"审查发现:\n{findings_text}\n"

            response = await client.call_with_retry(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model="",
                temperature=0.1,
                role=_summary_role,
            )

            if not response.choices:
                logger.warning("AI 生成 PR body 返回空结果，使用硬编码模板")
                return fallback_body

            raw = response.choices[0].message.content.strip()
            # 去除可能的 markdown 代码块包裹
            body = re.sub(r"^```\w*\n?", "", raw)
            body = re.sub(r"\n?```$", "", body)
            body = body.strip()

            if not body or len(body) < 100:
                logger.warning("AI 生成的 PR body 过短，使用硬编码模板")
                return fallback_body

            header = self._build_metadata_header(
                source_type,
                source_issue_number,
                iteration_count,
            )
            return header + "\n\n" + body

        except Exception as e:
            logger.warning("AI 生成 PR body 失败，使用硬编码模板: {}", e)
            return fallback_body

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

            client, _summary_role, _config = await create_agent_team_summary_client()

            files_text = ", ".join(modified_files)

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
                model="",
                temperature=0.1,
                role=_summary_role,
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

    async def generate_commit_message(
        self,
        task_title: str,
        task_summary: str,
        modified_files: list[str],
        fullstack_summary: str = "",
        review_feedback: str = "",
        fallback_message: str = "",
    ) -> str:
        """使用辅助 AI 生成 Conventional Commits 风格的提交信息。

        生成失败时回退到 fallback_message。
        """
        if not modified_files:
            return fallback_message or f"feat(agent): {task_title}"

        try:
            from backend.services.agent_team.ai_client import (
                create_agent_team_summary_client,
            )

            client, _summary_role, _config = await create_agent_team_summary_client()

            files_text = ", ".join(modified_files)

            system_prompt = (
                "你是一个代码提交信息生成助手。根据任务描述、实际修改文件和审查反馈，"
                "生成规范的 Git 提交信息。\n\n"
                "要求：\n"
                "- 使用 Conventional Commits 格式\n"
                "- 第一行: type(scope): 简短描述（不超过 72 字符）\n"
                "- type 从 feat/fix/refactor/docs/style/test/chore 中选择\n"
                "- scope 可选，表示影响范围\n"
                "- 空一行后写 body：用 1-3 句英文描述实际改动内容\n"
                "- 如果有审查反馈，在 body 末尾说明本次提交针对的反馈\n"
                "- 不加 emoji\n"
                "- 只返回提交信息文本，不要代码块包裹"
            )
            user_prompt = (
                f"任务标题: {task_title}\n"
                f"任务描述: {task_summary}\n"
                f"修改文件: {files_text}\n"
            )
            if fullstack_summary:
                user_prompt += f"全栈专家总结: {fullstack_summary}\n"
            if review_feedback:
                user_prompt += f"外部审查反馈: {review_feedback}\n"

            response = await client.call_with_retry(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model="",
                temperature=0.1,
                role=_summary_role,
            )

            if not response.choices:
                logger.warning("AI 生成 commit message 返回空结果，使用硬编码模板")
                return fallback_message or f"feat(agent): {task_title}"

            raw = response.choices[0].message.content.strip()
            msg = re.sub(r"^```\w*\n?", "", raw)
            msg = re.sub(r"\n?```$", "", msg).strip()

            if not msg or len(msg) < 10:
                logger.warning("AI 生成的 commit message 过短，使用硬编码模板")
                return fallback_message or f"feat(agent): {task_title}"

            return msg

        except Exception as e:
            logger.warning("AI 生成 commit message 失败，使用硬编码模板: {}", e)
            return fallback_message or f"feat(agent): {task_title}"
