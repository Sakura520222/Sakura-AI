"""Native console data and actions for repository memory and vector storage."""

import asyncio
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import require_api_super_admin
from backend.api.v1.private_routes import PrivateAPIRoute
from backend.core.time_service import format_rfc3339
from backend.models.database import SakuraMemoryState
from backend.services.vector_store import get_vector_store
from backend.webui.deps import get_db, require_csrf_header, toast_redirect
from backend.webui.helpers.admin_log import log_admin_action
from backend.webui.i18n import detect_language

router = APIRouter(
    prefix="/console", tags=["Console Data"], route_class=PrivateAPIRoute
)
MAX_SAKURA_FILE_CONTENT_LENGTH = 50_000


def action_result(response):
    if isinstance(response, RedirectResponse):
        location = response.headers.get("location", "")
        query = parse_qs(urlsplit(location).query)
        message = query.get("_toast", [""])[0]
        if query.get("_toast_type", [""])[0] == "error":
            raise HTTPException(400, message or "operation_failed")
        return {"success": True, "message": message}
    return response


def relative_file(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or any(part in {".", ".."} for part in value.split("/"))
        or any(char in value for char in ("\\", ":", "\x00"))
    ):
        raise HTTPException(422, "invalid_memory_path")
    return path.as_posix()


def _get_knowledge_extraction_status(repo_state: SakuraMemoryState) -> dict:
    """Return the current periodic knowledge extraction status."""
    from backend.core.config import get_sakura_memory_config

    config = get_sakura_memory_config().get("knowledge_extraction", {})
    enabled = config.get("enabled", True)
    interval = config.get("min_reflections", 15)
    if not enabled:
        return {"status": "disabled", "detail": "已禁用", "min_reflections": interval}

    since_last = repo_state.reflection_count - (repo_state.last_extraction_count or 0)
    if since_last >= interval:
        return {
            "status": "ready",
            "detail": f"待触发（{since_last} ≥ {interval}）",
            "min_reflections": interval,
        }
    return {
        "status": "pending",
        "detail": f"进行中（{since_last}/{interval}）",
        "min_reflections": interval,
    }


def _get_repo(repo_full_name: str):
    """Resolve a GitHub repository using the configured GitHub App."""
    from backend.core.github_app import GitHubAppClient

    parts = repo_full_name.split("/")
    if len(parts) != 2:
        return None
    owner, name = parts
    client = GitHubAppClient().get_repo_client(owner, name)
    if not client:
        return None
    return client.get_repo(repo_full_name)


def _get_write_service():
    """Create the GitHub writer used for Sakura memory files."""
    from backend.services.github_write_service import GitHubWriteService

    return GitHubWriteService()


def _get_sakura_memory_service():
    from backend.services.sakura_memory_service import get_sakura_memory_service

    return get_sakura_memory_service()


async def _list_sakura_files(repo_full_name: str) -> list:
    """List the files and directories under a repository's ``.sakura`` folder."""
    try:
        gh_repo = _get_repo(repo_full_name)
        if not gh_repo:
            return []

        sakura_ref = await _get_write_service().get_sakura_branch(gh_repo)

        def list_path(path):
            try:
                contents = gh_repo.get_contents(path, ref=sakura_ref or "HEAD")
                return contents if isinstance(contents, list) else [contents]
            except Exception:
                return []

        result = []

        async def scan(path: str):
            contents = await asyncio.to_thread(lambda: list_path(path))
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
                    await scan(item.path)

        await scan(".sakura")
        return result
    except Exception as exc:
        logger.error("列出文件失败: {} - {}", repo_full_name, exc)
        return []


async def _read_sakura_file(repo_full_name: str, path: str) -> str | None:
    """Read a file from a repository's Sakura memory branch."""
    try:
        gh_repo = _get_repo(repo_full_name)
        if not gh_repo:
            return None
        sakura_ref = await _get_write_service().get_sakura_branch(gh_repo)

        def read():
            content = gh_repo.get_contents(path, ref=sakura_ref or "HEAD")
            if isinstance(content, list):
                return None
            return content.decoded_content.decode("utf-8")

        return await asyncio.to_thread(read)
    except Exception:
        return None


@router.get("/memory")
async def memory_repositories(
    user=Depends(require_api_super_admin), db: AsyncSession = Depends(get_db)
):
    rows = (
        (
            await db.execute(
                select(SakuraMemoryState)
                .where(SakuraMemoryState.is_initialized.is_(True))
                .order_by(SakuraMemoryState.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "repo": row.repo_full_name,
                "reflections": row.reflection_count,
                "updated_at": format_rfc3339(row.updated_at),
                "extraction": _get_knowledge_extraction_status(row),
            }
            for row in rows
        ]
    }


@router.get("/memory/files")
async def memory_files(
    repo: str,
    file: str | None = None,
    user=Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    row = (
        await db.execute(
            select(SakuraMemoryState).where(
                SakuraMemoryState.repo_full_name == repo,
                SakuraMemoryState.is_initialized.is_(True),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "repository_not_initialized")
    if file is not None:
        content = await _read_sakura_file(repo, ".sakura/" + relative_file(file))
        if content is None:
            raise HTTPException(404, "file_unavailable")
        return {"content": content, "file": file}
    return {
        "files": await _list_sakura_files(repo),
        "extraction": _get_knowledge_extraction_status(row),
    }


class MemoryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    repo: str
    file: str
    content: str = Field(max_length=MAX_SAKURA_FILE_CONTENT_LENGTH)


@router.put("/memory/file", dependencies=[Depends(require_csrf_header)])
async def write_memory(
    body: MemoryWrite,
    user=Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    path = relative_file(body.file)
    repo = await asyncio.to_thread(_get_repo, body.repo)
    if repo is None:
        raise HTTPException(404, "repository_unavailable")
    await _get_write_service().commit_files(
        repo, {f".sakura/{path}": body.content}, f"docs(sakura): update {path}"
    )
    await log_admin_action(
        db, user["user_id"], "sakura_edit", "file", f"{body.repo}/{path}"
    )
    return {"success": True}


@router.delete("/memory/file")
async def delete_memory(
    request: Request,
    repo: str,
    file: str,
    user=Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
    csrf=Depends(require_csrf_header),
):
    repo_path = relative_file(file)
    try:
        gh_repo = _get_repo(repo)
        if not gh_repo:
            result = toast_redirect(
                f"/sakura-memory/{repo}",
                "toast.sakura_repo_unavailable",
                "error",
                lang=detect_language(),
            )
        else:
            write_service = _get_write_service()
            sakura_ref = await write_service.get_sakura_branch(gh_repo)

            def delete():
                file_content = gh_repo.get_contents(
                    f".sakura/{repo_path}", ref=sakura_ref or "HEAD"
                )
                if isinstance(file_content, list):
                    return
                gh_repo.delete_file(
                    f".sakura/{repo_path}",
                    f"chore(sakura): delete {repo_path}",
                    file_content.sha,
                    branch=sakura_ref or "main",
                )

            await asyncio.to_thread(delete)
            await log_admin_action(
                db, user["user_id"], "sakura_delete", "file", f"{repo}/{repo_path}"
            )
            result = toast_redirect(
                f"/sakura-memory/{repo}",
                "toast.sakura_file_deleted",
                lang=detect_language(),
            )
    except Exception as exc:
        logger.error("删除文件失败: {} - {}", repo_path, exc)
        result = toast_redirect(
            f"/sakura-memory/{repo}",
            "toast.save_failed",
            "error",
            lang=detect_language(),
        )
    return action_result(result)


async def _trigger_consolidate(repo_full_name: str, db, user):
    try:
        gh_repo = _get_repo(repo_full_name)
        if not gh_repo:
            return toast_redirect(
                f"/sakura-memory/{repo_full_name}",
                "toast.sakura_repo_unavailable",
                "error",
                lang=detect_language(),
            )

        result = await db.execute(
            select(SakuraMemoryState).where(
                SakuraMemoryState.repo_full_name == repo_full_name
            )
        )
        state = result.scalar_one_or_none()
        count = state.reflection_count if state else 0
        await _get_sakura_memory_service().consolidate(
            gh_repo, repo_full_name, count
        )
        await log_admin_action(
            db, user["user_id"], "sakura_trigger", "consolidate", repo_full_name
        )
    except Exception as exc:
        logger.error("手动合并失败: {} - {}", repo_full_name, exc)
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


async def _trigger_extract(repo_full_name: str, db, user):
    try:
        gh_repo = _get_repo(repo_full_name)
        if not gh_repo:
            return toast_redirect(
                f"/sakura-memory/{repo_full_name}",
                "toast.sakura_repo_unavailable",
                "error",
                lang=detect_language(),
            )

        success = await _get_sakura_memory_service().extract_and_save_knowledge(
            gh_repo, repo_full_name
        )
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
    except Exception as exc:
        logger.error("知识提取失败: {} - {}", repo_full_name, exc)
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


@router.post("/memory/{action}")
async def memory_operation(
    action: Literal["consolidate", "extract"],
    request: Request,
    repo: str,
    user=Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
    csrf=Depends(require_csrf_header),
):
    handler = _trigger_consolidate if action == "consolidate" else _trigger_extract
    return action_result(await handler(repo, db, user))


@router.get("/storage")
async def collections(user=Depends(require_api_super_admin)):
    def read():
        store = get_vector_store()
        return [
            {
                "name": item.name,
                "doc_count": item.count(),
                "metadata": item.metadata or {},
            }
            for item in store.client.list_collections()
        ]

    try:
        return {"items": await asyncio.to_thread(read)}
    except Exception:
        logger.exception("测试 ChromaDB 连接失败 / chromadb connection test failed")
        raise HTTPException(
            status_code=503, detail="ChromaDB 连接测试失败，请检查服务状态"
        ) from None


@router.get("/storage/{name}")
async def collection_detail(
    name: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    user=Depends(require_api_super_admin),
):
    def read():
        collection = get_vector_store().client.get_collection(name=name)
        data = collection.get(
            limit=limit, offset=offset, include=["documents", "metadatas"]
        )
        documents, metadata = data.get("documents") or [], data.get("metadatas") or []
        return {
            "name": name,
            "total": collection.count(),
            "offset": offset,
            "limit": limit,
            "metadata": collection.metadata or {},
            "documents": [
                {
                    "id": value,
                    "content": documents[index] if index < len(documents) else "",
                    "metadata": metadata[index] if index < len(metadata) else {},
                }
                for index, value in enumerate(data.get("ids") or [])
            ],
        }

    return await asyncio.to_thread(read)


async def _collection_action(name: str, action: str, db, user):
    if action == "delete":
        try:
            store = get_vector_store()
            await asyncio.to_thread(store.client.delete_collection, name=name)
            logger.info("Collection {} 已删除, by={}", name, user["sub"])
            await log_admin_action(
                db, user["user_id"], "vector_db_delete_collection", "collection", name
            )
        except Exception as exc:
            logger.error("删除 Collection {} 失败: {}", name, exc)
            return toast_redirect(
                "/vector-db/", "vector_db.delete_failed", "error", lang="zh-CN"
            )
        return toast_redirect(
            "/vector-db/", "vector_db.collection_deleted", lang="zh-CN"
        )

    try:
        store = get_vector_store()
        old_metadata = {}
        try:
            old_collection = await asyncio.to_thread(
                store.client.get_collection, name=name
            )
            old_metadata = old_collection.metadata or {}
        except Exception:
            logger.warning("获取 Collection {} 元数据失败，将使用空元数据重建", name)

        await asyncio.to_thread(store.client.delete_collection, name=name)
        await asyncio.to_thread(
            store.client.create_collection, name=name, metadata=old_metadata
        )
        logger.info("Collection {} 已清空, by={}", name, user["sub"])
        await log_admin_action(
            db, user["user_id"], "vector_db_clear_collection", "collection", name
        )
    except Exception as exc:
        logger.error("清空 Collection {} 失败: {}", name, exc)
        return toast_redirect(
            f"/vector-db/collection/{name}",
            "vector_db.clear_failed",
            "error",
            lang="zh-CN",
        )
    return toast_redirect(
        f"/vector-db/collection/{name}",
        "vector_db.collection_cleared",
        lang="zh-CN",
    )


@router.post("/storage/{name}/{action}")
async def collection_action(
    name: str,
    action: Literal["clear", "delete"],
    request: Request,
    user=Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
    csrf=Depends(require_csrf_header),
):
    return action_result(await _collection_action(name, action, db, user))


class DocumentSelection(BaseModel):
    ids: list[str] = Field(min_length=1)


@router.post(
    "/storage/{name}/documents/delete", dependencies=[Depends(require_csrf_header)]
)
async def delete_documents(
    name: str,
    body: DocumentSelection,
    user=Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    collection = await asyncio.to_thread(
        get_vector_store().client.get_collection, name=name
    )
    await asyncio.to_thread(collection.delete, ids=body.ids)
    await log_admin_action(
        db,
        user["user_id"],
        "vector_db_delete_docs",
        "documents",
        name,
        {"requested_count": len(body.ids)},
    )
    return {"success": True}
