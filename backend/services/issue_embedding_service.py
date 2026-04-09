"""Issue 语义嵌入与检索服务

利用 ChromaDB 缓存仓库 issues 的向量嵌入，
在 PR 审查时进行语义检索关联。

检索流程：Embedding 初步召回 → Reranker 重排序 → 阈值过滤 → AI 验证

向量库在首次 PR 审查时全量构建，后续依赖 webhook 增量同步。
如果 webhook 事件丢失（如服务宕机），可通过清空 ChromaDB collection 触发重建。
"""

import asyncio
import json
import re
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
    def _safe_parse_number(value) -> int | None:
        """安全地将 metadata 中的 number 转为 int，失败返回 None"""
        try:
            return int(value)
        except (ValueError, TypeError):
            return None

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
            if number is None or number in exclude_set:
                continue
            results.append({
                "number": number,
                "title": doc["metadata"].get("title", ""),
                "content": doc.get("content", ""),
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
            if number is None or number in exclude_set:
                continue
            # ChromaDB 默认 cosine distance，clamp 防止异常值
            similarity = max(0.0, 1.0 - c["distance"])
            if similarity < threshold:
                continue
            results.append({
                "number": number,
                "title": c["metadata"].get("title", ""),
                "content": c.get("content", ""),
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

    async def verify_related_issues(
        self,
        pr_title: str,
        pr_body: str,
        candidates: List[Dict[str, Any]],
        pr_summary: str = "",
        pr_files: str = "",
    ) -> List[Dict[str, Any]]:
        """使用 AI 批量验证候选 issues 是否真正与 PR 相关

        一次 LLM 调用，传入所有候选 issues，AI 返回真正相关的编号列表。
        使用辅助模型（summary_model）以降低成本。失败时回退返回原始候选。

        Args:
            pr_title: PR 标题
            pr_body: PR 描述
            candidates: search_related_issues 的返回结果
            pr_summary: PR 摘要（可选，提供更多上下文）
            pr_files: PR 变更文件列表文本（可选）

        Returns:
            通过 AI 验证的候选列表（可能为空）
        """
        if not candidates:
            return candidates

        try:
            from backend.core.config import get_settings
            from backend.services.ai_reviewer.api_client import AIApiClient

            settings = get_settings()

            # 使用辅助模型（更便宜）
            api_base = settings.summary_api_base or settings.openai_api_base
            api_key = settings.summary_api_key or settings.openai_api_key
            model = settings.summary_model or settings.openai_model

            client = AIApiClient(base_url=api_base, api_key=api_key)

            # 构建候选 issues 文本
            issues_text = ""
            for issue in candidates:
                content = issue.get("content", "")
                logger.info(
                    f"Issue #{issue['number']} content 长度: "
                    f"{len(content)}, 前100字: {content[:100]!r}"
                )
                issues_text += (
                    f"\n### Issue #{issue['number']}: {issue['title']}\n"
                    f"{content}\n"
                )

            # 构建 PR 上下文
            pr_context = f"标题: {pr_title}\n描述: {pr_body or '无描述'}"
            if pr_summary:
                pr_context += f"\n\nPR 摘要:\n{pr_summary}"
            if pr_files:
                pr_context += f"\n\n变更文件:\n{pr_files}"

            system_prompt = (
                "你是一个严格的代码审查助手。判断给定的 Issues 是否与 PR 的代码变更有【直接因果关系】。\n\n"
                "严格关联标准（必须满足）：\n"
                "Issue 描述的具体问题或功能需求，会被该 PR 的代码变更直接修复或实现。"
                "你能从 PR 的变更文件和内容中明确看到解决该 Issue 的改动。\n\n"
                "以下情况【不算关联】：\n"
                "- 仅关键词或主题相似，但 PR 并未解决 Issue 描述的具体问题\n"
                "- 属于同一项目/模块，但无直接解决关系\n"
                "- Issue 是一个广泛的需求，PR 只是碰巧涉及相关代码\n"
                "- 无法从 PR 变更中看出与 Issue 的直接因果关系\n\n"
                "宁可不关联也不要误关联。如果不确定，则不关联。\n\n"
                '返回 JSON: {"verified": [issue_number, ...], '
                '"reasons": {"编号": "一句话说明为什么关联或不关联"}}\n'
                "如果都不关联，返回 {\"verified\": [], \"reasons\": {}}\n"
                "只返回 JSON，不要其他文字。"
            )

            user_prompt = (
                f"## Pull Request\n{pr_context}\n\n"
                f"## 候选 Issues\n{issues_text}\n\n"
                "逐一判断每个 Issue 是否与该 PR 有直接因果关系，返回 JSON。"
            )

            logger.info(
                f"AI 验证请求: {len(candidates)} 个候选, "
                f"issues 内容长度: {sum(len(i.get('content', '')) for i in candidates)}"
            )

            response = await client.call_with_retry(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model=model,
                temperature=0.1,
                max_tokens=500,
                timeout=30.0,
            )

            # 解析 AI 响应
            ai_content = response.choices[0].message.content
            logger.info(f"AI 验证原始响应: {ai_content}")
            verified_numbers = self._parse_verified_numbers(ai_content)

            if verified_numbers is None:
                logger.warning("AI 验证响应解析失败，回退使用原始候选")
                return candidates

            # 过滤候选
            verified = [
                c for c in candidates if c["number"] in verified_numbers
            ]

            filtered_count = len(candidates) - len(verified)
            if filtered_count > 0:
                logger.info(
                    f"AI 验证过滤了 {filtered_count} 个误判: "
                    f"保留 {[c['number'] for c in verified]}, "
                    f"移除 {[c['number'] for c in candidates if c['number'] not in verified_numbers]}"
                )

            return verified

        except Exception as e:
            logger.warning(f"AI 验证失败，回退使用原始候选: {e}")
            return candidates

    @staticmethod
    def _parse_verified_numbers(content: str) -> set | None:
        """从 AI 响应中解析验证通过的 issue 编号集合"""
        try:
            # 尝试提取 JSON（可能被 markdown 代码块包裹）
            json_match = re.search(
                r'\{[^}]*"verified"\s*:\s*\[[^\]]*\][^}]*\}', content
            )
            if json_match:
                data = json.loads(json_match.group())
                return set(int(n) for n in data.get("verified", []))
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.debug(f"解析 AI 验证响应失败: {e}")
        return None

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
