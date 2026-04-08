"""向量存储管理服务

使用 ChromaDB 实现向量数据库的存储和检索
支持多租户隔离，每个仓库使用独立的 Collection
"""

from typing import List, Dict, Any, Optional
from loguru import logger
import hashlib
import re
from pathlib import Path

try:
    import chromadb
    from chromadb.config import Settings
except ImportError:
    chromadb = None
    logger.warning("ChromaDB 未安装，请运行: pip install chromadb")

from backend.core.config import get_settings

settings = get_settings()


class VectorStore:
    """向量存储管理器

    提供向量数据库的核心功能：
    - Collection 管理（多租户隔离）
    - 文档的增删改查
    - 向量相似度检索
    """

    def __init__(self, persist_directory: Optional[str] = None):
        """初始化向量存储

        Args:
            persist_directory: ChromaDB 持久化目录，默认从配置读取
        """
        if chromadb is None:
            raise RuntimeError("ChromaDB 未安装，请运行: pip install chromadb")

        self.persist_directory = persist_directory or settings.chroma_persist_dir
        self.client = None
        self._init_client()

    def _init_client(self):
        """初始化 ChromaDB 客户端"""
        try:
            # 确保持久化目录存在
            Path(self.persist_directory).mkdir(parents=True, exist_ok=True)

            # 初始化 ChromaDB 客户端
            self.client = chromadb.PersistentClient(
                path=self.persist_directory,
                settings=Settings(
                    anonymized_telemetry=False,  # 禁用匿名遥测
                    allow_reset=True,  # 允许重置数据库
                ),
            )

            logger.info(f"✅ ChromaDB 客户端初始化成功: {self.persist_directory}")

        except Exception as e:
            logger.error(f"❌ ChromaDB 客户端初始化失败: {e}")
            raise

    def _slugify_collection_name(self, repo_full_name: str) -> str:
        """将仓库名转换为符合 ChromaDB 规约的 Collection 名

        ChromaDB 限制：
        - 长度：3-63 字符
        - 字符：字母/数字开头，可包含 . _ -
        - 不能有两个连续的点

        策略：
        - 短名称（<50字符）：slugify 处理
        - 长名称（>=50字符）：使用 MD5 hash

        Args:
            repo_full_name: 仓库名称，如 "owner/repo"

        Returns:
            符合 ChromaDB 规约的 Collection 名称
        """
        # 如果仓库名较短，进行 slugify
        if len(repo_full_name) < 50:
            # 转换为合法字符
            slug = repo_full_name.lower()
            slug = re.sub(r"[^a-z0-9._-]", "_", slug)
            slug = re.sub(r"\.{2,}", ".", slug)  # 避免连续点
            slug = slug.strip("._-")[:63]  # 限制长度

            # 如果转换后为空，使用 fallback
            if not slug:
                slug = f"repo_{hashlib.md5(repo_full_name.encode()).hexdigest()[:8]}"

            return slug
        else:
            # 长名称使用 MD5（前 12 位）
            return f"repo_{hashlib.md5(repo_full_name.encode()).hexdigest()[:12]}"

    async def get_or_create_collection(self, repo_full_name: str):
        """获取或创建 Collection（多租户隔离）

        每个仓库使用独立的 Collection，实现物理隔离。

        Args:
            repo_full_name: 仓库名称，如 "owner/repo"

        Returns:
            ChromaDB Collection 对象
        """
        try:
            # 转换仓库名为合法的 Collection 名
            collection_name = self._slugify_collection_name(repo_full_name)

            # 获取或创建 Collection
            collection = self.client.get_or_create_collection(
                name=collection_name,
                metadata={
                    "repo_full_name": repo_full_name,  # 保存原始仓库名
                    "created_by": "sakura_ai_reviewer",
                },
            )

            logger.debug(
                f"Collection {collection_name} (repo: {repo_full_name}) 已就绪"
            )
            return collection

        except Exception as e:
            logger.error(f"❌ 获取 Collection 失败 (repo: {repo_full_name}): {e}")
            raise

    async def add_documents(
        self, repo_full_name: str, documents: List[Dict[str, Any]]
    ) -> int:
        """向 Collection 添加文档

        Args:
            repo_full_name: 仓库名称
            documents: 文档列表，每个文档包含：
                - id: 文档唯一标识
                - content: 文档内容
                - embedding: 向量嵌入（可选，如果不提供会自动生成）
                - metadata: 元数据字典

        Returns:
            添加的文档数量
        """
        try:
            collection = await self.get_or_create_collection(repo_full_name)

            # 准备数据
            ids = [doc["id"] for doc in documents]
            embeddings = [doc.get("embedding") for doc in documents]
            documents_text = [doc["content"] for doc in documents]
            metadatas = [doc.get("metadata", {}) for doc in documents]

            # 如果所有 embeddings 都是 None，则不提供（让 ChromaDB 自动生成）
            if all(e is None for e in embeddings):
                embeddings = None

            # 添加文档
            collection.add(
                ids=ids,
                embeddings=embeddings,
                documents=documents_text,
                metadatas=metadatas,
            )

            logger.info(
                f"✅ 已添加 {len(documents)} 个文档到 {repo_full_name} 的向量库"
            )
            return len(documents)

        except Exception as e:
            logger.error(f"❌ 添加文档失败 (repo: {repo_full_name}): {e}")
            raise

    async def search(
        self,
        repo_full_name: str,
        query_embedding: List[float],
        top_k: int = 20,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """向量相似度检索

        Args:
            repo_full_name: 仓库名称
            query_embedding: 查询向量
            top_k: 返回前 K 个结果
            where: 过滤条件（可选）

        Returns:
            检索结果列表，每个结果包含：
                - id: 文档 ID
                - content: 文档内容
                - metadata: 元数据
                - distance: 相似度距离
        """
        try:
            collection = await self.get_or_create_collection(repo_full_name)

            # 执行检索
            results = collection.query(
                query_embeddings=[query_embedding],
                n_results=top_k,
                where=where,
                include=["documents", "metadatas", "distances"],
            )

            # 解析结果
            documents = []
            if results and results["ids"] and results["ids"][0]:
                for i, doc_id in enumerate(results["ids"][0]):
                    documents.append(
                        {
                            "id": doc_id,
                            "content": results["documents"][0][i],
                            "metadata": results["metadatas"][0][i],
                            "distance": results["distances"][0][i],
                        }
                    )

            logger.debug(f"从 {repo_full_name} 检索到 {len(documents)} 个结果")
            return documents

        except Exception as e:
            logger.error(f"❌ 向量检索失败 (repo: {repo_full_name}): {e}")
            return []

    async def delete_collection(self, repo_full_name: str) -> bool:
        """删除仓库的 Collection

        Args:
            repo_full_name: 仓库名称

        Returns:
            是否删除成功
        """
        try:
            collection_name = self._slugify_collection_name(repo_full_name)

            # 检查 Collection 是否存在
            try:
                self.client.delete_collection(name=collection_name)
                logger.info(
                    f"✅ 已删除 Collection: {collection_name} (repo: {repo_full_name})"
                )
                return True
            except Exception as e:
                logger.warning(f"Collection 不存在或删除失败: {e}")
                return False

        except Exception as e:
            logger.error(f"❌ 删除 Collection 失败 (repo: {repo_full_name}): {e}")
            return False

    async def get_collection_count(self, repo_full_name: str) -> int:
        """获取 Collection 中的文档数量

        Args:
            repo_full_name: 仓库名称

        Returns:
            文档数量
        """
        try:
            collection = await self.get_or_create_collection(repo_full_name)
            return collection.count()
        except Exception as e:
            logger.error(f"❌ 获取文档数量失败 (repo: {repo_full_name}): {e}")
            return 0

    async def delete_documents(self, repo_full_name: str, doc_ids: List[str]) -> bool:
        """从 Collection 中删除指定文档

        Args:
            repo_full_name: 仓库名称
            doc_ids: 要删除的文档 ID 列表

        Returns:
            是否删除成功
        """
        try:
            collection = await self.get_or_create_collection(repo_full_name)
            collection.delete(ids=doc_ids)
            logger.debug(
                f"已从 {repo_full_name} 删除 {len(doc_ids)} 个文档"
            )
            return True
        except Exception as e:
            logger.warning(f"删除文档失败 (repo: {repo_full_name}): {e}")
            return False

    async def upsert_documents(
        self, repo_full_name: str, documents: List[Dict[str, Any]]
    ) -> int:
        """更新或插入文档（先删后加）

        Args:
            repo_full_name: 仓库名称
            documents: 文档列表，格式同 add_documents

        Returns:
            更新的文档数量
        """
        try:
            collection = await self.get_or_create_collection(repo_full_name)
            doc_ids = [doc["id"] for doc in documents]

            # 先尝试删除旧文档（可能不存在）
            try:
                collection.delete(ids=doc_ids)
            except Exception:
                pass

            # 再添加新文档
            return await self.add_documents(repo_full_name, documents)
        except Exception as e:
            logger.error(f"❌ upsert 文档失败 (repo: {repo_full_name}): {e}")
            raise

    async def clear_collection(self, repo_full_name: str) -> bool:
        """清空 Collection 中的所有文档

        Args:
            repo_full_name: 仓库名称

        Returns:
            是否清空成功
        """
        try:
            # 删除后重建是清空的最简单方法
            collection_name = self._slugify_collection_name(repo_full_name)

            try:
                self.client.delete_collection(name=collection_name)
                # 重新创建
                await self.get_or_create_collection(repo_full_name)
                logger.info(
                    f"✅ 已清空 Collection: {collection_name} (repo: {repo_full_name})"
                )
                return True
            except Exception as e:
                logger.error(f"❌ 清空 Collection 失败: {e}")
                return False

        except Exception as e:
            logger.error(f"❌ 清空 Collection 失败 (repo: {repo_full_name}): {e}")
            return False


# 全局单例
_vector_store_instance: Optional[VectorStore] = None


def get_vector_store() -> VectorStore:
    """获取向量存储单例"""
    global _vector_store_instance
    if _vector_store_instance is None:
        _vector_store_instance = VectorStore()
    return _vector_store_instance
