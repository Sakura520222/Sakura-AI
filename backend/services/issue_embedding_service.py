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
        self, repo_owner: str, repo_name: str, *, force: bool = False
    ) -> Dict[str, Any]:
        """将仓库 issues 索引到 ChromaDB

        Args:
            force: 增量补充缺失的 issues（含 open + closed），已有文档保留 AI 摘要。
                   默认 False 时仅索引 open issues 且跳过已有缓存。

        Returns:
            {"status": "cached"|"indexed"|"reindexed"|"no_issues", "count": int}
        """
        collection_key = self._collection_key(repo_owner, repo_name)

        # 检查已有缓存
        count = await self.vector_store.get_collection_count(collection_key)
        if count > 0 and not force:
            logger.debug(
                f"Issue 向量库已存在 ({count} 条): {repo_owner}/{repo_name}"
            )
            return {"status": "cached", "count": count}

        if force:
            return await self._incremental_reindex(
                repo_owner, repo_name, collection_key, count
            )

        # 首次索引：仅获取 open issues
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
            f"✅ 已索引 {len(documents)} 个 issues "
            f"到向量库: {repo_owner}/{repo_name}"
        )
        return {"status": "indexed", "count": len(documents)}

    async def _incremental_reindex(
        self,
        repo_owner: str,
        repo_name: str,
        collection_key: str,
        existing_count: int,
    ) -> Dict[str, Any]:
        """增量重索引：补充缺失 issues，已有文档保留 AI 摘要并更新 state"""
        # 1. 获取所有 issues（open + closed）
        issues = await asyncio.to_thread(
            self._fetch_all_issues, repo_owner, repo_name
        )
        if not issues:
            return {"status": "no_issues", "count": existing_count}

        issue_map = {issue.number: issue for issue in issues}

        # 2. 获取已有文档 ID
        collection = await self.vector_store.get_or_create_collection(
            collection_key
        )
        existing_ids = set()
        if existing_count > 0:
            # 仅获取 ids，避免大量 issues 时一次性加载 metadatas 导致 OOM
            all_existing = collection.get(include=[])
            existing_ids = set(all_existing["ids"])

        # 3. 从数据库获取 AI 分析结果
        ai_results = await self._fetch_ai_analysis(
            repo_owner, repo_name, list(issue_map.keys())
        )

        # 4. 分类：需要新增 vs 需要更新 state
        new_issues = []
        state_updates = []
        for number, issue in issue_map.items():
            doc_id = f"{self.ISSUE_ID_PREFIX}{number}"
            if doc_id not in existing_ids:
                new_issues.append(issue)
            else:
                # 检查 state 是否需要更新
                state_updates.append((doc_id, issue))

        # 5. 更新已有文档的 state metadata
        updated_count = 0
        if state_updates:
            updated_count = await self._update_existing_states(
                collection_key, state_updates
            )

        # 6. 新增缺失的 issues
        added_count = 0
        if new_issues:
            added_count = await self._add_new_issues_with_ai(
                collection_key, new_issues, ai_results
            )

        total = existing_count + added_count
        logger.info(
            f"✅ Issues 增量重索引完成: {repo_owner}/{repo_name}, "
            f"新增={added_count}, state更新={updated_count}, 总计={total}"
        )
        return {
            "status": "reindexed",
            "count": total,
            "added": added_count,
            "updated": updated_count,
        }

    async def _fetch_ai_analysis(
        self, repo_owner: str, repo_name: str, issue_numbers: list[int]
    ) -> Dict[int, Dict[str, str]]:
        """从数据库获取 issues 的 AI 分析结果（summary + suggested_title）"""
        try:
            from backend.models.database import IssueAnalysis, async_session
            from sqlalchemy import select

            repo_full = f"{repo_owner}/{repo_name}"
            async with async_session() as session:
                result = await session.execute(
                    select(
                        IssueAnalysis.issue_number,
                        IssueAnalysis.summary,
                        IssueAnalysis.suggested_title,
                    ).where(
                        IssueAnalysis.repo_name == repo_full,
                        IssueAnalysis.issue_number.in_(issue_numbers),
                    )
                )
                return {
                    row.issue_number: {
                        "summary": row.summary or "",
                        "suggested_title": row.suggested_title or "",
                    }
                    for row in result
                }
        except Exception as e:
            logger.warning(f"获取 AI 分析结果失败: {e}")
            return {}

    async def _update_existing_states(
        self,
        collection_key: str,
        state_updates: list[tuple[str, Any]],
    ) -> int:
        """批量更新已有文档的 state metadata"""
        collection = await self.vector_store.get_or_create_collection(
            collection_key
        )

        # 批量获取所有需要更新的文档
        doc_ids = [doc_id for doc_id, _ in state_updates]
        issue_map = {doc_id: issue for doc_id, issue in state_updates}

        try:
            all_existing = collection.get(
                ids=doc_ids, include=["embeddings", "metadatas", "documents"]
            )
        except Exception as e:
            logger.warning(f"批量获取 issue 文档失败: {e}")
            return 0

        # 筛选出 state 确实变化的文档，批量 upsert
        to_update = []
        for i, doc_id in enumerate(all_existing["ids"]):
            old_metadata = all_existing["metadatas"][i]
            issue = issue_map[doc_id]
            if old_metadata.get("state") == issue.state:
                continue

            new_metadata = {**old_metadata, "state": issue.state}
            to_update.append({
                "id": doc_id,
                "content": all_existing["documents"][i],
                "embedding": all_existing["embeddings"][i],
                "metadata": new_metadata,
            })

        if to_update:
            try:
                await self.vector_store.upsert_documents(
                    collection_key, to_update
                )
            except Exception as e:
                logger.warning(f"批量更新 issue state 失败: {e}")
                return 0

        return len(to_update)

    async def _add_new_issues_with_ai(
        self,
        collection_key: str,
        new_issues: list,
        ai_results: Dict[int, Dict[str, str]],
    ) -> int:
        """新增缺失的 issues，优先使用 AI 分析结果"""
        documents = []
        texts = []
        for issue in new_issues:
            ai = ai_results.get(issue.number, {})
            # 优先使用 AI 建议标题，否则用原始标题
            title = ai.get("suggested_title") or issue.title
            # 优先使用 AI 摘要，否则用原始 body
            body = ai.get("summary") or (issue.body or "")

            text = f"{title}\n{body}"
            texts.append(text)
            documents.append({
                "id": f"{self.ISSUE_ID_PREFIX}{issue.number}",
                "content": text,
                "metadata": {
                    "number": str(issue.number),
                    "title": title,
                    "state": issue.state,
                },
            })

        if not texts:
            return 0

        embeddings = await self.embedding_service.embed_texts(texts)
        for i, emb in enumerate(embeddings):
            documents[i]["embedding"] = emb

        await self.vector_store.add_documents(collection_key, documents)
        return len(documents)

    async def search_related_issues(
        self,
        repo_owner: str,
        repo_name: str,
        pr_title: str,
        pr_body: str,
        exclude_numbers: List[int],
        top_k: int = 5,
        similarity_threshold: float = 0.65,
        state_filter: str | None = None,
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
        where_clause = {"state": state_filter} if state_filter else None
        candidates = await self.vector_store.search(
            collection_key, query_embedding, top_k=top_k * 3, where=where_clause
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
                "state": doc["metadata"].get("state", "open"),
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
                "state": c["metadata"].get("state", "open"),
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
                "你是一个代码审查助手。判断给定的 Issues 是否与 PR 的代码变更有关联。\n\n"
                "关联方式（满足任一即可）：\n"
                "1. PR 描述显式提及修复/实现该 Issue\n"
                "2. PR 的代码变更直接修复了 Issue 描述的具体问题"
                "（例如 Issue 报告 API 返回 413 错误，PR 添加了请求体截断逻辑）\n"
                "3. PR 的代码变更直接实现了 Issue 要求的功能\n\n"
                "以下情况不算关联：\n"
                "- 仅关键词或主题相似，但 PR 并未解决 Issue 描述的具体问题\n"
                "- 属于同一项目/模块，但无直接解决关系\n"
                "- Issue 是一个广泛的需求，PR 只是碰巧涉及相关代码\n\n"
                "判断原则：如果 PR 的代码变更能够解决 Issue 描述的问题，即使 PR 描述未显式提及，也应判定为关联。"
                "仅在确实无法确认因果关系时才不关联。\n\n"
                '返回 JSON: {"verified": [issue_number, ...], '
                '"reasons": {"编号": "一句话说明为什么关联或不关联"}}\n'
                "如果都不关联，返回 {\"verified\": [], \"reasons\": {}}\n"
                "只返回 JSON，不要其他文字。"
            )

            user_prompt = (
                f"## Pull Request\n{pr_context}\n\n"
                f"## 候选 Issues\n{issues_text}\n\n"
                "逐一判断每个 Issue 是否与该 PR 有关联，返回 JSON。"
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
        # 1. 直接尝试解析整个响应
        try:
            data = json.loads(content.strip())
            if "verified" in data:
                return set(int(n) for n in data["verified"])
        except (json.JSONDecodeError, ValueError):
            pass

        # 2. 尝试从 markdown 代码块中提取完整内容再解析
        code_match = re.search(r"```(?:json)?\s*(.*?)\s*```", content, re.DOTALL)
        if code_match:
            try:
                data = json.loads(code_match.group(1).strip())
                if "verified" in data:
                    return set(int(n) for n in data["verified"])
            except (json.JSONDecodeError, ValueError):
                pass

        # 3. 逐字符花括号计数提取最外层完整 JSON
        depth = 0
        start = None
        for i, ch in enumerate(content):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        data = json.loads(content[start : i + 1])
                        if "verified" in data:
                            return set(int(n) for n in data["verified"])
                    except (json.JSONDecodeError, ValueError):
                        pass
                    start = None

        logger.warning(
            f"解析 AI 验证响应失败，前200字: {content[:200]!r}"
        )
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

    async def close_issue(
        self, repo_owner: str, repo_name: str, issue_number: int
    ) -> bool:
        """将 issue 标记为 closed（更新 metadata.state，而非删除）

        如果向量库中不存在该 issue，静默返回 True（可能尚未索引）。
        """
        try:
            collection_key = self._collection_key(repo_owner, repo_name)
            doc_id = f"{self.ISSUE_ID_PREFIX}{issue_number}"

            # 获取已有文档（含 embedding）
            collection = await self.vector_store.get_or_create_collection(
                collection_key
            )
            existing = collection.get(
                ids=[doc_id], include=["embeddings", "metadatas", "documents"]
            )
            if not existing["ids"]:
                logger.debug(
                    f"Issue 向量不存在，跳过关闭: "
                    f"{repo_owner}/{repo_name}#{issue_number}"
                )
                return True

            old_metadata = existing["metadatas"][0]
            old_content = existing["documents"][0]
            old_embedding = existing["embeddings"][0]

            new_metadata = {**old_metadata, "state": "closed"}

            await self.vector_store.upsert_documents(
                collection_key,
                [{
                    "id": doc_id,
                    "content": old_content,
                    "embedding": old_embedding,
                    "metadata": new_metadata,
                }],
            )
            logger.debug(
                f"已标记 issue 为 closed: {repo_owner}/{repo_name}#{issue_number}"
            )
            return True
        except Exception as e:
            logger.warning(
                f"标记 issue closed 失败: "
                f"{repo_owner}/{repo_name}#{issue_number}: {e}"
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

    def _fetch_all_issues(
        self, repo_owner: str, repo_name: str, states: list[str] | None = None
    ) -> list:
        """同步获取仓库所有 issues（含 open + closed），按 number 去重

        用于 WebUI 强制重索引场景。首次自动索引仍使用 _fetch_all_open_issues
        以减少 GitHub API 调用量。
        """
        if states is None:
            states = ["open", "closed"]

        client = self.github_app.get_repo_client(repo_owner, repo_name)
        if not client:
            logger.warning(f"无法获取仓库客户端: {repo_owner}/{repo_name}")
            return []
        repo = client.get_repo(f"{repo_owner}/{repo_name}")

        all_issues = []
        for state in states:
            all_issues.extend([
                issue
                for issue in repo.get_issues(state=state)
                if issue.pull_request is None
            ])

        # 按 number 去重（open/closed 列表可能有重叠）
        seen: set[int] = set()
        unique = []
        for issue in all_issues:
            if issue.number not in seen:
                seen.add(issue.number)
                unique.append(issue)

        logger.debug(
            f"获取 issues: {repo_owner}/{repo_name}, "
            f"states={states}, unique={len(unique)}"
        )
        return unique
