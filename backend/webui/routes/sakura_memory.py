"""Sakura 记忆管理 WebUI 路由（超级管理员专用）"""

import asyncio
from typing import Optional

from fastapi import APIRouter, Request, Depends, HTTPException
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import SakuraMemoryState
from backend.webui.deps import (
    require_super_admin,
    get_db,
    get_templates,
    get_csrf_serializer,
    require_csrf,
    get_user_preferences,
    toast_redirect,
    render_template,
)
from backend.webui.helpers.admin_log import log_admin_action
from backend.webui.i18n import detect_language

router = APIRouter(prefix="/sakura-memory", tags=["WebUI Sakura Memory"])
templates = get_templates()
MAX_SAKURA_FILE_CONTENT_LENGTH = 50_000


def _get_knowledge_extraction_status(repo_state: SakuraMemoryState) -> dict:
    """计算知识提取的诊断状态 / Compute diagnostic status for knowledge extraction

    Returns:
        dict with keys: status (str), detail (str)
        status: "completed" | "disabled" | "ready" | "insufficient"
    """
    from backend.core.config import get_settings

    settings = get_settings()
    enabled = settings.sakura_knowledge_extraction_enabled
    min_reflections = settings.sakura_extraction_min_reflections or 10

    if not enabled:
        return {"status": "disabled", "detail": "已禁用", "min_reflections": min_reflections}

    if repo_state.knowledge_extracted:
        return {"status": "completed", "detail": "已完成", "min_reflections": min_reflections}

    count = repo_state.reflection_count
    if count < min_reflections:
        return {
            "status": "insufficient",
            "detail": f"反思数不足 ({count}/{min_reflections})",
            "min_reflections": min_reflections,
        }

    return {
        "status": "ready",
        "detail": f"待触发 ({count}≥{min_reflections})",
        "min_reflections": min_reflections,
    }


def _get_repo(repo_full_name: str):
    """获取 GitHub repo 对象"""
    from backend.core.github_app import GitHubAppClient

    parts = repo_full_name.split("/")
    if len(parts) != 2:
        return None
    owner, name = parts
    github_app = GitHubAppClient()
    client = github_app.get_repo_client(owner, name)
    if not client:
        return None
    return client.get_repo(repo_full_name)


def _get_write_service():
    """获取 GitHubWriteService"""
    from backend.services.github_write_service import GitHubWriteService

    return GitHubWriteService()


# ========== GET: 记忆管理主页 ==========


@router.get("/")
async def sakura_memory_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """Sakura 记忆管理主页"""
    result = await db.execute(
        select(SakuraMemoryState)
        .where(SakuraMemoryState.is_initialized == True)  # noqa: E712
        .order_by(SakuraMemoryState.updated_at.desc())
    )
    repos = result.scalars().all()

    return render_template(
        "sakura_memory.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="sakura_memory",
        repos=repos,
        selected_repo=None,
        repo_state=None,
        files=[],
        file_content=None,
        file_path=None,
    )


# ========== GET: 查看文件内容（必须在 /{repo:path} 之前注册） ==========


@router.get("/{repo:path}/file/{file_path:path}")
async def view_file(
    repo: str,
    file_path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """查看指定记忆文件内容"""
    repo_full_name = repo

    # 安全检查
    if "../" in file_path or "..\\" in file_path:
        raise HTTPException(status_code=400, detail="非法文件路径")

    # 获取状态
    result = await db.execute(
        select(SakuraMemoryState).where(
            SakuraMemoryState.repo_full_name == repo_full_name
        )
    )
    repo_state = result.scalar_one_or_none()

    # 获取所有仓库列表
    all_repos_result = await db.execute(
        select(SakuraMemoryState)
        .where(SakuraMemoryState.is_initialized == True)  # noqa: E712
        .order_by(SakuraMemoryState.updated_at.desc())
    )
    all_repos = all_repos_result.scalars().all()

    # 获取文件列表
    files = await _list_sakura_files(repo_full_name)

    # 读取文件内容
    content = await _read_sakura_file(repo_full_name, f".sakura/{file_path}")

    # 知识提取诊断状态
    ke_status = _get_knowledge_extraction_status(repo_state) if repo_state else None

    return render_template(
        "sakura_memory.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="sakura_memory",
        repos=all_repos,
        selected_repo=repo_full_name,
        repo_state=repo_state,
        ke_status=ke_status,
        files=files,
        file_content=content,
        file_path=file_path,
    )


# ========== GET: 查看仓库记忆 ==========


@router.get("/{repo:path}")
async def view_repo_memory(
    repo: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """查看指定仓库的记忆文件"""
    repo_full_name = repo

    # 获取状态
    result = await db.execute(
        select(SakuraMemoryState).where(
            SakuraMemoryState.repo_full_name == repo_full_name
        )
    )
    repo_state = result.scalar_one_or_none()

    if not repo_state or not repo_state.is_initialized:
        return toast_redirect(
            "/sakura-memory/",
            "toast.sakura_repo_not_initialized",
            "error",
            lang=detect_language(),
        )

    # 获取所有仓库列表（用于导航）
    all_repos_result = await db.execute(
        select(SakuraMemoryState)
        .where(SakuraMemoryState.is_initialized == True)  # noqa: E712
        .order_by(SakuraMemoryState.updated_at.desc())
    )
    all_repos = all_repos_result.scalars().all()

    # 获取文件列表
    files = await _list_sakura_files(repo_full_name)

    # 知识提取诊断状态
    ke_status = _get_knowledge_extraction_status(repo_state)

    return render_template(
        "sakura_memory.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="sakura_memory",
        repos=all_repos,
        selected_repo=repo_full_name,
        repo_state=repo_state,
        ke_status=ke_status,
        files=files,
        file_content=None,
        file_path=None,
    )


# ========== POST: 手动触发合并 ==========


@router.post("/{repo:path}/trigger/consolidate")
async def trigger_consolidate(
    repo: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """手动触发合并"""
    repo_full_name = repo
    try:
        from backend.services.sakura_memory_service import get_sakura_memory_service

        gh_repo = _get_repo(repo_full_name)
        if not gh_repo:
            return toast_redirect(
                f"/sakura-memory/{repo_full_name}",
                "toast.sakura_repo_unavailable",
                "error",
                lang=detect_language(),
            )

        service = get_sakura_memory_service()
        result = await db.execute(
            select(SakuraMemoryState).where(
                SakuraMemoryState.repo_full_name == repo_full_name
            )
        )
        state = result.scalar_one_or_none()
        count = state.reflection_count if state else 0

        await service.consolidate(gh_repo, repo_full_name, count)

        await log_admin_action(
            db, user["user_id"], "sakura_trigger", "consolidate", repo_full_name
        )
    except Exception as e:
        logger.error("手动合并失败: {} - {}", repo_full_name, e)
        return toast_redirect(
            f"/sakura-memory/{repo_full_name}",
            "toast.save_failed",
            "error",
            lang=detect_language(),
        )

    return toast_redirect(
        f"/sakura-memory/{repo_full_name}",
        "toast.sakura_consolidate_triggered",
        lang=detect_language(),
    )


# ========== POST: 手动触发知识提取 ==========


@router.post("/{repo:path}/trigger/extract")
async def trigger_extract(
    repo: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """手动触发知识提取"""
    repo_full_name = repo
    try:
        from backend.services.sakura_memory_service import get_sakura_memory_service

        gh_repo = _get_repo(repo_full_name)
        if not gh_repo:
            return toast_redirect(
                f"/sakura-memory/{repo_full_name}",
                "toast.sakura_repo_unavailable",
                "error",
                lang=detect_language(),
            )

        service = get_sakura_memory_service()
        success = await service.extract_and_save_knowledge(gh_repo, repo_full_name)

        if not success:
            return toast_redirect(
                f"/sakura-memory/{repo_full_name}",
                "toast.sakura_extract_failed",
                "error",
                lang=detect_language(),
            )

        await log_admin_action(
            db, user["user_id"], "sakura_trigger", "extract", repo_full_name
        )
    except Exception as e:
        logger.error("知识提取失败: {} - {}", repo_full_name, e)
        return toast_redirect(
            f"/sakura-memory/{repo_full_name}",
            "toast.save_failed",
            "error",
            lang=detect_language(),
        )

    return toast_redirect(
        f"/sakura-memory/{repo_full_name}",
        "toast.sakura_extract_triggered",
        lang=detect_language(),
    )


# ========== POST: 保存文件 ==========


@router.post("/{repo:path}/file/{file_path:path}")
async def save_file(
    repo: str,
    file_path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """保存编辑后的文件"""
    repo_full_name = repo

    if "../" in file_path or "..\\" in file_path:
        raise HTTPException(status_code=400, detail="非法文件路径")

    try:
        form = await request.form()
        content = str(form.get("content", ""))
        if len(content) > MAX_SAKURA_FILE_CONTENT_LENGTH:
            raise HTTPException(status_code=400, detail="文件内容过大")

        gh_repo = _get_repo(repo_full_name)
        if not gh_repo:
            return toast_redirect(
                f"/sakura-memory/{repo_full_name}",
                "toast.sakura_repo_unavailable",
                "error",
                lang=detect_language(),
            )

        write_service = _get_write_service()
        await write_service.commit_files(
            gh_repo,
            {f".sakura/{file_path}": content},
            f"docs(sakura): update {file_path}",
        )

        await log_admin_action(
            db,
            user["user_id"],
            "sakura_edit",
            "file",
            f"{repo_full_name}/{file_path}",
        )
    except Exception as e:
        logger.error("保存文件失败: {} - {}", file_path, e)
        return toast_redirect(
            f"/sakura-memory/{repo_full_name}",
            "toast.save_failed",
            "error",
            lang=detect_language(),
        )

    return toast_redirect(
        f"/sakura-memory/{repo_full_name}/file/{file_path}",
        "toast.sakura_file_saved",
        lang=detect_language(),
    )


# ========== POST: 删除文件 ==========


@router.post("/{repo:path}/file/{file_path:path}/delete")
async def delete_file(
    repo: str,
    file_path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """删除指定记忆文件"""
    repo_full_name = repo

    if "../" in file_path or "..\\" in file_path:
        raise HTTPException(status_code=400, detail="非法文件路径")

    try:
        gh_repo = _get_repo(repo_full_name)
        if not gh_repo:
            return toast_redirect(
                f"/sakura-memory/{repo_full_name}",
                "toast.sakura_repo_unavailable",
                "error",
                lang=detect_language(),
            )

        write_service = _get_write_service()
        sakura_ref = await write_service.get_sakura_branch(gh_repo)

        def _delete():
            file_content = gh_repo.get_contents(
                f".sakura/{file_path}", ref=sakura_ref or "HEAD"
            )
            if isinstance(file_content, list):
                return
            gh_repo.delete_file(
                f".sakura/{file_path}",
                f"chore(sakura): delete {file_path}",
                file_content.sha,
                branch=sakura_ref or "main",
            )

        await asyncio.to_thread(_delete)

        await log_admin_action(
            db,
            user["user_id"],
            "sakura_delete",
            "file",
            f"{repo_full_name}/{file_path}",
        )
    except Exception as e:
        logger.error("删除文件失败: {} - {}", file_path, e)
        return toast_redirect(
            f"/sakura-memory/{repo_full_name}",
            "toast.save_failed",
            "error",
            lang=detect_language(),
        )

    return toast_redirect(
        f"/sakura-memory/{repo_full_name}",
        "toast.sakura_file_deleted",
        lang=detect_language(),
    )


# ========== 辅助函数 ==========


async def _list_sakura_files(repo_full_name: str) -> list:
    """列出 .sakura/ 目录结构"""
    try:
        gh_repo = _get_repo(repo_full_name)
        if not gh_repo:
            return []

        write_service = _get_write_service()
        sakura_ref = await write_service.get_sakura_branch(gh_repo)

        def _list(path):
            try:
                contents = gh_repo.get_contents(path, ref=sakura_ref or "HEAD")
                if isinstance(contents, list):
                    return contents
                return [contents]
            except Exception:
                return []

        # 递归获取文件列表
        result = []

        async def _scan(path: str):
            contents = await asyncio.to_thread(lambda: _list(path))
            for item in contents:
                if item.type == "file":
                    result.append(
                        {
                            "path": item.path.replace(".sakura/", "", 1),
                            "name": item.name,
                            "size": item.size,
                            "type": "file",
                        }
                    )
                elif item.type == "dir":
                    result.append(
                        {
                            "path": item.path.replace(".sakura/", "", 1),
                            "name": item.name,
                            "type": "dir",
                        }
                    )
                    await _scan(item.path)

        await _scan(".sakura")
        return result

    except Exception as e:
        logger.error("列出文件失败: {} - {}", repo_full_name, e)
        return []


async def _read_sakura_file(repo_full_name: str, path: str) -> Optional[str]:
    """读取 .sakura/ 下的文件内容"""
    try:
        gh_repo = _get_repo(repo_full_name)
        if not gh_repo:
            return None

        write_service = _get_write_service()
        sakura_ref = await write_service.get_sakura_branch(gh_repo)

        def _read():
            content = gh_repo.get_contents(path, ref=sakura_ref or "HEAD")
            if isinstance(content, list):
                return None
            return content.decoded_content.decode("utf-8")

        return await asyncio.to_thread(_read)
    except Exception:
        return None
