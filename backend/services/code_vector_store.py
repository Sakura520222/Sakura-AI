"""代码向量存储管理服务

使用 ChromaDB 实现代码向量的存储和检索
与文档向量存储分离，使用独立的 Collection（添加 _code 后缀）
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


class CodeVectorStore:
    """代码向量存储管理器

    提供代码向量数据库的核心功能：
    - Collection 管理（与文档索引分离，使用 _code 后缀）
    - 代码块的增删改查
    - 向量相似度检索
    - 支持按语言、PR等元数据过滤
    """

    def __init__(self, persist_directory: Optional[str] = None):
        """初始化代码向量存储

        Args:
            persist_directory: ChromaDB 持久化目录，默认从配置读取
        """
        if chromadb is None:
            raise RuntimeError("ChromaDB 未安装，请运行: pip install chromadb")

        self.persist_directory = persist_directory or settings.chroma_persist_dir
        self.client = None
        self._collection_suffix = "_code"  # 代码Collection后缀
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
                    anonymized_telemetry=False,
                    allow_reset=True,
                ),
            )

            logger.info(f"✅ 代码向量存储初始化成功: {self.persist_directory}")

        except Exception as e:
            logger.error(f"❌ 代码向量存储初始化失败: {e}")
            raise

    def _get_code_collection_name(self, repo_full_name: str) -> str:
        """获取代码Collection名称

        在文档Collection名称基础上添加 _code 后缀

        Args:
            repo_full_name: 仓库名称，如 "owner/repo"

        Returns:
            代码Collection名称
        """
        # 复用相同的slugify逻辑，但添加后缀
        if len(repo_full_name) < 50:
            slug = repo_full_name.lower()
            slug = re.sub(r"[^a-z0-9._-]", "_", slug)
            slug = re.sub(r"\.{2,}", ".", slug)
            slug = slug.strip("._-")[:63]

            if not slug:
                slug = f"repo_{hashlib.md5(repo_full_name.encode()).hexdigest()[:8]}"

            # 添加 _code 后缀（如果不超过长度限制）
            if len(slug) + len(self._collection_suffix) <= 63:
                return f"{slug}{self._collection_suffix}"
            else:
                # 截断以容纳后缀
                max_slug_len = 63 - len(self._collection_suffix)
                return f"{slug[:max_slug_len]}{self._collection_suffix}"
        else:
            # 长名称使用 MD5
            return f"code_{hashlib.md5(repo_full_name.encode()).hexdigest()[:12]}"

    async def get_or_create_collection(self, repo_full_name: str):
        """获取或创建代码Collection

        Args:
            repo_full_name: 仓库名称，如 "owner/repo"

        Returns:
            ChromaDB Collection 对象
        """
        try:
            collection_name = self._get_code_collection_name(repo_full_name)

            collection = self.client.get_or_create_collection(
                name=collection_name,
                metadata={
                    "repo_full_name": repo_full_name,
                    "type": "code",  # 标记为代码Collection
                    "created_by": "sakura_ai_reviewer",
                },
            )

            logger.debug(f"代码Collection {collection_name} (repo: {repo_full_name}) 已就绪")
            return collection

        except Exception as e:
            logger.error(f"❌ 获取代码Collection失败 (repo: {repo_full_name}): {e}")
            raise

    async def add_code_chunks(
        self, repo_full_name: str, chunks: List[Dict[str, Any]]
    ) -> int:
        """向Collection添加代码块

        Args:
            repo_full_name: 仓库名称
            chunks: 代码块列表，每个代码块包含：
                - id: 唯一标识
                - content: 代码内容
                - embedding: 向量嵌入（可选）
                - metadata: 元数据（包含 language, file_path, function_name 等）

        Returns:
            添加的代码块数量
        """
        try:
            collection = await self.get_or_create_collection(repo_full_name)

            # 准备数据
            ids = [chunk["id"] for chunk in chunks]
            embeddings = [chunk.get("embedding") for chunk in chunks]
            documents_text = [chunk["content"] for chunk in chunks]
            metadatas = [chunk.get("metadata", {}) for chunk in chunks]

            # 如果所有 embeddings 都是 None，则不提供（让 ChromaDB 自动生成）
            if all(e is None for e in embeddings):
                embeddings = None

            # 添加代码块
            collection.add(
                ids=ids,
                embeddings=embeddings,
                documents=documents_text,
                metadatas=metadatas,
            )

            logger.info(
                f"✅ 已添加 {len(chunks)} 个代码块到 {repo_full_name} 的代码向量库"
            )
            return len(chunks)

        except Exception as e:
            logger.error(f"❌ 添加代码块失败 (repo: {repo_full_name}): {e}")
            raise

    async def search_code(
        self,
        repo_full_name: str,
        query_embedding: List[float],
        top_k: int = 5,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """代码向量相似度检索

        Args:
            repo_full_name: 仓库名称
            query_embedding: 查询向量
            top_k: 返回前 K 个结果
            where: 过滤条件（如 language, pr_number 等）

        Returns:
            检索结果列表，每个结果包含：
                - id: 代码块 ID
                - content: 代码内容
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
            code_chunks = []
            if results and results["ids"] and results["ids"][0]:
                for i, chunk_id in enumerate(results["ids"][0]):
                    code_chunks.append(
                        {
                            "id": chunk_id,
                            "content": results["documents"][0][i],
                            "metadata": results["metadatas"][0][i],
                            "distance": results["distances"][0][i],
                        }
                    )

            logger.debug(f"从 {repo_full_name} 代码库检索到 {len(code_chunks)} 个结果")
            return code_chunks

        except Exception as e:
            logger.error(f"❌ 代码向量检索失败 (repo: {repo_full_name}): {e}")
            return []

    async def delete_by_file(self, repo_full_name: str, file_path: str) -> int:
        """删除指定文件的所有代码块

        用于文件删除时的Tombstone清理

        Args:
            repo_full_name: 仓库名称
            file_path: 文件路径

        Returns:
            删除的代码块数量
        """
        try:
            collection = await self.get_or_create_collection(repo_full_name)

            # 查询该文件的所有代码块ID
            results = collection.get(
                where={"file_path": file_path},
                include=["documents"],
            )

            if results and results["ids"]:
                collection.delete(ids=results["ids"])
                logger.info(
                    f"✅ 已删除文件 {file_path} 的 {len(results['ids'])} 个代码块"
                )
                return len(results["ids"])

            return 0

        except Exception as e:
            logger.error(f"❌ 删除文件代码块失败 (repo: {repo_full_name}, file: {file_path}): {e}")
            return 0

    async def delete_by_pr(self, repo_full_name: str, pr_number: int) -> int:
        """删除指定PR的所有代码块

        用于PR关闭时的清理

        Args:
            repo_full_name: 仓库名称
            pr_number: PR编号

        Returns:
            删除的代码块数量
        """
        try:
            collection = await self.get_or_create_collection(repo_full_name)

            results = collection.get(
                where={"pr_number": str(pr_number)},
                include=["documents"],
            )

            if results and results["ids"]:
                collection.delete(ids=results["ids"])
                logger.info(
                    f"✅ 已删除 PR #{pr_number} 的 {len(results['ids'])} 个代码块"
                )
                return len(results["ids"])

            return 0

        except Exception as e:
            logger.error(f"❌ 删除PR代码块失败 (repo: {repo_full_name}, pr: {pr_number}): {e}")
            return 0

    async def clear_collection(self, repo_full_name: str) -> bool:
        """清空代码Collection

        Args:
            repo_full_name: 仓库名称

        Returns:
            是否清空成功
        """
        try:
            collection_name = self._get_code_collection_name(repo_full_name)

            try:
                self.client.delete_collection(name=collection_name)
                await self.get_or_create_collection(repo_full_name)
                logger.info(f"✅ 已清空代码Collection: {collection_name}")
                return True
            except Exception as e:
                logger.error(f"❌ 清空代码Collection失败: {e}")
                return False

        except Exception as e:
            logger.error(f"❌ 清空代码Collection失败 (repo: {repo_full_name}): {e}")
            return False

    async def get_collection_count(self, repo_full_name: str) -> int:
        """获取代码Collection中的代码块数量

        Args:
            repo_full_name: 仓库名称

        Returns:
            代码块数量
        """
        try:
            collection = await self.get_or_create_collection(repo_full_name)
            return collection.count()
        except Exception as e:
            logger.error(f"❌ 获取代码块数量失败 (repo: {repo_full_name}): {e}")
            return 0

    async def upsert_code_chunks(
        self, repo_full_name: str, chunks: List[Dict[str, Any]]
    ) -> int:
        """插入或更新代码块

        实现幂等性：相同ID的代码块会被更新而非重复添加

        Args:
            repo_full_name: 仓库名称
            chunks: 代码块列表

        Returns:
            添加/更新的代码块数量
        """
        try:
            collection = await self.get_or_create_collection(repo_full_name)

            ids = [chunk["id"] for chunk in chunks]
            embeddings = [chunk.get("embedding") for chunk in chunks]
            documents_text = [chunk["content"] for chunk in chunks]
            metadatas = [chunk.get("metadata", {}) for chunk in chunks]

            if all(e is None for e in embeddings):
                embeddings = None

            # 使用 upsert 而非 add
            collection.upsert(
                ids=ids,
                embeddings=embeddings,
                documents=documents_text,
                metadatas=metadatas,
            )

            logger.info(
                f"✅ 已更新 {len(chunks)} 个代码块到 {repo_full_name} 的代码向量库"
            )
            return len(chunks)

        except Exception as e:
            logger.error(f"❌ 更新代码块失败 (repo: {repo_full_name}): {e}")
            raise


# 全局单例
_code_vector_store_instance: Optional[CodeVectorStore] = None


def get_code_vector_store() -> CodeVectorStore:
    """获取代码向量存储单例"""
    global _code_vector_store_instance
    if _code_vector_store_instance is None:
        _code_vector_store_instance = CodeVectorStore()
    return _code_vector_store_instance
