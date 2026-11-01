"""API v1 仓库管理端点与仓库索引后台任务"""

import asyncio
import shutil
import subprocess
import tempfile

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from loguru import logger
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import require_api_admin, require_api_super_admin
from backend.api.v1.responses import error_response, success_response
from backend.core.time_service import monotonic
from backend.models.database import IssueAnalysis, PRReview
from backend.services.billing_context import billable_platform
from backend.services.database_reset_runtime_service import (
    create_registered_background_task,
)
from backend.webui.deps import get_db
from backend.webui.helpers.admin_log import log_admin_action

router = APIRouter(prefix="/repos", tags=["Repos"])

# 索引任务锁：防止同一仓库同时执行多个索引
# 注意：此锁为进程级，多 worker 部署时可能存在竞争
_active_index_tasks: dict[str, asyncio.Task] = {}

# 安装仓库列表缓存（5 分钟 TTL）
_installations_cache: tuple[list[dict], float] | None = None
_INSTALLATIONS_CACHE_TTL = 300  # 5 分钟


def _is_index_locked(repo_name: str, index_type: str) -> bool:
    """检查仓库的指定索引类型是否正在执行"""
    key = f"{repo_name}:{index_type}"
    task = _active_index_tasks.get(key)
    return task is not None and not task.done()


async def _get_installations_with_stats(db: AsyncSession) -> list[dict]:
    """获取所有安装的仓库列表（带缓存），并附加 PR/Issue 统计"""
    global _installations_cache
    now = monotonic()

    if (
        _installations_cache
        and (now - _installations_cache[1]) < _INSTALLATIONS_CACHE_TTL
    ):
        data = _installations_cache[0]
    else:
        from backend.core.github_app import GitHubAppClient

        github_app = GitHubAppClient()
        data = await asyncio.to_thread(github_app.get_all_installations_with_repos)
        _installations_cache = (data, now)

    # 收集所有仓库的 (owner, short_name)，批量查询统计（4N → 4 查询）
    # 数据库 repo_name 存短名称，repo_owner 存 owner，用两者组合区分同名仓库
    all_repo_keys: list[tuple[str, str]] = []
    repo_key_map: dict[str, tuple[str, str]] = {}  # full_name → (owner, short_name)
    for inst in data:
        for repo in inst["repos"]:
            parts = repo["full_name"].split("/")
            key = (parts[0], parts[-1])
            all_repo_keys.append(key)
            repo_key_map[repo["full_name"]] = key

    if all_repo_keys:
        # 去重，避免 IN 子句参数膨胀
        unique_keys = list(set(all_repo_keys))

        pr_counts = (
            await db.execute(
                select(PRReview.repo_owner, PRReview.repo_name, func.count(PRReview.id))
                .where(tuple_(PRReview.repo_owner, PRReview.repo_name).in_(unique_keys))
                .group_by(PRReview.repo_owner, PRReview.repo_name)
            )
        ).all()
        issue_counts = (
            await db.execute(
                select(
                    IssueAnalysis.repo_owner,
                    IssueAnalysis.repo_name,
                    func.count(IssueAnalysis.id),
                )
                .where(
                    tuple_(IssueAnalysis.repo_owner, IssueAnalysis.repo_name).in_(
                        unique_keys
                    )
                )
                .group_by(IssueAnalysis.repo_owner, IssueAnalysis.repo_name)
            )
        ).all()
        last_prs = (
            await db.execute(
                select(
                    PRReview.repo_owner,
                    PRReview.repo_name,
                    func.max(PRReview.created_at),
                )
                .where(tuple_(PRReview.repo_owner, PRReview.repo_name).in_(unique_keys))
                .group_by(PRReview.repo_owner, PRReview.repo_name)
            )
        ).all()
        last_issues = (
            await db.execute(
                select(
                    IssueAnalysis.repo_owner,
                    IssueAnalysis.repo_name,
                    func.max(IssueAnalysis.created_at),
                )
                .where(
                    tuple_(IssueAnalysis.repo_owner, IssueAnalysis.repo_name).in_(
                        unique_keys
                    )
                )
                .group_by(IssueAnalysis.repo_owner, IssueAnalysis.repo_name)
            )
        ).all()

        # 用 (owner, short_name) 元组作为 key，避免同名仓库统计混淆
        pr_count_map = {(r[0], r[1]): r[2] for r in pr_counts}
        issue_count_map = {(r[0], r[1]): r[2] for r in issue_counts}
        last_pr_map = {(r[0], r[1]): r[2] for r in last_prs}
        last_issue_map = {(r[0], r[1]): r[2] for r in last_issues}
    else:
        pr_count_map = {}
        issue_count_map = {}
        last_pr_map = {}
        last_issue_map = {}

    for inst in data:
        for repo in inst["repos"]:
            key = repo_key_map[repo["full_name"]]
            repo["pr_count"] = pr_count_map.get(key, 0)
            repo["issue_count"] = issue_count_map.get(key, 0)
            lp = last_pr_map.get(key)
            li = last_issue_map.get(key)
            # Both values are aware UTC instants when present. Avoid a naive
            # sentinel so repositories with only one activity kind remain
            # comparable under the strict UTCDateTime contract.
            repo["last_activity"] = max(
                (value for value in (lp, li) if value is not None),
                default=None,
            )

    return data


async def _clone_repo_for_indexing(repo_name: str) -> str:
    """克隆仓库到临时目录，返回 temp_dir 路径

    使用 Installation Access Token 进行 git clone 认证。
    调用方负责在 finally 中使用 shutil.rmtree 清理临时目录。
    """
    from backend.core.github_app import GitHubAppClient

    if "/" not in repo_name:
        raise ValueError(f"无效的仓库名称格式: {repo_name}，应为 owner/repo")

    github_app = GitHubAppClient()
    repo_owner, repo_name_only = repo_name.split("/", 1)
    client = github_app.get_repo_client(repo_owner, repo_name_only)
    if not client:
        raise RuntimeError(f"无法访问仓库: {repo_name}")

    # 获取 installation access token 用于 git clone 认证
    installation = github_app.integration.get_installation(
        owner=repo_owner, repo=repo_name_only
    )
    auth_token = github_app.integration.get_access_token(installation.id)

    repo = await asyncio.to_thread(client.get_repo, repo_name)
    temp_dir = tempfile.mkdtemp()

    try:
        clone_url = repo.clone_url.replace(
            "https://", f"https://x-access-token:{auth_token.token}@"
        )
        await asyncio.to_thread(
            subprocess.run,
            ["git", "clone", "--depth", "1", clone_url, temp_dir],
            check=True,
            capture_output=True,
            timeout=60,
        )
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return temp_dir


@billable_platform("shared_index", "administrator_shared_repository_index")
async def _run_docs_index(repo_name: str, user_id: int) -> None:
    """后台执行文档索引"""
    key = f"{repo_name}:docs"
    try:
        from backend.services.rag_service import get_rag_service
        from backend.webui.sse import publish_event

        temp_dir = await _clone_repo_for_indexing(repo_name)

        try:
            # 执行文档索引
            rag_service = get_rag_service()
            result = await rag_service.index_repository_docs(repo_name, temp_dir)

            logger.info(f"WebUI 文档索引完成: {repo_name}, result={result}")
            await publish_event(
                "index:docs_completed",
                {
                    "repo_name": repo_name,
                    "success": True,
                    "result": result,
                    "error": None,
                },
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    except Exception as e:
        logger.opt(exception=True).error(f"WebUI 文档索引失败: {repo_name}, error={e}")
        try:
            from backend.webui.sse import publish_event

            await publish_event(
                "index:docs_completed",
                {
                    "repo_name": repo_name,
                    "success": False,
                    "result": None,
                    "error": str(e),
                },
            )
        except Exception:
            pass
    finally:
        _active_index_tasks.pop(key, None)


@billable_platform("shared_index", "administrator_shared_repository_index")
async def _run_code_index(repo_name: str, user_id: int) -> None:
    """后台执行代码索引"""
    key = f"{repo_name}:code"
    try:
        from backend.services.code_index_service import get_code_index_service
        from backend.webui.sse import publish_event

        temp_dir = await _clone_repo_for_indexing(repo_name)

        try:
            # 获取 commit SHA
            commit_result = await asyncio.to_thread(
                subprocess.run,
                ["git", "rev-parse", "HEAD"],
                cwd=temp_dir,
                check=True,
                capture_output=True,
                text=True,
            )
            commit_sha = commit_result.stdout.strip()

            # 执行代码索引
            code_index_service = get_code_index_service()
            result = await code_index_service.index_repository_code(
                repo_name, temp_dir, commit_sha
            )

            logger.info(f"WebUI 代码索引完成: {repo_name}, result={result}")
            await publish_event(
                "index:code_completed",
                {
                    "repo_name": repo_name,
                    "success": True,
                    "result": result,
                    "error": None,
                },
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    except Exception as e:
        logger.opt(exception=True).error(f"WebUI 代码索引失败: {repo_name}, error={e}")
        try:
            from backend.webui.sse import publish_event

            await publish_event(
                "index:code_completed",
                {
                    "repo_name": repo_name,
                    "success": False,
                    "result": None,
                    "error": str(e),
                },
            )
        except Exception:
            pass
    finally:
        _active_index_tasks.pop(key, None)


@billable_platform("shared_index", "administrator_shared_repository_index")
async def _run_issues_index(repo_name: str, user_id: int) -> None:
    """后台执行 Issues 索引（open + closed 全量重建）"""
    key = f"{repo_name}:issues"
    logger.info("WebUI Issues 索引开始: {}", repo_name)
    try:
        from backend.services.issue_embedding_service import IssueEmbeddingService
        from backend.webui.sse import publish_event

        repo_owner, repo_name_only = repo_name.split("/", 1)
        emb_service = IssueEmbeddingService()
        result = await emb_service.index_repo_issues(
            repo_owner, repo_name_only, force=True
        )

        logger.info(f"WebUI Issues 索引完成: {repo_name}, result={result}")

        # 索引完成后失效候选池缓存
        try:
            from backend.services.agent_team.candidate_service import (
                AgentTeamCandidateService,
            )

            AgentTeamCandidateService().invalidate_cache()
        except Exception:
            pass

        await publish_event(
            "index:issues_completed",
            {
                "repo_name": repo_name,
                "success": True,
                "result": result,
                "error": None,
            },
        )
    except Exception as e:
        logger.opt(exception=True).error(
            "WebUI Issues 索引失败: {}, error={}", repo_name, e
        )
        try:
            from backend.webui.sse import publish_event

            await publish_event(
                "index:issues_completed",
                {
                    "repo_name": repo_name,
                    "success": False,
                    "result": None,
                    "error": str(e),
                },
            )
        except Exception as sse_err:
            logger.debug(f"SSE 发布索引失败事件失败: {sse_err}")
    finally:
        _active_index_tasks.pop(key, None)


async def _run_repo_scan(repo_name: str, user_id: int) -> None:
    """后台执行仓库扫描"""
    key = f"{repo_name}:scan"
    try:
        from backend.webui.sse import publish_event
        from backend.workers.scan_worker import ScanWorker

        worker = ScanWorker()
        scan_id = await worker.create_scan_record(
            repo_name=repo_name,
            trigger_type="manual",
            triggered_by=f"webui:{user_id}",
        )

        logger.info(f"WebUI 触发仓库扫描: {repo_name}, scan_id={scan_id}")
        await worker.process_scan(scan_id)

        await publish_event(
            "scan:completed",
            {"repo_name": repo_name, "scan_id": scan_id, "success": True},
        )
    except Exception as e:
        logger.opt(exception=True).error(f"WebUI 仓库扫描失败: {repo_name}, error={e}")
        try:
            from backend.webui.sse import publish_event

            await publish_event(
                "scan:completed",
                {
                    "repo_name": repo_name,
                    "scan_id": None,
                    "success": False,
                    "error": str(e),
                },
            )
        except Exception:
            pass
    finally:
        _active_index_tasks.pop(key, None)


@router.get("")
async def list_repos(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
):
    """仓库列表（含统计）"""
    try:
        installations = await _get_installations_with_stats(db)
    except Exception as e:
        logger.opt(exception=True).error(f"API 获取仓库列表失败: {e}")
        return error_response("获取仓库列表失败", status_code=500)

    return success_response(data=jsonable_encoder(installations))


@router.post("/{repo_name:path}/index-docs")
async def index_docs(
    repo_name: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
):
    """触发文档索引（异步后台执行）"""
    from backend.core.config import get_settings

    settings = get_settings()
    if not settings.enable_rag:
        return error_response("RAG 功能未启用，请在设置中开启")

    if _is_index_locked(repo_name, "docs"):
        return error_response(
            f"仓库 {repo_name} 正在索引中，请稍后再试", status_code=409
        )

    task = create_registered_background_task(
        _run_docs_index(repo_name, user["user_id"]), "repo_docs_index"
    )
    _active_index_tasks[f"{repo_name}:docs"] = task

    logger.info(f"API 触发文档索引: {repo_name}, by={user['sub']}")
    await log_admin_action(db, user["user_id"], "repo_index_docs", "repo", repo_name)
    return success_response(message=f"文档索引已启动: {repo_name}")


@router.post("/{repo_name:path}/index-code")
async def index_code(
    repo_name: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
):
    """触发代码索引（异步后台执行）"""
    from backend.core.config import get_settings

    settings = get_settings()
    if not settings.enable_code_index:
        return error_response("代码索引功能未启用，请在设置中开启")

    if _is_index_locked(repo_name, "code"):
        return error_response(
            f"仓库 {repo_name} 正在索引中，请稍后再试", status_code=409
        )

    task = create_registered_background_task(
        _run_code_index(repo_name, user["user_id"]), "repo_code_index"
    )
    _active_index_tasks[f"{repo_name}:code"] = task

    logger.info(f"API 触发代码索引: {repo_name}, by={user['sub']}")
    await log_admin_action(db, user["user_id"], "repo_index_code", "repo", repo_name)
    return success_response(message=f"代码索引已启动: {repo_name}")


@router.post("/{repo_name:path}/index-issues")
async def index_issues(
    repo_name: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
):
    """触发 Issues 索引（异步后台执行）"""
    from backend.core.config import get_settings

    settings = get_settings()
    if not settings.enable_semantic_issue_linking:
        return error_response("语义 Issue 关联功能未启用，请在设置中开启")

    if _is_index_locked(repo_name, "issues"):
        return error_response(
            f"仓库 {repo_name} 正在索引中，请稍后再试", status_code=409
        )

    task = create_registered_background_task(
        _run_issues_index(repo_name, user["user_id"]), "repo_issues_index"
    )
    _active_index_tasks[f"{repo_name}:issues"] = task

    logger.info(f"API 触发 Issues 索引: {repo_name}, by={user['sub']}")
    await log_admin_action(db, user["user_id"], "repo_index_issues", "repo", repo_name)
    return success_response(message=f"Issues 索引已启动: {repo_name}")


@router.post("/{repo_name:path}/scan")
async def trigger_repo_scan(
    repo_name: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """触发仓库扫描（超级管理员）"""
    if _is_index_locked(repo_name, "scan"):
        return error_response(
            f"仓库 {repo_name} 正在扫描中，请稍后再试", status_code=409
        )

    task = create_registered_background_task(
        _run_repo_scan(repo_name, user["user_id"]), "repo_manual_scan"
    )
    _active_index_tasks[f"{repo_name}:scan"] = task

    logger.info(f"API 触发仓库扫描: {repo_name}, by={user['sub']}")
    await log_admin_action(db, user["user_id"], "repo_scan", "repo", repo_name)
    return success_response(message=f"仓库扫描已启动: {repo_name}")
