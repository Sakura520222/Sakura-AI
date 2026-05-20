"""向量存储与数据库管理 WebUI 路由（超级管理员专用）

提供 ChromaDB 向量存储库的查看、删除、清空等管理操作，
以及数据库连接状态检查。仅对超级管理员可见和可用。
"""

import asyncio

from fastapi import APIRouter, Request, Depends
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.vector_store import get_vector_store
from backend.webui.deps import (
    require_super_admin,
    get_db,
    get_csrf_serializer,
    require_csrf,
    require_csrf_header,
    get_user_preferences,
    toast_redirect,
    render_template,
)
from backend.webui.helpers.admin_log import log_admin_action

# CSRF strategy: JSON API endpoints use header-based CSRF (require_csrf_header),
# while form-submission endpoints use form-based CSRF (require_csrf).
from backend.webui.i18n import detect_language

router = APIRouter(prefix="/vector-db", tags=["WebUI Vector DB"])


def _get_collections_info():
    """获取所有 ChromaDB Collection 的摘要信息。

    Returns:
        list[dict]: 每个 Collection 的名称、文档数量和元数据
    """
    try:
        vs = get_vector_store()
        collections = vs.client.list_collections()
        result = []
        for col in collections:
            try:
                count = col.count()
                metadata = col.metadata or {}
                result.append(
                    {
                        "name": col.name,
                        "repo_full_name": metadata.get("repo_full_name", ""),
                        "doc_count": count,
                        "metadata": metadata,
                    }
                )
            except Exception as e:
                logger.warning("读取 Collection {} 信息失败: {}", col.name, e)
                result.append(
                    {
                        "name": col.name,
                        "repo_full_name": "",
                        "doc_count": -1,
                        "metadata": {},
                    }
                )
        return result
    except Exception as e:
        logger.error("获取 Collection 列表失败: {}", e)
        return []


def _get_collection_detail(collection_name: str):
    """获取单个 Collection 的详细信息，包括文档样本。

    Args:
        collection_name: ChromaDB Collection 名称

    Returns:
        dict | None: Collection 详情，不存在时返回 None
    """
    try:
        vs = get_vector_store()
        collection = vs.client.get_collection(name=collection_name)
        count = collection.count()

        # 获取前 50 个文档作为样本
        sample_docs = []
        if count > 0:
            peek_result = collection.peek(limit=50)
            ids = peek_result.get("ids", [])
            documents = peek_result.get("documents", [])
            metadatas = peek_result.get("metadatas", [])
            for i, doc_id in enumerate(ids):
                sample_docs.append(
                    {
                        "id": doc_id,
                        "content": (
                            documents[i][:500]
                            if i < len(documents) and documents[i]
                            else ""
                        ),
                        "metadata": metadatas[i] if i < len(metadatas) else {},
                    }
                )

        return {
            "name": collection.name,
            "metadata": collection.metadata or {},
            "doc_count": count,
            "sample_docs": sample_docs,
        }
    except Exception as e:
        logger.warning("获取 Collection {} 详情失败: {}", collection_name, e)
        return None


# ========== GET: 向量存储管理主页 ==========


@router.get("/")
async def vector_db_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """渲染向量存储管理主页，列出所有 Collection"""
    collections = await asyncio.to_thread(_get_collections_info)

    return render_template(
        "vector_db.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="vector_db",
        collections=collections,
        selected_collection=None,
        collection_detail=None,
    )


# ========== GET: 查看 Collection 详情 ==========


@router.get("/collection/{collection_name}")
async def view_collection(
    collection_name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """查看指定 Collection 的详细信息和文档样本"""
    collections = await asyncio.to_thread(_get_collections_info)
    detail = await asyncio.to_thread(_get_collection_detail, collection_name)

    if detail is None:
        return toast_redirect(
            "/vector-db/",
            "vector_db.collection_not_found",
            "error",
            lang=detect_language(user_prefs),
        )

    return render_template(
        "vector_db.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="vector_db",
        collections=collections,
        selected_collection=collection_name,
        collection_detail=detail,
    )


# ========== POST: 删除 Collection ==========


@router.post("/collection/{collection_name}/delete")
async def delete_collection(
    collection_name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    user_prefs: dict = Depends(get_user_preferences),
):
    """删除指定的 Collection 及其所有文档"""
    try:
        vs = get_vector_store()
        await asyncio.to_thread(vs.client.delete_collection, name=collection_name)

        logger.info("Collection {} 已删除, by={}", collection_name, user["sub"])
        await log_admin_action(
            db,
            user["user_id"],
            "vector_db_delete_collection",
            "collection",
            collection_name,
        )
    except Exception as e:
        logger.error("删除 Collection {} 失败: {}", collection_name, e)
        return toast_redirect(
            "/vector-db/",
            "vector_db.delete_failed",
            "error",
            lang=detect_language(user_prefs),
        )

    return toast_redirect(
        "/vector-db/",
        "vector_db.collection_deleted",
        lang=detect_language(user_prefs),
    )


# ========== POST: 清空 Collection ==========


@router.post("/collection/{collection_name}/clear")
async def clear_collection(
    collection_name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    user_prefs: dict = Depends(get_user_preferences),
):
    """清空指定 Collection 的所有文档（保留 Collection 本身）"""
    try:
        vs = get_vector_store()
        # 删除后重建，先获取旧元数据
        old_metadata = {}
        try:
            old_col = await asyncio.to_thread(
                vs.client.get_collection, name=collection_name
            )
            old_metadata = old_col.metadata or {}
        except Exception:
            logger.warning(
                "获取 Collection {} 元数据失败，将使用空元数据重建", collection_name
            )

        await asyncio.to_thread(vs.client.delete_collection, name=collection_name)
        await asyncio.to_thread(
            vs.client.create_collection,
            name=collection_name,
            metadata=old_metadata,
        )

        logger.info("Collection {} 已清空, by={}", collection_name, user["sub"])
        await log_admin_action(
            db,
            user["user_id"],
            "vector_db_clear_collection",
            "collection",
            collection_name,
        )
    except Exception as e:
        logger.error("清空 Collection {} 失败: {}", collection_name, e)
        return toast_redirect(
            f"/vector-db/collection/{collection_name}",
            "vector_db.clear_failed",
            "error",
            lang=detect_language(user_prefs),
        )

    return toast_redirect(
        f"/vector-db/collection/{collection_name}",
        "vector_db.collection_cleared",
        lang=detect_language(user_prefs),
    )


# ========== POST: 删除 Collection 中的指定文档 ==========


@router.post("/collection/{collection_name}/documents/delete")
async def delete_documents(
    collection_name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    user_prefs: dict = Depends(get_user_preferences),
):
    """从 Collection 中删除指定的文档"""
    form = await request.form()
    doc_ids_raw = form.get("doc_ids", "")
    if not doc_ids_raw or not str(doc_ids_raw).strip():
        return toast_redirect(
            f"/vector-db/collection/{collection_name}",
            "vector_db.no_docs_selected",
            "error",
            lang=detect_language(user_prefs),
        )

    doc_ids = [did.strip() for did in str(doc_ids_raw).split(",") if did.strip()]
    if not doc_ids:
        return toast_redirect(
            f"/vector-db/collection/{collection_name}",
            "vector_db.no_docs_selected",
            "error",
            lang=detect_language(user_prefs),
        )

    try:
        vs = get_vector_store()
        collection = await asyncio.to_thread(
            vs.client.get_collection, name=collection_name
        )
        await asyncio.to_thread(collection.delete, ids=doc_ids)

        logger.info(
            "从 Collection {} 删除 {} 个文档, by={}",
            collection_name,
            len(doc_ids),
            user["sub"],
        )
        await log_admin_action(
            db,
            user["user_id"],
            "vector_db_delete_docs",
            "documents",
            collection_name,
            {"deleted_count": len(doc_ids)},
        )
    except Exception as e:
        logger.error("删除文档失败: {}", e)
        return toast_redirect(
            f"/vector-db/collection/{collection_name}",
            "vector_db.delete_failed",
            "error",
            lang=detect_language(user_prefs),
        )

    return toast_redirect(
        f"/vector-db/collection/{collection_name}",
        "vector_db.docs_deleted",
        lang=detect_language(user_prefs),
    )


# ========== POST: 测试数据库连接 ==========


@router.post("/test-connection")
async def test_db_connection(
    request: Request,
    user: dict = Depends(require_super_admin),
    _csrf: str = Depends(require_csrf_header),
    user_prefs: dict = Depends(get_user_preferences),
):
    """测试 MySQL 数据库连接"""
    try:
        from backend.core.setup_service import setup_service
        from backend.core.config import get_settings

        settings = get_settings()
        result = await setup_service.test_database_connection(settings.database_url)
        return {
            "success": result.get("success", False),
            "message": result.get("message", ""),
        }
    except Exception as e:
        logger.error("测试数据库连接失败: {}", e)
        return {"success": False, "message": str(e)}


# ========== POST: 测试 ChromaDB 连接 ==========


@router.post("/test-chromadb")
async def test_chromadb(
    request: Request,
    user: dict = Depends(require_super_admin),
    _csrf: str = Depends(require_csrf_header),
    user_prefs: dict = Depends(get_user_preferences),
):
    """测试 ChromaDB 向量数据库连接"""
    try:
        vs = get_vector_store()
        collections = await asyncio.to_thread(vs.client.list_collections)
        return {
            "success": True,
            "message": f"ChromaDB 连接正常，共有 {len(collections)} 个 Collection",
            "data": {"collection_count": len(collections)},
        }
    except Exception as e:
        logger.error("测试 ChromaDB 连接失败: {}", e)
        return {"success": False, "message": str(e)}
