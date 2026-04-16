"""代码索引服务

提供PR代码文件和仓库代码的索引功能：
- 索引PR变更文件
- 索引仓库代码
- 增量更新（基于文件Hash）
- 代码检索
"""

from typing import List, Dict, Any, Optional
from loguru import logger
import asyncio
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
        removed_count = 0
        total_chunks = 0
        consecutive_failures = 0

        async with async_session() as session:
            for file_info in files:
                # 连续失败 >= 3 次，提前终止索引
                if consecutive_failures >= 3:
                    remaining = len(files) - (
                        indexed_count + skipped_count + failed_count
                        + removed_count
                    )
                    if remaining > 0:
                        logger.warning(
                            f"⚠️  Embedding 服务连续失败 {consecutive_failures} 次，"
                            f"跳过剩余 {remaining} 个文件"
                        )
                        failed_count += remaining
                    break
                file_path = file_info["path"]
                file_status = file_info.get("status", "modified")
                content = file_info.get("content")

                # 处理 removed 状态的文件，清理旧索引
                if file_status == "removed":
                    try:
                        await self.vector_store.delete_by_file(
                            repo_full_name, file_path
                        )
                        existing = await self._get_code_file(
                            session, repo_full_name, file_path
                        )
                        if existing:
                            existing.is_deleted = 1
                        removed_count += 1
                        logger.info(f"已清理删除文件的索引: {file_path}")
                    except Exception as e:
                        logger.error(
                            f"清理删除文件索引失败 ({file_path}): {e}"
                        )
                        failed_count += 1
                    consecutive_failures = 0
                    continue

                if not content:
                    logger.warning(f"文件 {file_path} 没有内容，跳过索引")
                    skipped_count += 1
                    consecutive_failures = 0
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

                    # 文件 hash 变化时清理旧代码块
                    if existing and existing.file_hash != file_hash:
                        await self._cleanup_stale_file_chunks(
                            repo_full_name, file_path
                        )

                    # 解析代码
                    chunks = self.parser.parse_code_file(
                        file_path=file_path,
                        content=content,
                        repo_full_name=repo_full_name,
                        pr_number=pr_number,
                        commit_sha=commit_sha,
                        file_content_hash=file_hash[:12],
                    )

                    if not chunks:
                        logger.warning(f"文件 {file_path} 解析后没有生成代码块")
                        skipped_count += 1
                        continue

                    # 生成嵌入向量
                    chunk_texts = [chunk.content for chunk in chunks]
                    embeddings = await self.embedding_service.embed_texts(chunk_texts)

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
                    consecutive_failures = 0
                    logger.debug(
                        f"✅ 已索引文件 {file_path}，生成 {len(chunks)} 个代码块"
                    )

                except Exception as e:
                    logger.error(f"❌ 索引文件 {file_path} 失败: {e}")
                    failed_count += 1
                    consecutive_failures += 1

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
            f"删除={removed_count}, 代码块={total_chunks}"
        )

        return {
            "indexed": indexed_count,
            "skipped": skipped_count,
            "failed": failed_count,
            "removed": removed_count,
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

                    # 文件 hash 变化时清理旧代码块
                    if existing and existing.file_hash != file_hash:
                        await self._cleanup_stale_file_chunks(
                            repo_full_name, str(file_path)
                        )

                    # 解析代码
                    chunks = self.parser.parse_code_file(
                        file_path=str(file_path),
                        content=content,
                        repo_full_name=repo_full_name,
                        commit_sha=commit_sha,
                        file_content_hash=file_hash[:12],
                    )

                    if not chunks:
                        skipped_count += 1
                        continue

                    # 生成嵌入向量
                    chunk_texts = [chunk.content for chunk in chunks]
                    embeddings = await self.embedding_service.embed_texts(chunk_texts)

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

            # 差异清理：比对当前文件列表与数据库索引，清理已删除文件
            cleaned_count = 0
            code_file_set = {str(f) for f in code_files}

            result = await session.execute(
                select(CodeFile).where(
                    and_(
                        CodeFile.repo_full_name == repo_full_name,
                        CodeFile.is_deleted == 0,
                    )
                )
            )
            all_indexed_files = result.scalars().all()

            for indexed_file in all_indexed_files:
                if indexed_file.file_path not in code_file_set:
                    try:
                        await self.vector_store.delete_by_file(
                            repo_full_name, indexed_file.file_path
                        )
                        indexed_file.is_deleted = 1
                        cleaned_count += 1
                        logger.debug(
                            f"清理已删除文件的索引: {indexed_file.file_path}"
                        )
                    except Exception as e:
                        logger.error(
                            f"清理文件索引失败 ({indexed_file.file_path}): {e}"
                        )

            if cleaned_count > 0:
                logger.info(
                    f"仓库 {repo_full_name} 差异清理完成: "
                    f"清理了 {cleaned_count} 个已删除文件的索引"
                )

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
            f"清理={cleaned_count}, 代码块={total_chunks}"
        )

        return {
            "indexed": indexed_count,
            "skipped": skipped_count,
            "failed": failed_count,
            "cleaned": cleaned_count,
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
        query_embedding = await self.embedding_service.embed_texts([query])
        if not query_embedding:
            return []

        # 构建过滤条件（ChromaDB 多条件需使用 $and 运算符）
        filters = []
        if language:
            filters.append({"language": language})
        if file_path:
            filters.append({"file_path": file_path})
        if pr_number is not None:
            filters.append({"pr_number": str(pr_number)})

        if len(filters) == 0:
            where_filters = None
        elif len(filters) == 1:
            where_filters = filters[0]
        else:
            where_filters = {"$and": filters}

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

        使用上一次索引的 commit hash 与当前 commit hash 进行比较，
        只索引发生变化的文件

        Args:
            repo_full_name: 仓库名称
            repo_path: 仓库本地路径
            commit_sha: 新的Commit SHA

        Returns:
            更新结果统计
        """
        logger.info(f"开始增量更新索引，仓库: {repo_full_name}")

        # 获取上一次索引的 commit hash
        last_commit_hash = await self._get_last_commit_hash(repo_full_name)

        if not last_commit_hash:
            logger.info(
                f"仓库 {repo_full_name} 无历史索引记录，执行全量索引"
            )
            return await self.index_repository_code(
                repo_full_name=repo_full_name,
                repo_path=repo_path,
                commit_sha=commit_sha,
            )

        # 获取变更文件列表
        changed_files = await self._get_changed_files(
            repo_path, last_commit_hash, commit_sha
        )

        if changed_files is None:
            # git diff 失败，回退到全量索引
            logger.warning(
                f"获取变更文件列表失败，回退到全量索引: {repo_full_name}"
            )
            return await self.index_repository_code(
                repo_full_name=repo_full_name,
                repo_path=repo_path,
                commit_sha=commit_sha,
            )

        added_files, modified_files, deleted_files = changed_files

        logger.info(
            f"增量更新: 新增={len(added_files)}, "
            f"修改={len(modified_files)}, "
            f"删除={len(deleted_files)}"
        )

        indexed_count = 0
        skipped_count = 0
        failed_count = 0
        cleaned_count = 0
        total_chunks = 0

        repo_path_obj = Path(repo_path)

        async with async_session() as session:
            # 处理新增和修改的文件
            for file_path in added_files + modified_files:
                try:
                    full_path = repo_path_obj / file_path
                    if not full_path.exists():
                        skipped_count += 1
                        continue

                    content = full_path.read_text(
                        encoding="utf-8", errors="ignore"
                    )
                    file_hash = hashlib.sha256(
                        content.encode()
                    ).hexdigest()

                    # 检查是否需要索引
                    existing = await self._get_code_file(
                        session, repo_full_name, file_path
                    )
                    if (
                        existing
                        and existing.file_hash == file_hash
                        and not existing.is_deleted
                    ):
                        skipped_count += 1
                        continue

                    # 文件 hash 变化时清理旧代码块
                    if existing and existing.file_hash != file_hash:
                        await self._cleanup_stale_file_chunks(
                            repo_full_name, file_path
                        )

                    # 解析代码
                    chunks = self.parser.parse_code_file(
                        file_path=file_path,
                        content=content,
                        repo_full_name=repo_full_name,
                        commit_sha=commit_sha,
                        file_content_hash=file_hash[:12],
                    )

                    if not chunks:
                        skipped_count += 1
                        continue

                    # 生成嵌入向量
                    chunk_texts = [chunk.content for chunk in chunks]
                    embeddings = await self.embedding_service.embed_texts(
                        chunk_texts
                    )

                    # 准备向量存储数据
                    vector_chunks = [
                        {
                            "id": chunk.id,
                            "content": chunk.content,
                            "embedding": embedding,
                            "metadata": chunk.metadata,
                        }
                        for chunk, embedding in zip(chunks, embeddings)
                    ]

                    await self.vector_store.upsert_code_chunks(
                        repo_full_name, vector_chunks
                    )
                    total_chunks += len(chunks)

                    await self._upsert_code_file(
                        session=session,
                        repo_full_name=repo_full_name,
                        file_path=file_path,
                        file_hash=file_hash,
                        language=chunks[0].metadata.get("language"),
                        chunk_count=len(chunks),
                        commit_sha=commit_sha,
                    )

                    indexed_count += 1

                except Exception as e:
                    logger.error(
                        f"增量索引文件 {file_path} 失败: {e}"
                    )
                    failed_count += 1

            # 处理删除的文件
            for file_path in deleted_files:
                try:
                    await self.vector_store.delete_by_file(
                        repo_full_name, file_path
                    )
                    existing = await self._get_code_file(
                        session, repo_full_name, file_path
                    )
                    if existing:
                        existing.is_deleted = 1
                        cleaned_count += 1
                except Exception as e:
                    logger.error(
                        f"清理删除文件索引失败 ({file_path}): {e}"
                    )
                    failed_count += 1

            # 更新索引状态
            await self._update_code_index_status(
                session=session,
                repo_full_name=repo_full_name,
                commit_hash=commit_sha,
                file_count=indexed_count,
                total_chunks=total_chunks,
                index_type="incremental",
            )

            await session.commit()

        logger.info(
            f"仓库 {repo_full_name} 增量更新完成: "
            f"索引={indexed_count}, 跳过={skipped_count}, "
            f"失败={failed_count}, 清理={cleaned_count}, "
            f"代码块={total_chunks}"
        )

        return {
            "indexed": indexed_count,
            "skipped": skipped_count,
            "failed": failed_count,
            "cleaned": cleaned_count,
            "total_chunks": total_chunks,
        }

    async def _get_last_commit_hash(
        self, repo_full_name: str
    ) -> Optional[str]:
        """获取仓库上一次索引的 commit hash"""
        async with async_session() as session:
            result = await session.execute(
                select(CodeIndex.last_commit_hash).where(
                    CodeIndex.repo_full_name == repo_full_name
                )
            )
            row = result.scalar_one_or_none()
            return row if row else None

    async def _get_changed_files(
        self, repo_path: str, old_hash: str, new_hash: str
    ) -> Optional[tuple]:
        """使用 git diff 获取变更文件列表

        Args:
            repo_path: 仓库本地路径
            old_hash: 上一次索引的 commit hash
            new_hash: 新的 commit hash

        Returns:
            (added, modified, deleted) 三元组，失败返回 None
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                repo_path,
                "diff",
                "--name-status",
                f"{old_hash}..{new_hash}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.warning(
                    f"git diff 执行失败: {stderr.decode()[:200]}"
                )
                return None

            added, modified, deleted = [], [], []
            for line in stdout.decode().strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                status = parts[0]
                # 过滤只保留支持的代码文件
                if status.startswith("R") or status.startswith("C"):
                    # 重命名/复制: "R100\told\tnew" -> 取新路径
                    if len(parts) >= 3:
                        file_path = parts[-1]
                    else:
                        continue
                else:
                    file_path = parts[1]

                if not self._is_supported_code_file(file_path):
                    continue
                if status == "A":
                    added.append(file_path)
                elif status.startswith("M"):
                    modified.append(file_path)
                elif status.startswith("R") or status.startswith("C"):
                    modified.append(file_path)
                elif status == "D":
                    deleted.append(file_path)

            return added, modified, deleted

        except Exception as e:
            logger.error(f"获取变更文件列表失败: {e}")
            return None

    def _is_supported_code_file(self, file_path: str) -> bool:
        """检查文件是否为支持的代码文件"""
        ext = Path(file_path).suffix.lower()
        for extensions in CodeParserService.LANGUAGE_MAP.values():
            if ext in extensions:
                return True
        return False

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

    async def _cleanup_stale_file_chunks(
        self,
        repo_full_name: str,
        file_path: str,
    ) -> int:
        """清理文件变更前的旧代码块

        当文件 hash 变化或 chunk ID 策略变更时，
        需要在写入新 chunks 之前清理旧 chunks

        Args:
            repo_full_name: 仓库名称
            file_path: 文件路径

        Returns:
            删除的 chunk 数量
        """
        try:
            deleted_count = await self.vector_store.delete_by_file(
                repo_full_name, file_path
            )
            if deleted_count > 0:
                logger.debug(
                    f"已清理文件 {file_path} 的 {deleted_count} 个旧代码块"
                )
            return deleted_count
        except Exception as e:
            logger.warning(f"清理旧代码块失败 ({file_path}): {e}")
            return 0

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
