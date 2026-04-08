"""Issue 语义嵌入与检索服务

利用 ChromaDB 缓存仓库 issues 的向量嵌入，
在 PR 审查时进行语义检索关联。
检索流程：Embedding 初步召回 → Reranker 重排序 → 阈值过滤

注意：向量库在首次 PR 审查时全量构建，后续依赖 webhook 增量同步。
如果 webhook 事件丢失（如服务宕机），可通过清空 ChromaDB collection 触发重建。
"""

import asyncio
from typing import Any, Dict, List

from loguru import logger

from backend.core.github_app import GitHubAppClient
from backend.services.embedding_service import (
    get_embedding_service,
    get_reranker_service,
)
from backend.services.vector_store import get_vector_store


class IssueEmbeddingService:
    """Issue 语义嵌入与检索服务"""

    ISSUE_COLLECTION_SUFFIX = "_issues"
    ISSUE_ID_PREFIX = "issue_"

    def __init__(self):
        self.github_app = GitHubAppClient()
        self._embedding_service = None
        self._vector_store = None
        self._reranker_service = None

    @property
    def embedding_service(self):
        if self._embedding_service is None:
            self._embedding_service = get_embedding_service()
        return self._embedding_service

    @property
    def vector_store(self):
        if self._vector_store is None:
            self._vector_store = get_vector_store()
        return self._vector_store

    @property
    def reranker_service(self):
        if self._reranker_service is None:
            self._reranker_service = get_reranker_service()
        return self._reranker_service

    def _collection_key(self, repo_owner: str, repo_name: str) -> str:
        """生成 ChromaDB Collection key"""
        return f"{repo_owner}/{repo_name}{self.ISSUE_COLLECTION_SUFFIX}"

    @staticmethod
    def _safe_parse_number(value) -> int:
        """安全地将 metadata 中的 number 转为 int"""
        try:
            return int(value)
        except (ValueError, TypeError):
            return 0

    async def index_repo_issues(
        self, repo_owner: str, repo_name: str
    ) -> Dict[str, Any]:
        """将仓库所有 open issues 索引到 ChromaDB

        Collection 已有数据时跳过（依赖 webhook 增量同步）。

        Returns:
            {"status": "cached"|"indexed"|"no_issues", "count": int}
        """
        collection_key = self._collection_key(repo_owner, repo_name)

        # 检查已有缓存
        count = await self.vector_store.get_collection_count(collection_key)
        if count > 0:
            logger.debug(
                f"Issue 向量库已存在 ({count} 条): {repo_owner}/{repo_name}"
            )
            return {"status": "cached", "count": count}

        # 获取所有 open issues（PyGithub 自动分页）
        issues = await asyncio.to_thread(
            self._fetch_all_open_issues, repo_owner, repo_name
        )
        if not issues:
            return {"status": "no_issues", "count": 0}

        # 构建文档并批量嵌入
        documents, texts = self._build_documents(issues)
        embeddings = await self.embedding_service.embed_texts(texts)
        for i, emb in enumerate(embeddings):
            documents[i]["embedding"] = emb

        # 写入 ChromaDB
        await self.vector_store.add_documents(collection_key, documents)
        logger.info(
            f"✅ 已索引 {len(documents)} 个 issues 到向量库: {repo_owner}/{repo_name}"
        )
        return {"status": "indexed", "count": len(documents)}

    async def search_related_issues(
        self,
        repo_owner: str,
        repo_name: str,
        pr_title: str,
        pr_body: str,
        exclude_numbers: List[int],
        top_k: int = 5,
        similarity_threshold: float = 0.65,
    ) -> List[Dict[str, Any]]:
        """检索与 PR 语义相关的 issues

        流程: Embedding 召回 → Reranker 精排 → 阈值过滤
        """
        # 1. 确保索引存在
        index_result = await self.index_repo_issues(repo_owner, repo_name)
        if index_result["status"] == "no_issues":
            return []

        collection_key = self._collection_key(repo_owner, repo_name)

        # 2. 生成查询向量
        pr_text = f"{pr_title}\n{pr_body or ''}"
        query_embedding = await self.embedding_service.embed_query(pr_text)

        # 3. ChromaDB ANN 初步召回（多取候选用于后续过滤）
        candidates = await self.vector_store.search(
            collection_key, query_embedding, top_k=top_k * 3
        )

        if not candidates:
            return []

        # 4. Reranker 重排序
        exclude_set = set(exclude_numbers)
        reranked = await self._rerank_candidates(
            pr_text, candidates, top_k, similarity_threshold, exclude_set
        )

        return reranked

    async def _rerank_candidates(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
        top_k: int,
        threshold: float,
        exclude_set: set,
    ) -> List[Dict[str, Any]]:
        """使用 Reranker 对候选进行精排"""
        # 检查 Reranker 是否启用
        if self.reranker_service.client is None:
            return self._filter_by_cosine(
                candidates, top_k, threshold, exclude_set
            )

        # 构造 reranker 输入格式
        docs_for_rerank = [
            {"content": c["content"], "metadata": c["metadata"]}
            for c in candidates
        ]

        reranked_docs = await self.reranker_service.rerank(
            query=query, docs=docs_for_rerank, top_k=top_k
        )

        # 从 reranked 结果中提取 issue 信息
        results = []
        for doc in reranked_docs:
            number = self._safe_parse_number(doc["metadata"].get("number"))
            if number in exclude_set or number == 0:
                continue
            results.append({
                "number": number,
                "title": doc["metadata"].get("title", ""),
                "similarity": "reranked",
            })
            if len(results) >= top_k:
                break

        return results

    def _filter_by_cosine(
        self,
        candidates: List[Dict[str, Any]],
        top_k: int,
        threshold: float,
        exclude_set: set,
    ) -> List[Dict[str, Any]]:
        """回退：使用 cosine similarity 过滤"""
        results = []
        for c in candidates:
            number = self._safe_parse_number(c["metadata"].get("number"))
            if number in exclude_set or number == 0:
                continue
            # ChromaDB 默认 cosine distance，clamp 防止异常值
            similarity = max(0.0, 1.0 - c["distance"])
            if similarity < threshold:
                continue
            results.append({
                "number": number,
                "title": c["metadata"].get("title", ""),
                "similarity": round(similarity, 3),
            })
            if len(results) >= top_k:
                break
        return results

    async def upsert_issue(
        self,
        repo_owner: str,
        repo_name: str,
        issue_number: int,
        title: str,
        body: str,
        state: str,
    ) -> bool:
        """更新或插入单个 issue（webhook 调用）"""
        try:
            collection_key = self._collection_key(repo_owner, repo_name)
            text = f"{title}\n{body or ''}"
            embedding = await self.embedding_service.embed_query(text)

            document = {
                "id": f"{self.ISSUE_ID_PREFIX}{issue_number}",
                "content": text,
                "embedding": embedding,
                "metadata": {
                    "number": str(issue_number),
                    "title": title,
                    "state": state,
                },
            }

            await self.vector_store.upsert_documents(
                collection_key, [document]
            )
            logger.debug(
                f"已更新 issue 向量: {repo_owner}/{repo_name}#{issue_number}"
            )
            return True
        except Exception as e:
            logger.warning(
                f"更新 issue 向量失败: {repo_owner}/{repo_name}#{issue_number}: {e}"
            )
            return False

    async def remove_issue(
        self, repo_owner: str, repo_name: str, issue_number: int
    ) -> bool:
        """从 ChromaDB 删除单个 issue（webhook 调用）"""
        try:
            collection_key = self._collection_key(repo_owner, repo_name)
            doc_id = f"{self.ISSUE_ID_PREFIX}{issue_number}"
            await self.vector_store.delete_documents(collection_key, [doc_id])
            logger.debug(
                f"已删除 issue 向量: {repo_owner}/{repo_name}#{issue_number}"
            )
            return True
        except Exception as e:
            logger.warning(
                f"删除 issue 向量失败: {repo_owner}/{repo_name}#{issue_number}: {e}"
            )
            return False

    def _build_documents(
        self, issues: list
    ) -> tuple:
        """从 PyGithub Issue 对象构建 ChromaDB 文档"""
        documents = []
        texts = []
        for issue in issues:
            text = f"{issue.title}\n{issue.body or ''}"
            texts.append(text)
            documents.append({
                "id": f"{self.ISSUE_ID_PREFIX}{issue.number}",
                "content": text,
                "metadata": {
                    "number": str(issue.number),
                    "title": issue.title,
                    "state": issue.state,
                },
            })
        return documents, texts

    def _fetch_all_open_issues(
        self, repo_owner: str, repo_name: str
    ) -> list:
        """同步获取仓库所有 open issues（线程内调用，PyGithub 自动分页）

        注意：PyGithub 的 get_issues() 会同时返回 Issue 和 PR，
        通过 pull_request 属性过滤掉 PR。
        """
        client = self.github_app.get_repo_client(repo_owner, repo_name)
        if not client:
            logger.warning(f"无法获取仓库客户端: {repo_owner}/{repo_name}")
            return []
        repo = client.get_repo(f"{repo_owner}/{repo_name}")
        # 过滤掉 Pull Request（PyGithub 的 get_issues 会包含 PR）
        return [
            issue
            for issue in repo.get_issues(state="open")
            if issue.pull_request is None
        ]
