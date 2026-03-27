"""WebUI 仓库管理路由"""

from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import RedirectResponse, HTMLResponse
from loguru import logger
import re
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.telegram_models import RepoSubscription
from backend.webui.deps import require_admin, get_db, get_templates, get_csrf_serializer, validate_csrf_token, get_user_preferences

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

    # 总数
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)

    # 分页
    offset = (page - 1) * per_page
    query = query.offset(offset).limit(per_page)

    result = await db.execute(query)
    repos = result.scalars().all()

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
    csrf_token: str = Form(...),
    repo_name: str = Form(...),
):
    """添加仓库到白名单"""
    if not validate_csrf_token(csrf_token):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="CSRF 验证失败")

    repo_name = repo_name.strip()

    # 验证格式 (owner/repo)
    if not repo_name or not re.match(r"^[a-zA-Z0-9_.\-]+/[a-zA-Z0-9_.\-]+$", repo_name):
        return RedirectResponse(url="/webui/repos/?error=invalid_format", status_code=302)

    # 检查是否已存在
    existing = await db.execute(
        select(RepoSubscription).where(RepoSubscription.repo_name == repo_name)
    )
    if existing.scalar_one_or_none():
        return RedirectResponse(url="/webui/repos/?error=already_exists", status_code=302)

    repo = RepoSubscription(
        repo_name=repo_name,
        is_active=True,
        added_by=user["user_id"],
    )
    db.add(repo)
    await db.commit()

    logger.info(f"仓库已添加到白名单: {repo_name}, by={user['sub']}")
    return RedirectResponse(url="/webui/repos/?saved=1", status_code=302)


@router.post("/{repo_id}/toggle")
async def toggle_repo_status(
    request: Request,
    repo_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    csrf_token: str = Form(...),
):
    """启用/禁用仓库"""
    if not validate_csrf_token(csrf_token):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="CSRF 验证失败")

    result = await db.execute(
        select(RepoSubscription).where(RepoSubscription.id == repo_id)
    )
    repo = result.scalar_one_or_none()
    if not repo:
        return HTMLResponse("<h1>仓库不存在</h1>", status_code=404)

    repo.is_active = not repo.is_active
    await db.commit()

    status = "启用" if repo.is_active else "禁用"
    logger.info(f"仓库状态已变更: repo={repo.repo_name}, status={status}, by={user['sub']}")
    return RedirectResponse(url="/webui/repos/?saved=1", status_code=302)


@router.post("/{repo_id}/remove")
async def remove_repo(
    request: Request,
    repo_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    csrf_token: str = Form(...),
):
    """移除仓库"""
    if not validate_csrf_token(csrf_token):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="CSRF 验证失败")

    result = await db.execute(
        select(RepoSubscription).where(RepoSubscription.id == repo_id)
    )
    repo = result.scalar_one_or_none()
    if not repo:
        return HTMLResponse("<h1>仓库不存在</h1>", status_code=404)

    repo_name = repo.repo_name
    await db.delete(repo)
    await db.commit()

    logger.info(f"仓库已从白名单移除: {repo_name}, by={user['sub']}")
    return RedirectResponse(url="/webui/repos/?saved=1", status_code=302)
