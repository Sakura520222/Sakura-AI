"""API v1 仓库管理端点"""

import asyncio

from fastapi import APIRouter, Depends
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from backend.webui.deps import get_db
from backend.webui.helpers.admin_log import log_admin_action

from backend.api.v1.deps import require_api_admin, require_api_super_admin
from backend.api.v1.responses import success_response, error_response

from backend.webui.routes.repos import (
    _get_installations_with_stats,
    _is_index_locked,
    _run_docs_index,
    _run_code_index,
    _run_issues_index,
    _run_repo_scan,
    _active_index_tasks,
)

router = APIRouter(prefix="/repos", tags=["Repos"])


@router.get("")
async def list_repos(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
):
    """仓库列表（含统计）"""
    try:
        installations = await _get_installations_with_stats(db)
    except Exception as e:
        logger.error(f"API 获取仓库列表失败: {e}", exc_info=True)
        return error_response("获取仓库列表失败", status_code=500)

    return success_response(data=installations)


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

    task = asyncio.create_task(_run_docs_index(repo_name, user["user_id"]))
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

    task = asyncio.create_task(_run_code_index(repo_name, user["user_id"]))
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

    task = asyncio.create_task(_run_issues_index(repo_name, user["user_id"]))
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

    task = asyncio.create_task(_run_repo_scan(repo_name, user["user_id"]))
    _active_index_tasks[f"{repo_name}:scan"] = task

    logger.info(f"API 触发仓库扫描: {repo_name}, by={user['sub']}")
    await log_admin_action(db, user["user_id"], "repo_scan", "repo", repo_name)
    return success_response(message=f"仓库扫描已启动: {repo_name}")
