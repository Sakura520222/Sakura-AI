"""Agent 专家团队 Git 工作区同步服务"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

from backend.core.config import get_dynamic_config, get_settings
from backend.core.github_app import GitHubAppClient
from backend.services.agent_team.dependency_venv import (
    DependencyVenvLifecycleMixin,
)
from backend.services.agent_team.execution import (
    ExecutionError,
    ExecutionProfile,
    ExecutionRequest,
    ExecutionResult,
    ExecutionRunner,
    LocalExecutionRunner,
    TrustedGitRunner,
    execute_request,
    execution_workspace_key,
    trusted_remote_urls_match,
)
from backend.services.agent_team.network_policy import get_agent_team_network_policy
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


@dataclass(frozen=True)
class GitWorkspaceInfo:
    """Git 工作区同步结果。"""

    workspace: Path
    branch_name: str
    default_branch: str
    commit_sha: str


class StalePRHeadError(RuntimeError):
    """Raised when an original PR head moved during workspace admission."""


_repo_locks: dict[str, asyncio.Lock] = {}
_repo_locks_guard = asyncio.Lock()
# 防止 Lock 字典无限增长的简单上限。
# 实际场景中仓库数量远小于此值，超出时清理最旧的条目。
_REPO_LOCKS_MAX_SIZE = 256


async def _get_repo_lock(repo_full_name: str) -> asyncio.Lock:
    """获取同仓库 clone/fetch/worktree 操作的进程内锁。

    Known trade-off: 字典达到 ``_REPO_LOCKS_MAX_SIZE`` 时按 FIFO 淘汰最旧条目。
    若被淘汰的 Lock 恰好仍被 ``async with`` 持有，下一次同仓库请求会拿到新的
    Lock 实例，导致同仓库两个 git 操作短暂并发。256 的阈值使该竞态在实际
    部署中极难触发；驱逐时额外跳过 ``locked()`` 的活跃锁以进一步降低风险，
    极端情况下（全部活跃）允许暂时超限，优先避免并发。
    """
    async with _repo_locks_guard:
        # 超出上限时清理：FIFO 遍历，跳过仍被持有的活跃锁以避免竞态
        if len(_repo_locks) >= _REPO_LOCKS_MAX_SIZE:
            for oldest_key in list(_repo_locks):
                if not _repo_locks[oldest_key].locked():
                    del _repo_locks[oldest_key]
                    break
        lock = _repo_locks.get(repo_full_name)
        if lock is None:
            lock = asyncio.Lock()
            _repo_locks[repo_full_name] = lock
        return lock


class AgentTeamGitWorkspaceService(DependencyVenvLifecycleMixin):
    """负责 clone/fetch/checkout Agent 独立工作区。"""

    BRANCH_PREFIX = "sakura-agent"

    def __init__(
        self,
        github_app: GitHubAppClient | None = None,
        workspace_service: AgentTeamWorkspaceService | None = None,
    ):
        self.github_app = github_app or GitHubAppClient()
        self.workspace_service = workspace_service or AgentTeamWorkspaceService()

    @staticmethod
    def _dependency_result_was_cancelled(
        result: ExecutionResult,
        cancel_event: asyncio.Event | None,
    ) -> bool:
        """Apply the task cancellation signal to one dependency result.

        Cleanup failures are infrastructure errors even when the task was
        concurrently cancelled.  After that check, a runner's ``cancelled``
        bit is meaningful only when it corresponds to the task event owned by
        the worker; an unexplained cancellation must not make admission look
        successful.
        """

        if result.infrastructure_error:
            raise ExecutionError(
                f"Agent 依赖安装执行清理失败: {result.infrastructure_error}"
            )
        if cancel_event is not None and cancel_event.is_set():
            return True
        if result.cancelled:
            raise ExecutionError("Agent 依赖安装收到未关联任务取消的执行结果")
        return False

    async def prepare_workspace(
        self,
        repo_owner: str,
        repo_name: str,
        source_issue_number: int | None = None,
        source_id: int | None = None,
        base_branch: str | None = None,
        task_id: int | None = None,
        source_type: str | None = None,
        *,
        workspace_repo_owner: str | None = None,
        workspace_repo_name: str | None = None,
        source_branch: str | None = None,
        source_commit_sha: str | None = None,
    ) -> GitWorkspaceInfo:
        """准备仓库 checkout，并为当前 task 创建独立 worktree。

        普通任务沿用 ``base_branch``/默认分支并创建 ``sakura-agent`` 分支。
        PR_REVIEW 任务可以提供原 PR head 的仓库、分支和 SHA；此时 base
        checkout 保持 detached，task worktree 从原 head SHA 创建一个仅供本地
        使用的 task-local 分支。远端 push 目标由 task 的 PR head identity
        单独维护，避免同一个 original branch 被多个 task worktree 同时 checkout。
        """
        if task_id is None or task_id <= 0:
            raise RuntimeError("准备 Agent 工作区需要有效的 task_id")

        source_checkout = any(
            value is not None
            for value in (
                workspace_repo_owner,
                workspace_repo_name,
                source_branch,
                source_commit_sha,
            )
        )
        if source_checkout and (not source_branch or not source_commit_sha):
            raise RuntimeError("PR head checkout 缺少 branch 或 commit SHA")

        workspace_repo_owner = workspace_repo_owner or repo_owner
        workspace_repo_name = workspace_repo_name or repo_name
        workspace_repo_full_name = f"{workspace_repo_owner}/{workspace_repo_name}"
        default_branch, clone_url = await self._get_repo_info(
            workspace_repo_owner,
            workspace_repo_name,
            workspace_repo_full_name,
        )
        resolved_branch = (
            source_branch if source_checkout else base_branch or default_branch
        )
        if source_checkout:
            if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", source_commit_sha):
                raise RuntimeError("PR head commit SHA 格式无效")
            # The original PR branch is a remote write target, not a local
            # worktree identity.  A task-local branch lets a failed task remain
            # resumable while a retry creates another worktree for the same PR.
            branch_name = self.make_local_branch_name(task_id)
        else:
            branch_name = self.make_branch_name(
                task_id, source_issue_number, source_id, source_type
            )
        repo_root = self.workspace_service.get_repo_root_path(
            workspace_repo_owner, workspace_repo_name
        )
        base_workspace = self.workspace_service.ensure_base_workspace(
            workspace_repo_owner, workspace_repo_name
        )
        worktree = self.workspace_service.get_task_worktree_path(
            workspace_repo_owner, workspace_repo_name, task_id, branch_name
        )

        repo_lock = await _get_repo_lock(workspace_repo_full_name)
        async with repo_lock:
            base_executor = TrustedGitRunner(base_workspace, self.workspace_service)
            repo_executor = TrustedGitRunner(repo_root, self.workspace_service)
            credential_token = self._get_installation_token(
                workspace_repo_owner, workspace_repo_name
            )
            if source_checkout:
                await self._run_checked_args(
                    base_executor,
                    ["git", "check-ref-format", "--branch", source_branch],
                    "validate PR head branch",
                )
            if not (base_workspace / ".git").exists():
                await self._run_checked_args(
                    base_executor,
                    ["git", "clone", "--branch", resolved_branch, clone_url, "."],
                    "clone repository",
                    credential_token=credential_token,
                    trusted_expected_remote=clone_url,
                )
                if source_checkout:
                    actual_sha = (
                        await self._run_checked_args(
                            base_executor,
                            ["git", "rev-parse", "HEAD"],
                            "read PR head commit",
                        )
                    ).stdout.strip()
                    if actual_sha.lower() != source_commit_sha.lower():
                        raise RuntimeError(
                            "PR head 分支在工作区创建前已发生变化，请重新触发 /agent"
                        )
                    await self._run_checked_args(
                        base_executor,
                        ["git", "checkout", "--detach", source_commit_sha],
                        "checkout PR head commit",
                    )
                    await self._run_checked_args(
                        base_executor,
                        ["git", "reset", "--hard", source_commit_sha],
                        "reset PR head commit",
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
                    credential_token=credential_token,
                    trusted_expected_remote=clone_url,
                )
                if source_checkout:
                    actual_sha = (
                        await self._run_checked_args(
                            base_executor,
                            ["git", "rev-parse", f"origin/{resolved_branch}"],
                            "read PR head branch",
                        )
                    ).stdout.strip()
                    if actual_sha.lower() != source_commit_sha.lower():
                        raise RuntimeError(
                            "PR head 分支在任务创建后已发生变化，请重新触发 /agent"
                        )
                    await self._run_checked_args(
                        base_executor,
                        ["git", "checkout", "--detach", source_commit_sha],
                        "checkout PR head commit",
                    )
                    await self._run_checked_args(
                        base_executor,
                        ["git", "reset", "--hard", source_commit_sha],
                        "reset PR head commit",
                    )
                else:
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
                    repo_executor,
                    ["git", "worktree", "remove", "--force", str(worktree)],
                    "remove stale task worktree",
                    cwd=base_workspace,
                )
            worktree.parent.mkdir(parents=True, exist_ok=True)
            worktree_start = (
                source_commit_sha
                if source_checkout
                else f"origin/{resolved_branch}"
            )
            await self._run_checked_args(
                repo_executor,
                [
                    "git",
                    "worktree",
                    "add",
                    "-B",
                    branch_name,
                    str(worktree),
                    worktree_start,
                ],
                "create task worktree",
                cwd=base_workspace,
            )

        worktree_executor = TrustedGitRunner(worktree, self.workspace_service)
        commit_sha = (
            await self._run_checked_args(
                worktree_executor, ["git", "rev-parse", "HEAD"], "read commit sha"
            )
        ).stdout.strip()
        return GitWorkspaceInfo(
            workspace=worktree,
            branch_name=branch_name,
            default_branch=resolved_branch,
            commit_sha=commit_sha,
        )

    async def install_workspace_dependencies(
        self,
        workspace: str | Path,
        execution_runner: ExecutionRunner,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        """安装工作区依赖 after the workspace-scoped runner is admitted.

        The worker deliberately calls this only after Git has determined the
        exact task worktree and after the runner has passed its backend
        admission gate.  Dependency hooks are untrusted code and therefore
        cannot use ``TrustedGitRunner`` or an implicit host fallback; the
        explicit local-development path is separately gated by ``full_access``.
        """

        await self._install_workspace_dependencies(
            execution_runner,
            self._safe_workspace_path(workspace),
            cancel_event=cancel_event,
        )

    async def _install_workspace_dependencies(
        self,
        executor: ExecutionRunner,
        workspace: Path,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        """为工作区安装项目依赖（仅 Python 项目创建 venv）。"""
        from loguru import logger

        value = await get_dynamic_config("agent_team_auto_install_deps")
        settings = get_settings()
        enabled = getattr(settings, "agent_team_auto_install_deps", True)
        if value is not None:
            enabled = str(value).strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            return

        pyproject_path = self._resolve_dependency_path(workspace, "pyproject.toml")
        requirements_path = self._resolve_dependency_path(workspace, "requirements.txt")
        has_pyproject = pyproject_path.is_file()
        has_requirements = requirements_path.is_file()
        if not has_pyproject and not has_requirements:
            return

        # TrustedGitRunner inherits the local process implementation but is a
        # trusted-control runner, never an Agent dependency backend.
        if type(executor) is LocalExecutionRunner:
            try:
                network_policy = await get_agent_team_network_policy()
            except Exception as exc:
                raise ExecutionError(
                    "Agent 本地依赖安装无法读取网络策略，已拒绝在线安装"
                ) from exc
            if not network_policy.allows_local_backend:
                raise ExecutionError(
                    "Agent 本地依赖安装仅在 full_access 下可用；"
                    f"当前策略为 {network_policy.value}，不能在宿主联网安装依赖"
                )
            await self._install_local_workspace_dependencies(
                executor,
                workspace,
                has_pyproject=has_pyproject,
                cancel_event=cancel_event,
            )
            return

        supports_profile = getattr(executor, "supports_profile", None)
        if not callable(supports_profile) or not supports_profile(
            ExecutionProfile.DEPENDENCY
        ):
            raise ExecutionError(
                "Agent dependency installation requires an explicit sandbox runner"
            )

        try:
            network_policy = await get_agent_team_network_policy()
        except Exception as exc:
            raise ExecutionError(
                "Agent dependency installation 无法读取网络策略，已拒绝在线安装"
            ) from exc
        if not network_policy.allows_dependency_network:
            logger.warning(
                "Agent 工作区检测到依赖文件，但网络策略为 {}；"
                "跳过在线依赖安装，仅消费镜像内依赖或显式离线缓存。",
                network_policy.value,
            )
            return

        # ``full_access`` is the only policy that may run untrusted package
        # hooks online.  The sandbox runner maps this policy to its fixed
        # server-owned egress capability; no deployment network name is read
        # or accepted by this Backend service.
        egress_capability = getattr(executor, "egress_capability", None)
        if egress_capability != "egress":
            raise ExecutionError(
                "Agent dependency installation requires the sandboxd egress capability"
            )

        venv_dir = self._agent_dependency_venv_path(workspace, "sandbox")
        needs_bootstrap = not os.path.lexists(str(venv_dir))
        if not needs_bootstrap and not self._dependency_venv_has_launchers(
            venv_dir,
            "sandbox",
        ):
            self._remove_agent_dependency_venv(workspace, "sandbox")
            needs_bootstrap = True

        if needs_bootstrap:
            logger.info("Agent 工作区创建独立 venv: {}", venv_dir)
            venv_dir = self._create_dependency_venv_directory(
                workspace,
                "sandbox",
            )
            result = await execute_request(
                executor,
                ExecutionRequest(
                    workspace_key=execution_workspace_key(
                        workspace, self.workspace_service
                    ),
                    command=("python -m venv --copies /workspace/.venv/sandbox"),
                    profile=ExecutionProfile.DEPENDENCY,
                    timeout_seconds=600,
                    cancel_event=cancel_event,
                ),
            )
            if self._dependency_result_was_cancelled(result, cancel_event):
                return
            if result.returncode != 0:
                raise ExecutionError(
                    f"创建 Agent 依赖 venv 失败: {result.stderr or result.stdout}"
                )
            venv_dir = self._agent_dependency_venv_path(workspace, "sandbox")
            if not self._dependency_venv_has_launchers(venv_dir, "sandbox"):
                raise ExecutionError("创建 Agent sandbox 依赖 venv 不完整")

        # Re-resolve after the bootstrap request.  A replacement symlink or
        # junction must never redirect the final pip request outside the task
        # workspace.
        venv_dir = self._agent_dependency_venv_path(workspace, "sandbox")

        # sandboxd runner images are Linux OCI images even when the Web
        # process is developed on Windows; use the container-visible path.
        # The daemon maps this full-access dependency request to its
        # server-owned egress network.  No request or model field can select
        # or widen that network.
        pip_cmd = "/workspace/.venv/sandbox/bin/pip"
        if has_pyproject:
            result = await execute_request(
                executor,
                ExecutionRequest(
                    workspace_key=execution_workspace_key(
                        workspace, self.workspace_service
                    ),
                    command=f"{pip_cmd} install -e . --quiet",
                    profile=ExecutionProfile.DEPENDENCY,
                    timeout_seconds=600,
                    cancel_event=cancel_event,
                ),
            )
        elif has_requirements:
            result = await execute_request(
                executor,
                ExecutionRequest(
                    workspace_key=execution_workspace_key(
                        workspace, self.workspace_service
                    ),
                    command=(f"{pip_cmd} install -r requirements.txt --quiet"),
                    profile=ExecutionProfile.DEPENDENCY,
                    timeout_seconds=600,
                    cancel_event=cancel_event,
                ),
            )
        if self._dependency_result_was_cancelled(result, cancel_event):
            return
        if result.returncode != 0:
            raise ExecutionError(
                f"安装 Agent 工作区依赖失败: {result.stderr or result.stdout}"
            )

    async def _install_local_workspace_dependencies(
        self,
        executor: LocalExecutionRunner,
        workspace: Path,
        *,
        has_pyproject: bool,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        """在 full_access 源码开发模式下使用本机 venv 安装依赖。

        This path is deliberately separate from the sandbox protocol.  It
        uses only application-constructed argv, the current host interpreter,
        and the task workspace's OS-specific ``.venv`` layout; no Linux
        container path or shell command is ever passed to the local runner.
        """

        from loguru import logger

        if executor.workspace != workspace:
            raise ExecutionError("Agent 本地依赖 runner 与工作区不匹配")

        workspace_key = execution_workspace_key(workspace, self.workspace_service)
        venv_dir = self._agent_dependency_venv_path(workspace, "local")
        needs_bootstrap = not os.path.lexists(str(venv_dir))
        if not needs_bootstrap and not self._dependency_venv_has_launchers(
            venv_dir,
            "local",
        ):
            self._remove_agent_dependency_venv(workspace, "local")
            needs_bootstrap = True

        if needs_bootstrap:
            # Reserve the exact internal path before starting bootstrap. A
            # cancellation/timeout/nonzero exit leaves a partial tree that the
            # next admission detects and recreates.
            venv_dir = self._create_dependency_venv_directory(
                workspace,
                "local",
            )
            logger.info("Agent 工作区创建本地 venv: {}", venv_dir)
            result = await execute_request(
                executor,
                ExecutionRequest(
                    workspace_key=workspace_key,
                    argv=(
                        str(executor.dependency_python_executable),
                        "-m",
                        "venv",
                        ".venv/local",
                    ),
                    cwd=PurePosixPath("."),
                    profile=ExecutionProfile.DEPENDENCY,
                    timeout_seconds=600,
                    cancel_event=cancel_event,
                ),
            )
            if self._dependency_result_was_cancelled(result, cancel_event):
                return
            if result.returncode != 0:
                raise ExecutionError(
                    f"创建 Agent 本地依赖 venv 失败: {result.stderr or result.stdout}"
                )

        # Re-resolve after the bootstrap request before deriving the host
        # interpreter path, so a newly-created/replaced link cannot escape the
        # task workspace between the two dependency requests.
        venv_dir = self._agent_dependency_venv_path(workspace, "local")
        if not self._dependency_venv_has_launchers(venv_dir, "local"):
            raise ExecutionError("创建 Agent 本地依赖 venv 不完整")
        try:
            dependency_python = executor.dependency_venv_python()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ExecutionError("Agent 本地依赖 Python 路径不在工作区内") from exc
        if has_pyproject:
            dependency_args = (
                str(dependency_python),
                "-m",
                "pip",
                "install",
                "-e",
                ".",
                "--quiet",
            )
        else:
            dependency_args = (
                str(dependency_python),
                "-m",
                "pip",
                "install",
                "-r",
                "requirements.txt",
                "--quiet",
            )
        result = await execute_request(
            executor,
            ExecutionRequest(
                workspace_key=workspace_key,
                argv=dependency_args,
                cwd=PurePosixPath("."),
                profile=ExecutionProfile.DEPENDENCY,
                timeout_seconds=600,
                cancel_event=cancel_event,
            ),
        )
        if self._dependency_result_was_cancelled(result, cancel_event):
            return
        if result.returncode != 0:
            raise ExecutionError(
                f"安装 Agent 本地工作区依赖失败: {result.stderr or result.stdout}"
            )

    def _resolve_dependency_path(self, workspace: Path, relative_path: str) -> Path:
        """Resolve a dependency path and reject links escaping the workspace."""

        try:
            return self.workspace_service.resolve_inside_workspace(
                workspace,
                relative_path,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise ExecutionError(
                f"Agent 依赖路径不在工作区内: {relative_path}"
            ) from exc

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

    def make_local_branch_name(self, task_id: int) -> str:
        """生成仅供本地 worktree 使用的 task 分支名。"""
        if task_id <= 0:
            raise ValueError("task_id 必须为正整数")
        return f"{self.BRANCH_PREFIX}/local-task-{task_id}"

    async def resume_workspace(
        self,
        repo_owner: str,
        repo_name: str,
        workspace_path: str,
        branch_name: str,
        base_branch: str | None = None,
        base_commit_sha: str | None = None,
        *,
        expected_remote_branch: str | None = None,
        expected_remote_sha: str | None = None,
    ) -> GitWorkspaceInfo:
        """恢复既有 Agent 工作区，不重置未提交改动。"""
        if (expected_remote_branch is None) != (expected_remote_sha is None):
            raise ValueError("远端分支校验必须同时提供 branch 和 commit SHA")
        workspace = self.workspace_service.ensure_within_base(workspace_path)
        if not self.workspace_service.is_path_inside_repo(
            repo_owner, repo_name, workspace
        ):
            raise RuntimeError("续跑工作区与任务仓库不匹配")
        if not workspace.exists() or not (workspace / ".git").exists():
            raise RuntimeError("续跑工作区不存在或不是 Git 仓库")

        executor = TrustedGitRunner(workspace, self.workspace_service)
        current_branch = (
            await self._run_checked_args(
                executor, ["git", "branch", "--show-current"], "read current branch"
            )
        ).stdout.strip()
        if current_branch != branch_name:
            raise RuntimeError(
                f"续跑分支不匹配: 当前 {current_branch or '(detached)'}，期望 {branch_name}"
            )

        _, expected_remote_url = await self._get_repo_info(
            repo_owner,
            repo_name,
            f"{repo_owner}/{repo_name}",
        )
        remote_url = (
            await self._run_checked_args(
                executor, ["git", "remote", "get-url", "origin"], "read remote url"
            )
        ).stdout.strip()
        if not trusted_remote_urls_match(remote_url, expected_remote_url):
            raise RuntimeError("续跑工作区 remote 与 GitHub 仓库不匹配")

        if expected_remote_branch and expected_remote_sha:
            await self._run_checked_args(
                executor,
                ["git", "check-ref-format", "--branch", expected_remote_branch],
                "validate expected PR head branch",
            )
            await self._run_checked_args(
                executor,
                ["git", "fetch", "origin", expected_remote_branch],
                "fetch expected PR head branch",
                credential_token=self._get_installation_token(repo_owner, repo_name),
                trusted_expected_remote=expected_remote_url,
            )
            remote_sha = (
                await self._run_checked_args(
                    executor,
                    ["git", "rev-parse", f"origin/{expected_remote_branch}"],
                    "read expected PR head branch",
                )
            ).stdout.strip()
            if remote_sha.lower() != expected_remote_sha.lower():
                raise StalePRHeadError(
                    "原 PR head 已在 Agent 执行期间被其他提交推进，"
                    "请重新触发 /agent"
                )

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
        executor = TrustedGitRunner(workspace, self.workspace_service)
        stat_result = await self._run_checked_args(
            executor, ["git", "diff", "--stat"], "diff stat"
        )
        status_result = await self._run_checked_args(
            executor, ["git", "status", "--short"], "status short"
        )
        return "\n".join(
            item
            for item in (stat_result.stdout.strip(), status_result.stdout.strip())
            if item
        )

    async def get_changed_file_stats(self, workspace: str | Path) -> dict[str, dict]:
        """读取当前工作区未提交变更的逐文件行数统计。"""
        executor = TrustedGitRunner(workspace, self.workspace_service)
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
        # 凭据只在 TrustedGitRunner 的单次 askpass 生命周期内注入；remote URL
        # 永远保持公开地址，避免 token 落入 .git/config、异常或日志。
        return default_branch, _strip_git_credentials(repo.clone_url)

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
        executor: TrustedGitRunner,
        command: str,
        action: str,
    ) -> ExecutionResult:
        try:
            args = shlex.split(command)
        except ValueError as exc:
            raise RuntimeError(f"Git 工作区操作参数无效 ({action})") from exc
        if not args:
            raise RuntimeError(f"Git 工作区操作命令为空 ({action})")
        return await self._run_checked_args(executor, args, action)

    async def _run_checked_args(
        self,
        executor: TrustedGitRunner,
        args: list[str],
        action: str,
        cwd: str | Path = ".",
        credential_token: str | None = None,
        trusted_expected_remote: str | None = None,
    ) -> ExecutionResult:
        result = await executor.run_args(
            args,
            cwd=cwd,
            credential_token=credential_token,
            trusted_expected_remote=trusted_expected_remote,
        )
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
    except TypeError, ValueError:
        return 0


def _strip_git_credentials(value: str) -> str:
    """移除 remote userinfo，并拒绝 query/fragment 形式的隐藏凭据。"""
    if _looks_like_scp_remote(value):
        userinfo, target = value.split("@", 1)
        if userinfo == "git":
            return value
        return target
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https", "ssh"} or not parsed.netloc:
            return value
        if parsed.query or parsed.fragment:
            raise ValueError("Git remote 不得包含 query 或 fragment")
        if "@" not in parsed.netloc:
            return value
        if scheme == "ssh" and parsed.username == "git" and parsed.password is None:
            return value
        # 保留 host、端口、IPv6 方括号和大小写，只切掉最后一个 @ 之前的
        # userinfo；TrustedGitRunner 会进一步要求 SSH URL 使用 git 用户。
        host_netloc = parsed.netloc.rsplit("@", 1)[-1]
        return urlunsplit(
            (parsed.scheme, host_netloc, parsed.path, parsed.query, parsed.fragment)
        )
    except ValueError, TypeError:
        if isinstance(value, str) and _looks_like_scp_remote(value):
            userinfo, target = value.split("@", 1)
            if userinfo == "git":
                return value
            return target
        raise


def _looks_like_scp_remote(value: str) -> bool:
    """识别 user@host:path，避免误处理普通仓库相对路径。"""

    if "://" in value or "\x00" in value:
        return False
    at_index = value.find("@")
    if at_index <= 0:
        return False
    return ":" in value[at_index + 1 :] and "/" not in value[:at_index]


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
