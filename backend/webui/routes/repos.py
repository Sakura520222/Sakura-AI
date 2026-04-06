"""WebUI 仓库管理路由"""

import asyncio
import shutil
import subprocess
import tempfile

from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import RedirectResponse, JSONResponse
from loguru import logger
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.telegram_models import RepoSubscription
from backend.webui.deps import (
    require_admin,
    get_db,
    get_templates,
    get_csrf_serializer,
    require_csrf,
    require_csrf_header,
    get_user_preferences,
    paginate,
    error_page,
    toast_redirect,
)
from backend.webui.helpers.admin_log import log_admin_action

router = APIRouter(prefix="/repos", tags=["WebUI Repos"])
templates = get_templates()

# 索引任务锁：防止同一仓库同时执行多个索引
# 注意：此锁为进程级，多 worker 部署时可能存在竞争
_active_index_tasks: dict[str, asyncio.Task] = {}


def _is_index_locked(repo_name: str, index_type: str) -> bool:
    """检查仓库的指定索引类型是否正在执行"""
    key = f"{repo_name}:{index_type}"
    task = _active_index_tasks.get(key)
    return task is not None and not task.done()


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
            publish_event(
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

            publish_event(
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
            publish_event(
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

            publish_event(
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
    user: dict = Depends(require_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """渲染仓库列表页面"""
    return templates.TemplateResponse(
        "repos.html",
        {
            "request": request,
            "current_user": user,
            "csrf_token": get_csrf_serializer().dumps({}),
            "active_page": "repos",
            "user_prefs": user_prefs,
        },
    )


@router.get("/list-fragment")
async def repo_list_fragment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    user_prefs: dict = Depends(get_user_preferences),
    search: str = Query("", description="搜索仓库名称"),
    status: str = Query("active", description="按状态过滤（active/all）"),
    page: int = Query(1, ge=1),
    per_page: int = Query(None, ge=1, le=100),
):
    """仓库列表 HTMX 片段（支持搜索、过滤、分页）"""
    if per_page is None:
        per_page = user_prefs["items_per_page"]
    query = select(RepoSubscription)
    count_query = select(func.count(RepoSubscription.id))

    # 搜索过滤
    if search:
        escaped = search.replace("%", r"\%").replace("_", r"\_")
        search_filter = RepoSubscription.repo_name.ilike(f"%{escaped}%", escape="\\")
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)

    # 状态过滤
    if status == "active":
        query = query.where(RepoSubscription.is_active)
        count_query = count_query.where(RepoSubscription.is_active)

    # 排序
    query = query.order_by(desc(RepoSubscription.created_at))

    # 分页
    repos, total, total_pages, page = await paginate(
        db, query, count_query, page, per_page
    )

    return templates.TemplateResponse(
        "components/repo_list_fragment.html",
        {
            "request": request,
            "repos": repos,
            "search": search,
            "status": status,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "per_page": per_page,
            "csrf_token": get_csrf_serializer().dumps({}),
        },
    )


@router.post("/add")
async def add_repo(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    csrf_token: str = Depends(require_csrf),
    repo_name: str = Form(...),
) -> RedirectResponse:
    """添加仓库到白名单"""
    repo_name = repo_name.strip()

    # 验证格式: 必须包含 owner/repo，具体合法性由 GitHub API 保证
    if (
        not repo_name
        or repo_name.count("/") != 1
        or not repo_name.replace("/", "").strip()
    ):
        return toast_redirect(
            "/webui/repos/", "仓库名称格式不正确，请使用 owner/repo 格式", "error"
        )

    # 检查是否已存在
    existing = await db.execute(
        select(RepoSubscription).where(RepoSubscription.repo_name == repo_name)
    )
    if existing.scalar_one_or_none():
        return toast_redirect("/webui/repos/", "仓库已存在于白名单中", "warning")

    repo = RepoSubscription(
        repo_name=repo_name,
        is_active=True,
        added_by=user["user_id"],
    )
    db.add(repo)
    await db.commit()

    logger.info(f"仓库已添加到白名单: {repo_name}, by={user['sub']}")
    await log_admin_action(db, user["user_id"], "repo_add", "repo", repo_name)
    return toast_redirect("/webui/repos/", f"仓库 {repo_name} 已添加到白名单")


@router.post("/{repo_id}/toggle")
async def toggle_repo_status(
    request: Request,
    repo_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    csrf_token: str = Depends(require_csrf),
) -> RedirectResponse:
    """启用/禁用仓库"""
    result = await db.execute(
        select(RepoSubscription).where(RepoSubscription.id == repo_id)
    )
    repo = result.scalar_one_or_none()
    if not repo:
        return error_page(request, message="仓库不存在", user=user)

    repo.is_active = not repo.is_active
    await db.commit()

    status = "启用" if repo.is_active else "禁用"
    logger.info(
        f"仓库状态已变更: repo={repo.repo_name}, status={status}, by={user['sub']}"
    )
    await log_admin_action(
        db,
        user["user_id"],
        "repo_toggle",
        "repo",
        repo.repo_name,
        {"is_active": repo.is_active},
    )
    return toast_redirect("/webui/repos/", f"仓库 {repo.repo_name} 已{status}")


@router.post("/{repo_id}/remove")
async def remove_repo(
    request: Request,
    repo_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    csrf_token: str = Depends(require_csrf),
) -> RedirectResponse:
    """移除仓库"""
    result = await db.execute(
        select(RepoSubscription).where(RepoSubscription.id == repo_id)
    )
    repo = result.scalar_one_or_none()
    if not repo:
        return error_page(request, message="仓库不存在", user=user)

    repo_name = repo.repo_name
    await db.delete(repo)
    await db.commit()

    logger.info(f"仓库已从白名单移除: {repo_name}, by={user['sub']}")
    await log_admin_action(db, user["user_id"], "repo_remove", "repo", repo_name)
    return toast_redirect("/webui/repos/", f"仓库 {repo_name} 已从白名单移除")


@router.post("/{repo_id}/index-docs")
async def index_docs(
    repo_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    csrf_token: str = Depends(require_csrf_header),
) -> JSONResponse:
    """触发仓库文档索引（异步后台执行）"""
    from backend.core.config import get_settings

    # 查询仓库
    result = await db.execute(
        select(RepoSubscription).where(RepoSubscription.id == repo_id)
    )
    repo = result.scalar_one_or_none()
    if not repo:
        return JSONResponse(
            {"success": False, "message": "仓库不存在"}, status_code=404
        )

    # 检查 RAG 功能是否启用
    settings = get_settings()
    if not settings.enable_rag:
        return JSONResponse(
            {"success": False, "message": "RAG 功能未启用，请在设置中开启"},
            status_code=400,
        )

    # 检查是否正在索引
    if _is_index_locked(repo.repo_name, "docs"):
        return JSONResponse(
            {"success": False, "message": f"仓库 {repo.repo_name} 正在索引中，请稍后再试"},
            status_code=409,
        )

    # 启动后台任务
    task = asyncio.create_task(_run_docs_index(repo.repo_name, user["user_id"]))
    _active_index_tasks[f"{repo.repo_name}:docs"] = task

    logger.info(f"WebUI 触发文档索引: {repo.repo_name}, by={user['sub']}")
    await log_admin_action(
        db, user["user_id"], "repo_index_docs", "repo", repo.repo_name
    )
    return JSONResponse({"success": True, "message": f"文档索引已启动: {repo.repo_name}"})


@router.post("/{repo_id}/index-code")
async def index_code(
    repo_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    csrf_token: str = Depends(require_csrf_header),
) -> JSONResponse:
    """触发仓库代码索引（异步后台执行）"""
    from backend.core.config import get_settings

    # 查询仓库
    result = await db.execute(
        select(RepoSubscription).where(RepoSubscription.id == repo_id)
    )
    repo = result.scalar_one_or_none()
    if not repo:
        return JSONResponse(
            {"success": False, "message": "仓库不存在"}, status_code=404
        )

    # 检查代码索引功能是否启用
    settings = get_settings()
    if not settings.enable_code_index:
        return JSONResponse(
            {"success": False, "message": "代码索引功能未启用，请在设置中开启"},
            status_code=400,
        )

    # 检查是否正在索引
    if _is_index_locked(repo.repo_name, "code"):
        return JSONResponse(
            {"success": False, "message": f"仓库 {repo.repo_name} 正在索引中，请稍后再试"},
            status_code=409,
        )

    # 启动后台任务
    task = asyncio.create_task(_run_code_index(repo.repo_name, user["user_id"]))
    _active_index_tasks[f"{repo.repo_name}:code"] = task

    logger.info(f"WebUI 触发代码索引: {repo.repo_name}, by={user['sub']}")
    await log_admin_action(
        db, user["user_id"], "repo_index_code", "repo", repo.repo_name
    )
    return JSONResponse({"success": True, "message": f"代码索引已启动: {repo.repo_name}"})
