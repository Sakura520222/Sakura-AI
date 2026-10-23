"""Issue 语义嵌入与检索服务

利用 ChromaDB 缓存仓库 issues 的向量嵌入，
在 PR 审查时进行语义检索关联。

检索使用共享 open + closed 语料，按持久检查点增量同步 GitHub 原始正文。
候选在余弦过滤后重新获取当前 GitHub 事实，并按严格错误语义重排序。
"""

from typing import Any

from loguru import logger

from backend.core.github_app import GitHubAppClient
from backend.services.ai_reviewer.api_client import AIApiClient
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
    def _safe_parse_number(value) -> int | None:
        """安全地将 metadata 中的 number 转为 int，失败返回 None"""
        try:
            return int(value)
        except ValueError, TypeError:
            return None

    async def index_repo_issues(
        self, repo_owner: str, repo_name: str, *, force: bool = False
    ) -> dict[str, Any]:
        """将仓库 issues 索引到 ChromaDB

        Args:
            force: 绕过同步间隔与增量游标，重新同步所有 open + closed issues。
                   默认按持久检查点增量同步；无检查点的旧集合全量初始化。

        Returns:
            {"status": "cached"|"indexed"|"reindexed"|"no_issues", "count": int}
        """
        from backend.services.issues.corpus_service import IssueCorpusService

        return await IssueCorpusService(self).reconcile(
            repo_owner, repo_name, force=force
        )

    async def search_related_issues(
        self,
        repo_owner: str,
        repo_name: str,
        pr_title: str,
        pr_body: str,
        exclude_numbers: list[int],
        top_k: int = 5,
        similarity_threshold: float = 0.65,
        state_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Compatible retrieval API; PR queries default to current open Issues."""
        from backend.services.issues.candidate_retriever import IssueCandidateRetriever
        from backend.services.pr_body import strip_sakura_generated_sections

        return await IssueCandidateRetriever(self).retrieve(
            repo_owner,
            repo_name,
            text=f"{pr_title}\n{strip_sakura_generated_sections(pr_body)}",
            state=state_filter or "open",
            exclude_numbers=exclude_numbers,
            top_k=top_k,
            similarity_threshold=similarity_threshold,
        )

    async def upsert_issue(
        self,
        repo_owner: str,
        repo_name: str,
        issue_number: int,
        title: str,
        body: str,
        state: str,
        analysis_metadata: dict | None = None,
    ) -> bool:
        """更新或插入单个 issue（webhook 调用）

        Args:
            analysis_metadata: AI 分析元数据，可包含 category/priority/feasibility 等字段
        """
        try:
            from backend.services.issues.corpus_service import IssueCorpusService

            enrichment = None
            if analysis_metadata is not None:
                enrichment = {
                    key: analysis_metadata.get(key)
                    for key in ("category", "priority", "feasibility")
                }
                enrichment["summary"] = body
            # Caller title/body/state may come from a delayed lifecycle event.
            # All source facts are hydrated under common corpus ownership.
            await IssueCorpusService(self).update_issue(
                repo_owner, repo_name, issue_number, enrichment=enrichment
            )
            logger.debug(f"已更新 issue 向量: {repo_owner}/{repo_name}#{issue_number}")
            return True
        except Exception as e:
            logger.warning(
                f"更新 issue 向量失败: {repo_owner}/{repo_name}#{issue_number}: {e}"
            )
            return False

    async def verify_related_issues(
        self,
        pr_title: str,
        pr_body: str,
        candidates: list[dict[str, Any]],
        pr_summary: str = "",
        pr_files: str = "",
    ) -> list[dict[str, Any]]:
        """Compatible list boundary; failures never admit unverified candidates.

        Legacy free-form files cannot establish complete closing evidence.
        Generated summaries are deliberately ignored.
        """
        from backend.services.issues.pr_verifier import PRRelationVerifier

        result = await PRRelationVerifier(AIApiClient()).verify(
            pr_title=pr_title,
            pr_body=pr_body,
            candidates=candidates,
            files=[{"path": "legacy", "patch": pr_files, "complete": False}],
            raise_configuration_error=True,
        )
        return result.relations if result.succeeded else []

    async def remove_issue(
        self, repo_owner: str, repo_name: str, issue_number: int
    ) -> bool:
        """从 ChromaDB 删除单个 issue（webhook 调用）"""
        try:
            from backend.services.issues.corpus_service import IssueCorpusService

            return await IssueCorpusService(self).remove_issue(
                repo_owner, repo_name, issue_number
            )
        except Exception as e:
            logger.warning(
                f"删除 issue 向量失败: {repo_owner}/{repo_name}#{issue_number}: {e}"
            )
            return False

    async def close_issue(
        self, repo_owner: str, repo_name: str, issue_number: int
    ) -> bool:
        """Reconcile current source state, including deferred close/reopen events."""
        try:
            from backend.services.issues.corpus_service import IssueCorpusService

            await IssueCorpusService(self).update_issue(
                repo_owner, repo_name, issue_number
            )
            return True
        except Exception as e:
            logger.warning(
                f"同步 issue state 失败: {repo_owner}/{repo_name}#{issue_number}: {e}"
            )
            return False
