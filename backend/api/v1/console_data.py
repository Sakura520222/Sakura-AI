"""Native console data and actions for repository memory and vector storage."""

import asyncio
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import require_api_super_admin
from backend.api.v1.private_routes import PrivateAPIRoute
from backend.core.time_service import format_rfc3339
from backend.models.database import SakuraMemoryState
from backend.services.vector_store import get_vector_store
from backend.webui.deps import get_db, require_csrf_header
from backend.webui.helpers.admin_log import log_admin_action
from backend.webui.routes import sakura_memory as memory, vector_db as storage

router = APIRouter(
    prefix="/console", tags=["Console Data"], route_class=PrivateAPIRoute
)


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
                "extraction": memory._get_knowledge_extraction_status(row),
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
        content = await memory._read_sakura_file(repo, ".sakura/" + relative_file(file))
        if content is None:
            raise HTTPException(404, "file_unavailable")
        return {"content": content, "file": file}
    return {
        "files": await memory._list_sakura_files(repo),
        "extraction": memory._get_knowledge_extraction_status(row),
    }


class MemoryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    repo: str
    file: str
    content: str = Field(max_length=memory.MAX_SAKURA_FILE_CONTENT_LENGTH)


@router.put("/memory/file", dependencies=[Depends(require_csrf_header)])
async def write_memory(
    body: MemoryWrite,
    user=Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    path = relative_file(body.file)
    repo = await asyncio.to_thread(memory._get_repo, body.repo)
    if repo is None:
        raise HTTPException(404, "repository_unavailable")
    await memory._get_write_service().commit_files(
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
    return action_result(
        await memory.delete_file(
            repo, relative_file(file), request, db=db, user=user, csrf_token=csrf
        )
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
    handler = (
        memory.trigger_consolidate
        if action == "consolidate"
        else memory.trigger_extract
    )
    return action_result(
        await handler(repo, request, db=db, user=user, csrf_token=csrf)
    )


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

    return {"items": await asyncio.to_thread(read)}


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


@router.post("/storage/{name}/{action}")
async def collection_action(
    name: str,
    action: Literal["clear", "delete"],
    request: Request,
    user=Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
    csrf=Depends(require_csrf_header),
):
    handler = (
        storage.clear_collection if action == "clear" else storage.delete_collection
    )
    return action_result(
        await handler(
            name,
            request,
            db=db,
            user=user,
            csrf_token=csrf,
            user_prefs={"language": "zh-CN"},
        )
    )


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
