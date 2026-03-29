"""WebUI 仓库管理路由"""

from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import RedirectResponse
from loguru import logger
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.telegram_models import RepoSubscription
from backend.webui.deps import require_admin, get_db, get_templates, get_csrf_serializer, require_csrf, get_user_preferences, paginate, error_page, toast_redirect
from backend.webui.helpers.admin_log import log_admin_action

router = APIRouter(prefix="/repos", tags=["WebUI Repos"])
templates = get_templates()


@router.get("/")
async def repo_list_page(
    request: Request,
    user: dict = Depends(require_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """渲染仓库列表页面"""
    return templates.TemplateResponse("repos.html", {
        "request": request,
        "current_user": user,
        "csrf_token": get_csrf_serializer().dumps({}),
        "active_page": "repos",
        "user_prefs": user_prefs,
    })


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
        query = query.where(RepoSubscription.is_active == True)
        count_query = count_query.where(RepoSubscription.is_active == True)

    # 排序
    query = query.order_by(desc(RepoSubscription.created_at))

    # 分页
    repos, total, total_pages, page = await paginate(db, query, count_query, page, per_page)

    return templates.TemplateResponse("components/repo_list_fragment.html", {
        "request": request,
        "repos": repos,
        "search": search,
        "status": status,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "per_page": per_page,
        "csrf_token": get_csrf_serializer().dumps({}),
    })


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
    if not repo_name or repo_name.count("/") != 1 or not repo_name.replace("/", "").strip():
        return toast_redirect("/webui/repos/", "仓库名称格式不正确，请使用 owner/repo 格式", "error")

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
    await log_admin_action(db, user['user_id'], "repo_add", "repo", repo_name)
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
    logger.info(f"仓库状态已变更: repo={repo.repo_name}, status={status}, by={user['sub']}")
    await log_admin_action(db, user['user_id'], "repo_toggle", "repo", repo.repo_name, {"is_active": repo.is_active})
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
    await log_admin_action(db, user['user_id'], "repo_remove", "repo", repo_name)
    return toast_redirect("/webui/repos/", f"仓库 {repo_name} 已从白名单移除")
