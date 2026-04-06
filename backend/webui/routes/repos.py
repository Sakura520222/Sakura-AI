"""WebUI 仓库管理路由"""

import asyncio
import shutil
import subprocess
import tempfile
import time
from datetime import datetime

from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import PRReview, IssueAnalysis
from backend.webui.deps import (
    require_admin,
    get_db,
    get_templates,
    get_csrf_serializer,
    require_csrf_header,
    get_user_preferences,
)
from backend.webui.helpers.admin_log import log_admin_action

router = APIRouter(prefix="/repos", tags=["WebUI Repos"])
templates = get_templates()

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
    now = time.time()

    if _installations_cache and (now - _installations_cache[1]) < _INSTALLATIONS_CACHE_TTL:
        data = _installations_cache[0]
    else:
        from backend.core.github_app import GitHubAppClient

        github_app = GitHubAppClient()
        data = await asyncio.to_thread(github_app.get_all_installations_with_repos)
        _installations_cache = (data, now)

    # 为每个仓库附加统计数据
    for inst in data:
        for repo in inst["repos"]:
            full_name = repo["full_name"]
            # 查询 PR 数量
            pr_count = await db.scalar(
                select(func.count(PRReview.id)).where(PRReview.repo_name == full_name)
            )
            # 查询 Issue 数量
            issue_count = await db.scalar(
                select(func.count(IssueAnalysis.id)).where(
                    IssueAnalysis.repo_name == full_name
                )
            )
            # 最后活动时间
            last_pr = await db.scalar(
                select(func.max(PRReview.created_at)).where(
                    PRReview.repo_name == full_name
                )
            )
            last_issue = await db.scalar(
                select(func.max(IssueAnalysis.created_at)).where(
                    IssueAnalysis.repo_name == full_name
                )
            )
            last_activity = max(last_pr or datetime.min, last_issue or datetime.min)
            if last_activity == datetime.min:
                last_activity = None

            repo["pr_count"] = pr_count or 0
            repo["issue_count"] = issue_count or 0
            repo["last_activity"] = last_activity

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
        logger.error(f"WebUI 文档索引失败: {repo_name}, error={e}", exc_info=True)
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
        logger.error(f"WebUI 代码索引失败: {repo_name}, error={e}", exc_info=True)
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


@router.get("/")
async def repo_list_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """渲染仓库列表页面（从 GitHub App 安装获取仓库）"""
    error_message = None
    try:
        installations = await _get_installations_with_stats(db)
    except Exception as e:
        logger.error(f"获取安装仓库列表失败: {e}", exc_info=True)
        installations = []
        error_message = f"获取仓库列表失败: {e}"

    return templates.TemplateResponse(
        "repos.html",
        {
            "request": request,
            "current_user": user,
            "csrf_token": get_csrf_serializer().dumps({}),
            "active_page": "repos",
            "user_prefs": user_prefs,
            "installations": installations,
            "error_message": error_message,
        },
    )


@router.get("/list-fragment")
async def repo_list_fragment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
):
    """仓库列表 HTMX 片段（刷新统计数据）"""
    try:
        installations = await _get_installations_with_stats(db)
    except Exception as e:
        logger.error(f"获取安装仓库列表失败: {e}", exc_info=True)
        installations = []

    return templates.TemplateResponse(
        "components/repo_list_fragment.html",
        {
            "request": request,
            "installations": installations,
            "csrf_token": get_csrf_serializer().dumps({}),
        },
    )


@router.post("/{repo_name:path}/index-docs")
async def index_docs(
    request: Request,
    repo_name: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    csrf_token: str = Depends(require_csrf_header),
) -> JSONResponse:
    """触发仓库文档索引（异步后台执行）"""
    from backend.core.config import get_settings

    # 检查 RAG 功能是否启用
    settings = get_settings()
    if not settings.enable_rag:
        return JSONResponse(
            {"success": False, "message": "RAG 功能未启用，请在设置中开启"},
            status_code=400,
        )

    # 检查是否正在索引
    if _is_index_locked(repo_name, "docs"):
        return JSONResponse(
            {"success": False, "message": f"仓库 {repo_name} 正在索引中，请稍后再试"},
            status_code=409,
        )

    # 启动后台任务
    task = asyncio.create_task(_run_docs_index(repo_name, user["user_id"]))
    _active_index_tasks[f"{repo_name}:docs"] = task

    logger.info(f"WebUI 触发文档索引: {repo_name}, by={user['sub']}")
    await log_admin_action(db, user["user_id"], "repo_index_docs", "repo", repo_name)
    return JSONResponse({"success": True, "message": f"文档索引已启动: {repo_name}"})


@router.post("/{repo_name:path}/index-code")
async def index_code(
    request: Request,
    repo_name: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    csrf_token: str = Depends(require_csrf_header),
) -> JSONResponse:
    """触发仓库代码索引（异步后台执行）"""
    from backend.core.config import get_settings

    # 检查代码索引功能是否启用
    settings = get_settings()
    if not settings.enable_code_index:
        return JSONResponse(
            {"success": False, "message": "代码索引功能未启用，请在设置中开启"},
            status_code=400,
        )

    # 检查是否正在索引
    if _is_index_locked(repo_name, "code"):
        return JSONResponse(
            {"success": False, "message": f"仓库 {repo_name} 正在索引中，请稍后再试"},
            status_code=409,
        )

    # 启动后台任务
    task = asyncio.create_task(_run_code_index(repo_name, user["user_id"]))
    _active_index_tasks[f"{repo_name}:code"] = task

    logger.info(f"WebUI 触发代码索引: {repo_name}, by={user['sub']}")
    await log_admin_action(db, user["user_id"], "repo_index_code", "repo", repo_name)
    return JSONResponse({"success": True, "message": f"代码索引已启动: {repo_name}"})
