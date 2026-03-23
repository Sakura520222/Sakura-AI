"""代码索引服务

提供PR代码文件和仓库代码的索引功能：
- 索引PR变更文件
- 索引仓库代码
- 增量更新（基于文件Hash）
- 代码检索
"""

from typing import List, Dict, Any, Optional
from loguru import logger
import hashlib
from pathlib import Path
from datetime import datetime

from backend.services.code_parser_service import CodeParserService, get_code_parser
from backend.services.code_vector_store import CodeVectorStore, get_code_vector_store
from backend.services.embedding_service import EmbeddingService, get_embedding_service
from backend.models.database import (
    CodeIndex,
    CodeFile,
    CodeIndexingStatus,
    async_session,
)
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession


class CodeIndexService:
    """代码索引服务

    协调代码解析、向量化和存储
    """

    def __init__(
        self,
        parser: Optional[CodeParserService] = None,
        vector_store: Optional[CodeVectorStore] = None,
        embedding_service: Optional[EmbeddingService] = None,
    ):
        """初始化代码索引服务

        Args:
            parser: 代码解析服务
            vector_store: 代码向量存储
            embedding_service: 嵌入向量服务
        """
        self.parser = parser or get_code_parser()
        self.vector_store = vector_store or get_code_vector_store()
        self.embedding_service = embedding_service or get_embedding_service()

    async def index_pr_changes(
        self,
        repo_full_name: str,
        pr_number: int,
        files: List[Dict[str, Any]],
        commit_sha: Optional[str] = None,
    ) -> Dict[str, Any]:
        """索引PR变更的文件

        Args:
            repo_full_name: 仓库名称（如 "owner/repo"）
            pr_number: PR编号
            files: 文件列表，每个文件包含：
                - path: 文件路径
                - content: 文件内容（可选，如果不提供则从仓库读取）
            commit_sha: Commit SHA（可选）

        Returns:
            索引结果统计
        """
        logger.info(f"开始索引PR #{pr_number}的代码文件，仓库: {repo_full_name}")

        indexed_count = 0
        skipped_count = 0
        failed_count = 0
        total_chunks = 0

        async with async_session() as session:
            for file_info in files:
                file_path = file_info["path"]
                content = file_info.get("content")

                if not content:
                    logger.warning(f"文件 {file_path} 没有内容，跳过索引")
                    skipped_count += 1
                    continue

                try:
                    # 计算文件Hash
                    file_hash = hashlib.sha256(content.encode()).hexdigest()

                    # 检查是否需要索引（幂等性）
                    existing = await self._get_code_file(
                        session, repo_full_name, file_path
                    )
                    if (
                        existing
                        and existing.file_hash == file_hash
                        and not existing.is_deleted
                    ):
                        logger.debug(f"文件 {file_path} 未变化，跳过索引")
                        skipped_count += 1
                        continue

                    # 解析代码
                    chunks = self.parser.parse_code_file(
                        file_path=file_path,
                        content=content,
                        repo_full_name=repo_full_name,
                        pr_number=pr_number,
                        commit_sha=commit_sha,
                    )

                    if not chunks:
                        logger.warning(f"文件 {file_path} 解析后没有生成代码块")
                        skipped_count += 1
                        continue

                    # 生成嵌入向量
                    chunk_texts = [chunk.content for chunk in chunks]
                    embeddings = await self.embedding_service.get_embeddings(
                        chunk_texts
                    )

                    # 准备向量存储数据
                    vector_chunks = []
                    for chunk, embedding in zip(chunks, embeddings):
                        vector_chunks.append(
                            {
                                "id": chunk.id,
                                "content": chunk.content,
                                "embedding": embedding,
                                "metadata": chunk.metadata,
                            }
                        )

                    # 存储到向量库
                    await self.vector_store.upsert_code_chunks(
                        repo_full_name, vector_chunks
                    )
                    total_chunks += len(chunks)

                    # 更新数据库记录
                    await self._upsert_code_file(
                        session=session,
                        repo_full_name=repo_full_name,
                        file_path=file_path,
                        file_hash=file_hash,
                        language=chunks[0].metadata.get("language"),
                        chunk_count=len(chunks),
                        pr_number=pr_number,
                        commit_sha=commit_sha,
                    )

                    indexed_count += 1
                    logger.debug(
                        f"✅ 已索引文件 {file_path}，生成 {len(chunks)} 个代码块"
                    )

                except Exception as e:
                    logger.error(f"❌ 索引文件 {file_path} 失败: {e}")
                    failed_count += 1

            # 更新索引状态
            await self._update_code_index_status(
                session=session,
                repo_full_name=repo_full_name,
                file_count=indexed_count,
                total_chunks=total_chunks,
                index_type="pr",
            )

            await session.commit()

        logger.info(
            f"PR #{pr_number} 索引完成: "
            f"索引={indexed_count}, 跳过={skipped_count}, 失败={failed_count}, "
            f"代码块={total_chunks}"
        )

        return {
            "indexed": indexed_count,
            "skipped": skipped_count,
            "failed": failed_count,
            "total_chunks": total_chunks,
        }

    async def index_repository_code(
        self,
        repo_full_name: str,
        repo_path: str,
        commit_sha: str,
        paths: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """索引仓库代码

        Args:
            repo_full_name: 仓库名称
            repo_path: 仓库本地路径
            commit_sha: Commit SHA
            paths: 要索引的路径列表（可选），默认索引所有支持的文件

        Returns:
            索引结果统计
        """
        logger.info(f"开始索引仓库代码，仓库: {repo_full_name}, 路径: {repo_path}")

        indexed_count = 0
        skipped_count = 0
        failed_count = 0
        total_chunks = 0

        repo_path_obj = Path(repo_path)
        if not repo_path_obj.exists():
            logger.error(f"仓库路径不存在: {repo_path}")
            return {"indexed": 0, "skipped": 0, "failed": 0, "total_chunks": 0}

        # 收集要索引的文件
        code_files = self._collect_code_files(repo_path_obj, paths)

        logger.info(f"找到 {len(code_files)} 个代码文件")

        async with async_session() as session:
            for file_path in code_files:
                try:
                    full_path = repo_path_obj / file_path
                    content = full_path.read_text(encoding="utf-8", errors="ignore")

                    # 计算文件Hash
                    file_hash = hashlib.sha256(content.encode()).hexdigest()

                    # 检查是否需要索引
                    existing = await self._get_code_file(
                        session, repo_full_name, str(file_path)
                    )
                    if (
                        existing
                        and existing.file_hash == file_hash
                        and not existing.is_deleted
                    ):
                        skipped_count += 1
                        continue

                    # 解析代码
                    chunks = self.parser.parse_code_file(
                        file_path=str(file_path),
                        content=content,
                        repo_full_name=repo_full_name,
                        commit_sha=commit_sha,
                    )

                    if not chunks:
                        skipped_count += 1
                        continue

                    # 生成嵌入向量
                    chunk_texts = [chunk.content for chunk in chunks]
                    embeddings = await self.embedding_service.get_embeddings(
                        chunk_texts
                    )

                    # 准备向量存储数据
                    vector_chunks = []
                    for chunk, embedding in zip(chunks, embeddings):
                        vector_chunks.append(
                            {
                                "id": chunk.id,
                                "content": chunk.content,
                                "embedding": embedding,
                                "metadata": chunk.metadata,
                            }
                        )

                    # 存储到向量库
                    await self.vector_store.upsert_code_chunks(
                        repo_full_name, vector_chunks
                    )
                    total_chunks += len(chunks)

                    # 更新数据库记录
                    await self._upsert_code_file(
                        session=session,
                        repo_full_name=repo_full_name,
                        file_path=str(file_path),
                        file_hash=file_hash,
                        language=chunks[0].metadata.get("language"),
                        chunk_count=len(chunks),
                        commit_sha=commit_sha,
                    )

                    indexed_count += 1

                except Exception as e:
                    logger.error(f"❌ 索引文件 {file_path} 失败: {e}")
                    failed_count += 1

            # 更新索引状态
            await self._update_code_index_status(
                session=session,
                repo_full_name=repo_full_name,
                commit_hash=commit_sha,
                file_count=indexed_count,
                total_chunks=total_chunks,
                index_type="full",
            )

            await session.commit()

        logger.info(
            f"仓库 {repo_full_name} 索引完成: "
            f"索引={indexed_count}, 跳过={skipped_count}, 失败={failed_count}, "
            f"代码块={total_chunks}"
        )

        return {
            "indexed": indexed_count,
            "skipped": skipped_count,
            "failed": failed_count,
            "total_chunks": total_chunks,
        }

    async def search_code_context(
        self,
        repo_full_name: str,
        query: str,
        top_k: int = 5,
        language: Optional[str] = None,
        file_path: Optional[str] = None,
        pr_number: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """检索代码上下文

        Args:
            repo_full_name: 仓库名称
            query: 查询文本
            top_k: 返回结果数量
            language: 语言过滤（可选）
            file_path: 文件路径过滤（可选）
            pr_number: PR编号过滤（可选）

        Returns:
            检索结果列表
        """
        # 生成查询向量
        query_embedding = await self.embedding_service.get_embeddings([query])
        if not query_embedding:
            return []

        # 构建过滤条件
        where_filters = {}
        if language:
            where_filters["language"] = language
        if file_path:
            where_filters["file_path"] = file_path
        if pr_number is not None:
            where_filters["pr_number"] = str(pr_number)

        # 执行检索
        results = await self.vector_store.search_code(
            repo_full_name=repo_full_name,
            query_embedding=query_embedding[0],
            top_k=top_k,
            where=where_filters if where_filters else None,
        )

        return results

    async def incremental_update(
        self,
        repo_full_name: str,
        repo_path: str,
        commit_sha: str,
    ) -> Dict[str, Any]:
        """增量更新索引

        只索引发生变化的文件

        Args:
            repo_full_name: 仓库名称
            repo_path: 仓库本地路径
            commit_sha: 新的Commit SHA

        Returns:
            更新结果统计
        """
        logger.info(f"开始增量更新索引，仓库: {repo_full_name}")

        async with async_session() as session:
            # 获取上次索引的Commit
            code_index = await session.execute(
                select(CodeIndex).where(CodeIndex.repo_full_name == repo_full_name)
            )
            code_index_obj = code_index.scalar_one_or_none()

            last_commit = code_index_obj.last_commit_hash if code_index_obj else None

            # TODO: 使用git diff获取变更文件列表
            # 这里简化处理，直接调用完整索引
            result = await self.index_repository_code(
                repo_full_name=repo_full_name,
                repo_path=repo_path,
                commit_sha=commit_sha,
            )

            return result

    async def delete_file_index(self, repo_full_name: str, file_path: str) -> bool:
        """删除文件的索引

        用于文件删除时的Tombstone清理

        Args:
            repo_full_name: 仓库名称
            file_path: 文件路径

        Returns:
            是否删除成功
        """
        try:
            # 从向量库删除
            deleted_count = await self.vector_store.delete_by_file(
                repo_full_name, file_path
            )

            # 标记数据库记录为已删除
            async with async_session() as session:
                result = await session.execute(
                    select(CodeFile).where(
                        and_(
                            CodeFile.repo_full_name == repo_full_name,
                            CodeFile.file_path == file_path,
                        )
                    )
                )
                code_file = result.scalar_one_or_none()

                if code_file:
                    code_file.is_deleted = 1
                    await session.commit()

            logger.info(f"✅ 已删除文件 {file_path} 的索引 ({deleted_count} 个代码块)")
            return True

        except Exception as e:
            logger.error(f"❌ 删除文件索引失败 (file: {file_path}): {e}")
            return False

    def _collect_code_files(
        self, repo_path: Path, paths: Optional[List[str]] = None
    ) -> List[Path]:
        """收集要索引的代码文件

        Args:
            repo_path: 仓库路径
            paths: 指定的路径列表（可选）

        Returns:
            文件路径列表
        """
        code_files = []
        supported_extensions = set()

        for extensions in CodeParserService.LANGUAGE_MAP.values():
            supported_extensions.update(extensions)

        if paths:
            # 指定路径
            for path_str in paths:
                path = repo_path / path_str
                if path.is_file():
                    code_files.append(path.relative_to(repo_path))
                elif path.is_dir():
                    for ext in supported_extensions:
                        code_files.extend(path.rglob(f"*{ext}"))
        else:
            # 全部文件
            for ext in supported_extensions:
                code_files.extend(repo_path.rglob(f"*{ext}"))

        # 去重并排序
        code_files = sorted(set(code_files))

        # 转换为相对路径字符串
        return [f.relative_to(repo_path) for f in code_files]

    async def _get_code_file(
        self, session: AsyncSession, repo_full_name: str, file_path: str
    ) -> Optional[CodeFile]:
        """获取代码文件记录

        Args:
            session: 数据库会话
            repo_full_name: 仓库名称
            file_path: 文件路径

        Returns:
            CodeFile对象或None
        """
        result = await session.execute(
            select(CodeFile).where(
                and_(
                    CodeFile.repo_full_name == repo_full_name,
                    CodeFile.file_path == file_path,
                )
            )
        )
        return result.scalar_one_or_none()

    async def _upsert_code_file(
        self,
        session: AsyncSession,
        repo_full_name: str,
        file_path: str,
        file_hash: str,
        language: Optional[str],
        chunk_count: int,
        pr_number: Optional[int] = None,
        commit_sha: Optional[str] = None,
    ):
        """插入或更新代码文件记录

        Args:
            session: 数据库会话
            repo_full_name: 仓库名称
            file_path: 文件路径
            file_hash: 文件Hash
            language: 语言类型
            chunk_count: 代码块数量
            pr_number: PR编号（可选）
            commit_sha: Commit SHA（可选）
        """
        existing = await self._get_code_file(session, repo_full_name, file_path)

        now = datetime.utcnow()

        if existing:
            # 更新
            existing.file_hash = file_hash
            existing.language = language
            existing.chunk_count = chunk_count
            existing.last_indexed_at = now
            existing.last_indexed_commit_hash = commit_sha
            existing.commit_sha = commit_sha
            existing.indexed = 1
            existing.is_deleted = 0
            if pr_number is not None:
                existing.pr_number = pr_number
        else:
            # 插入
            new_file = CodeFile(
                repo_full_name=repo_full_name,
                file_path=file_path,
                file_hash=file_hash,
                language=language,
                chunk_count=chunk_count,
                last_indexed_at=now,
                last_indexed_commit_hash=commit_sha,
                commit_sha=commit_sha,
                indexed=1,
                is_deleted=0,
                pr_number=pr_number,
            )
            session.add(new_file)

    async def _update_code_index_status(
        self,
        session: AsyncSession,
        repo_full_name: str,
        file_count: int,
        total_chunks: int,
        index_type: str = "full",
        commit_hash: Optional[str] = None,
    ):
        """更新代码索引状态

        Args:
            session: 数据库会话
            repo_full_name: 仓库名称
            file_count: 文件数量
            total_chunks: 代码块总数
            index_type: 索引类型
            commit_hash: Commit SHA（可选）
        """
        result = await session.execute(
            select(CodeIndex).where(CodeIndex.repo_full_name == repo_full_name)
        )
        code_index = result.scalar_one_or_none()

        now = datetime.utcnow()

        if code_index:
            code_index.file_count = file_count
            code_index.total_chunks = total_chunks
            code_index.last_indexed_at = now
            code_index.indexing_status = CodeIndexingStatus.COMPLETED.value
            code_index.index_type = index_type
            if commit_hash:
                code_index.last_commit_hash = commit_hash
        else:
            new_index = CodeIndex(
                repo_full_name=repo_full_name,
                last_commit_hash=commit_hash,
                last_indexed_at=now,
                file_count=file_count,
                total_chunks=total_chunks,
                indexing_status=CodeIndexingStatus.COMPLETED.value,
                index_type=index_type,
            )
            session.add(new_index)


# 全局单例
_code_index_service_instance: Optional[CodeIndexService] = None


def get_code_index_service() -> CodeIndexService:
    """获取代码索引服务单例"""
    global _code_index_service_instance
    if _code_index_service_instance is None:
        _code_index_service_instance = CodeIndexService()
    return _code_index_service_instance
