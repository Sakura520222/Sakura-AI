"""RAG 服务主类

协调所有 RAG 相关的服务，提供完整的文档索引和检索功能：
- 文档索引（增量更新）
- 语义检索
- 重排序
- Push vs Pull 混合策略
"""

from typing import List, Dict, Any, Optional
from loguru import logger
from datetime import datetime, timedelta

from backend.services.vector_store import get_vector_store
from backend.services.embedding_service import (
    get_embedding_service,
    get_reranker_service,
)
from backend.services.document_service import get_document_service
from backend.core.config import get_settings

settings = get_settings()


class RAGService:
    """RAG 服务主类

    协调向量存储、嵌入服务和文档管理，提供完整的 RAG 功能。
    """

    def __init__(self):
        """初始化 RAG 服务"""
        self.vector_store = get_vector_store()
        self.embedding_service = get_embedding_service()
        self.reranker_service = get_reranker_service()
        self.document_service = get_document_service()

        # 索引状态缓存
        self._index_status_cache = {}  # 格式: {repo_full_name: (data, expire_time)}
        self._cache_duration = timedelta(minutes=5)  # 缓存5分钟

    async def index_repository_docs(
        self,
        repo_full_name: str,
        repo_path: str,
        commit_hash: Optional[str] = None,
    ) -> Dict[str, int]:
        """索引仓库文档（增量更新）

        Args:
            repo_full_name: 仓库名称（如 "owner/repo"）
            repo_path: 仓库本地路径
            commit_hash: Git Commit Hash（用于增量更新判断）

        Returns:
            索引结果统计：
                - total_files: 总文件数
                - new_files: 新增文件数
                - updated_files: 更新文件数
                - deleted_files: 删除文件数
                - total_chunks: 总块数
        """
        try:
            logger.info(f"🔄 开始索引仓库文档: {repo_full_name}")

            # 1. 扫描文档文件
            files_info = await self.document_service.scan_sakura_directory(repo_path)

            if not files_info:
                logger.info(f"仓库 {repo_full_name} 中没有文档")
                return {
                    "total_files": 0,
                    "new_files": 0,
                    "updated_files": 0,
                    "deleted_files": 0,
                    "total_chunks": 0,
                }

            # TODO: 实现增量更新逻辑（对比数据库中的 file_hash）
            # 当前简化实现：完全重建索引

            # 2. 解析文档
            documents = await self.document_service.parse_markdown_documents(files_info)

            if not documents:
                logger.warning(f"没有成功解析任何文档: {repo_full_name}")
                return {
                    "total_files": len(files_info),
                    "new_files": 0,
                    "updated_files": 0,
                    "deleted_files": 0,
                    "total_chunks": 0,
                }

            # 3. 分块并准备索引
            chunks = await self.document_service.prepare_documents_for_indexing(
                documents
            )

            if not chunks:
                logger.warning(f"没有生成任何文档块: {repo_full_name}")
                return {
                    "total_files": len(files_info),
                    "new_files": 0,
                    "updated_files": 0,
                    "deleted_files": 0,
                    "total_chunks": 0,
                }

            # 4. 生成嵌入向量
            logger.info(f"🔄 正在生成 {len(chunks)} 个块嵌入向量...")
            texts = [chunk["content"] for chunk in chunks]
            embeddings = await self.embedding_service.embed_texts(texts)

            # 将嵌入向量添加到块数据中
            for i, chunk in enumerate(chunks):
                chunk["embedding"] = embeddings[i]

            # 5. 清空旧索引并添加新索引
            logger.info("🔄 正在更新向量库...")
            await self.vector_store.clear_collection(repo_full_name)
            added_count = await self.vector_store.add_documents(repo_full_name, chunks)

            # 6. 返回统计结果
            result = {
                "total_files": len(files_info),
                "new_files": len(files_info),  # 简化实现：全部视为新文件
                "updated_files": 0,
                "deleted_files": 0,
                "total_chunks": added_count,
            }

            logger.info(
                f"✅ 索引完成: {repo_full_name} - "
                f"{result['total_files']} 个文件, {result['total_chunks']} 个块"
            )

            # 清除索引状态缓存
            self._invalidate_index_cache(repo_full_name)

            return result

        except Exception as e:
            logger.error(f"❌ 索引失败 ({repo_full_name}): {e}")
            raise

    async def search_relevant_docs(
        self,
        repo_full_name: str,
        query: str,
        top_k: int = 5,
        use_rerank: bool = True,
    ) -> List[Dict[str, Any]]:
        """检索相关文档（RAG 流程）

        完整的检索流程：
        1. 生成查询向量
        2. 向量检索（召回）
        3. 重排序（可选）
        4. 返回最相关的文档

        Args:
            repo_full_name: 仓库名称
            query: 查询文本
            top_k: 返回结果数量
            use_rerank: 是否使用重排序

        Returns:
            相关文档列表，每个文档包含：
                - content: 文档内容
                - metadata: 元数据
                - score: 相关性分数（可选）
        """
        try:
            # 1. 生成查询向量
            query_embedding = await self.embedding_service.embed_query(query)

            # 2. 向量检索（召回阶段，获取更多候选）
            recall_k = top_k * 4  # 召回更多候选用于重排序
            candidates = await self.vector_store.search(
                repo_full_name, query_embedding, top_k=recall_k
            )

            if not candidates:
                logger.debug(
                    f"未找到相关文档: {repo_full_name}, query: {query[:50]}..."
                )
                return []

            logger.debug(f"召回 {len(candidates)} 个候选文档")

            # 3. 重排序（可选）
            if use_rerank and len(candidates) > top_k:
                # 准备重排序输入
                docs_for_rerank = [
                    {"content": doc["content"], "metadata": doc["metadata"]}
                    for doc in candidates
                ]

                # 执行重排序
                reranked_docs = await self.reranker_service.rerank(
                    query=query, docs=docs_for_rerank, top_k=top_k
                )

                if not reranked_docs:
                    # 重排序后所有文档都低于阈值，返回空列表
                    logger.debug("重排序后所有文档都低于阈值")
                    return []

                results = [
                    {
                        "content": doc["content"],
                        "metadata": doc["metadata"],
                    }
                    for doc in reranked_docs
                ]

                logger.debug(f"重排序后返回 {len(results)} 个文档")
                return results
            else:
                # 不使用重排序，直接返回 top_k 结果
                results = candidates[:top_k]
                logger.debug(f"返回前 {len(results)} 个召回结果")
                return [
                    {"content": doc["content"], "metadata": doc["metadata"]}
                    for doc in results
                ]

        except Exception as e:
            logger.error(f"❌ 检索失败 ({repo_full_name}): {e}")
            return []

    async def get_core_documents(
        self, repo_full_name: str, doc_types: List[str]
    ) -> List[Dict[str, Any]]:
        """获取核心文档（用于 Push 策略）

        获取特定类型的文档（如 review-rules、coding-standards）。

        Args:
            repo_full_name: 仓库名称
            doc_types: 文档类型列表（文件名模式）

        Returns:
            文档列表
        """
        try:
            # 生成检索查询
            query = " ".join(doc_types)

            # 检索相关文档
            docs = await self.search_relevant_docs(
                repo_full_name=repo_full_name,
                query=query,
                top_k=10,
                use_rerank=True,
            )

            # 过滤特定类型的文档
            filtered_docs = []
            for doc in docs:
                file_path = doc["metadata"].get("file_path", "")
                # 检查文件名是否匹配任何类型
                for doc_type in doc_types:
                    if doc_type in file_path.lower():
                        filtered_docs.append(doc)
                        break

            logger.debug(f"找到 {len(filtered_docs)} 个核心文档")
            return filtered_docs

        except Exception as e:
            logger.error(f"❌ 获取核心文档失败 ({repo_full_name}): {e}")
            return []

    async def get_index_status(self, repo_full_name: str) -> Dict[str, Any]:
        """获取索引状态（带缓存）

        Args:
            repo_full_name: 仓库名称

        Returns:
            索引状态信息
        """
        # 检查缓存
        if repo_full_name in self._index_status_cache:
            cached_data, expire_time = self._index_status_cache[repo_full_name]
            if datetime.now() < expire_time:
                logger.debug(f"使用缓存的索引状态: {repo_full_name}")
                return cached_data

        try:
            # 获取文档数量
            doc_count = await self.vector_store.get_collection_count(repo_full_name)
            result = {
                "repo_full_name": repo_full_name,
                "indexed": doc_count > 0,
                "document_count": doc_count,
                "last_indexed_at": None,  # TODO: 从数据库读取
            }

            # 更新缓存
            expire_time = datetime.now() + self._cache_duration
            self._index_status_cache[repo_full_name] = (result, expire_time)

            return result

        except Exception as e:
            logger.error(f"❌ 获取索引状态失败 ({repo_full_name}): {e}")
            return {
                "repo_full_name": repo_full_name,
                "indexed": False,
                "document_count": 0,
                "error": str(e),
            }

    def _invalidate_index_cache(self, repo_full_name: str) -> None:
        """清除指定仓库的索引状态缓存

        Args:
            repo_full_name: 仓库名称
        """
        if repo_full_name in self._index_status_cache:
            del self._index_status_cache[repo_full_name]
            logger.debug(f"已清除索引状态缓存: {repo_full_name}")

    async def delete_index(self, repo_full_name: str) -> bool:
        """删除仓库索引

        Args:
            repo_full_name: 仓库名称

        Returns:
            是否删除成功
        """
        try:
            success = await self.vector_store.delete_collection(repo_full_name)

            if success:
                logger.info(f"✅ 已删除仓库索引: {repo_full_name}")
                # 清除索引状态缓存
                self._invalidate_index_cache(repo_full_name)

            return success

        except Exception as e:
            logger.error(f"❌ 删除索引失败 ({repo_full_name}): {e}")
            return False


# 全局单例
_rag_service_instance: Optional[RAGService] = None


def get_rag_service() -> RAGService:
    """获取 RAG 服务单例"""
    global _rag_service_instance
    if _rag_service_instance is None:
        _rag_service_instance = RAGService()
    return _rag_service_instance
