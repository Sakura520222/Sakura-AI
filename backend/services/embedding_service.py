"""嵌入服务

支持多种嵌入模型提供商：
- SiliconFlow (默认): BAAI/bge-m3
- OpenAI: text-embedding-3-small/large
- Ollama: 本地模型
"""

import threading
from typing import Any
from uuid import uuid4

import httpx
from loguru import logger
from openai import AsyncOpenAI

from backend.core.config import get_settings
from backend.services.activity_observability.contracts import InvocationContext

# 嵌入文本最大字符数（防御性兜底，确保不超过 BGE-M3 的 8192 token 限制）
# 中文约 1 token ≈ 1.5 字符，8000 字符 ≈ 5300 tokens，留足余量
MAX_EMBEDDING_CHARS = 8000


class EmbeddingService:
    """嵌入服务

    将文本转换为向量嵌入，支持多种提供商。
    """

    def __init__(
        self, *, observer: Any = None, context: InvocationContext | None = None
    ):
        """初始化服务；observer/context 仅用于可选真实发送观察。"""
        self.provider = ""
        self._observer = observer
        self._context = context
        self.client = None
        self._client_config = None
        self._retired_clients = []
        self._refresh_client()

    def _refresh_client(self):
        """配置变化时刷新客户端，旧客户端延迟到服务关闭时释放。

        本方法被 async 的 ``embed_texts`` 同步调用，须保持轻量：当前仅做配置
        比较与客户端对象构造（AsyncOpenAI 构造不建连）；若未来
        ``_init_client`` 引入耗时初始化需改用 ``asyncio.to_thread``。
        """
        settings = get_settings()
        config = (
            settings.embedding_provider.lower(),
            settings.embedding_base_url,
            settings.embedding_api_key,
            settings.embedding_model,
        )
        if self._client_config == config:
            return

        if self.client is not None:
            self._retired_clients.append(self.client)
        self.provider = config[0]
        self.client = None
        self._init_client(settings)
        self._client_config = config

    def _init_client(self, settings):
        """根据配置初始化对应的客户端"""
        try:
            if self.provider == "siliconflow":
                # SiliconFlow 使用 OpenAI 兼容 API
                self.client = AsyncOpenAI(
                    base_url=settings.embedding_base_url,
                    api_key=settings.embedding_api_key,
                    max_retries=0,
                )
                logger.info(
                    f"✅ 嵌入服务初始化成功: {self.provider} ({settings.embedding_model})"
                )

            elif self.provider == "openai":
                # OpenAI 官方 API
                self.client = AsyncOpenAI(
                    base_url=settings.embedding_base_url,
                    api_key=settings.embedding_api_key,
                    max_retries=0,
                )
                logger.info(
                    f"✅ 嵌入服务初始化成功: {self.provider} ({settings.embedding_model})"
                )

            elif self.provider == "ollama":
                # Ollama 本地 API（也兼容 OpenAI 格式）
                self.client = AsyncOpenAI(
                    base_url=settings.embedding_base_url,
                    api_key=settings.embedding_api_key or "ollama",  # Ollama 不需要 key
                    max_retries=0,
                )
                logger.info("✅ 嵌入服务初始化成功: {}", self.provider)

            else:
                raise ValueError(f"不支持的嵌入提供商: {self.provider}")

        except Exception as e:
            logger.error("❌ 嵌入服务初始化失败: type={}", type(e).__name__)
            raise

    async def embed_texts(
        self,
        texts: list[str],
        *,
        context: InvocationContext | None = None,
        observer: Any = None,
        logical_call_id: str | None = None,
    ) -> list[list[float]]:
        """批量生成文本嵌入向量

        Args:
            texts: 文本列表

        Returns:
            嵌入向量列表，每个向量是一个 float 数组
        """
        if not texts:
            return []

        active_context = context or self._context
        active_observer = observer or self._observer
        active_logical_call_id = logical_call_id or str(uuid4())
        if active_context is not None and active_observer is None:
            raise ValueError("InvocationContext requires an observer")
        if active_observer is not None:
            active_observer.context = active_context
        self._refresh_client()
        try:
            if self.provider in ["siliconflow", "openai", "ollama"]:
                # 使用 OpenAI 兼容 API
                return await self._embed_via_openai_api(
                    texts,
                    observer=active_observer,
                    context=active_context,
                    logical_call_id=active_logical_call_id,
                )
            raise ValueError(f"不支持的嵌入提供商: {self.provider}")
        except Exception as e:
            logger.error("❌ 生成嵌入向量失败: type={}", type(e).__name__)
            raise

    @staticmethod
    def _truncate_text(text: str, max_chars: int = MAX_EMBEDDING_CHARS) -> str:
        """截断超长文本（防御性兜底，优先在段落边界截断）"""
        if len(text) <= max_chars:
            return text
        truncated = text[:max_chars]
        last_break = truncated.rfind("\n\n")
        if last_break > max_chars * 0.7:
            truncated = truncated[:last_break]
        return truncated

    async def _embed_via_openai_api(
        self,
        texts: list[str],
        *,
        observer: Any = None,
        context: InvocationContext | None = None,
        logical_call_id: str | None = None,
    ) -> list[list[float]]:
        """通过 OpenAI 兼容 API 生成嵌入（支持批处理）

        支持：SiliconFlow、OpenAI、Ollama
        """
        try:
            settings = get_settings()
            batch_size = settings.embedding_batch_size
            all_embeddings = []

            # 分批处理（API 有批次大小限制）
            total_batches = (len(texts) + batch_size - 1) // batch_size
            for i in range(0, len(texts), batch_size):
                batch = [self._truncate_text(t) for t in texts[i : i + batch_size]]
                batch_num = i // batch_size + 1

                logger.debug(
                    f"正在处理批次 {batch_num}/{total_batches}: {len(batch)} 个文本"
                )

                if observer is not None and context is not None:
                    response, _ = await observer.send_embedding(
                        lambda: self.client.embeddings.create(
                            model=settings.embedding_model,
                            input=batch,
                        ),
                        logical_call_id=logical_call_id or str(uuid4()),
                        requested={
                            "provider_id": self.provider,
                            "model_id": settings.embedding_model,
                            "protocol_family": "openai-compatible",
                            "endpoint_url": settings.embedding_base_url,
                        },
                        effective={
                            "provider_id": self.provider,
                            "model_id": settings.embedding_model,
                            "protocol_family": "openai-compatible",
                            "endpoint_url": settings.embedding_base_url,
                        },
                    )
                else:
                    response = await self.client.embeddings.create(
                        model=settings.embedding_model,
                        input=batch,
                    )

                # 提取嵌入向量
                batch_embeddings = [item.embedding for item in response.data]
                all_embeddings.extend(batch_embeddings)

            logger.debug("✅ 成功生成 {} 个嵌入向量", len(all_embeddings))
            return all_embeddings

        except Exception as e:
            logger.error(
                "❌ Embedding API 请求失败 (provider={}, model={}): type={}",
                self.provider,
                settings.embedding_model,
                type(e).__name__,
            )
            raise

    async def embed_query(
        self,
        query: str,
        *,
        context: InvocationContext | None = None,
        observer: Any = None,
        logical_call_id: str | None = None,
    ) -> list[float]:
        """生成查询文本的嵌入向量

        Args:
            query: 查询文本
            context: 可选的无 transcript InvocationContext
            observer: 可选的真实发送观察器

        Returns:
            嵌入向量
        """
        embeddings = await self.embed_texts(
            [query],
            context=context,
            observer=observer,
            logical_call_id=logical_call_id,
        )
        return embeddings[0] if embeddings else []

    async def close(self):
        """关闭客户端连接，释放资源"""
        clients = [self.client, *self._retired_clients]
        for client in clients:
            if client and hasattr(client, "close"):
                await client.close()
        self._retired_clients.clear()
        logger.debug("嵌入服务客户端已关闭")


class RerankerService:
    """重排序服务

    使用重排序模型对检索结果进行重新评分和排序。
    支持 SiliconFlow Rerank API。
    """

    def __init__(self):
        """初始化重排序服务"""
        self.provider = ""
        self.client = None
        self._client_config = None
        self._retired_clients = []
        self._refresh_client()

    def _refresh_client(self):
        """配置变化时刷新客户端，旧客户端延迟到服务关闭时释放。"""
        settings = get_settings()
        config = (
            settings.rerank_provider.lower(),
            settings.rerank_base_url,
            settings.rerank_api_key,
            settings.rerank_model,
        )
        if self._client_config == config:
            return

        if self.client is not None:
            self._retired_clients.append(self.client)
        self.provider = config[0]
        self.client = None
        self._init_client(settings)
        self._client_config = config

    def _init_client(self, settings):
        """根据配置初始化对应的客户端"""
        try:
            if self.provider == "siliconflow":
                # SiliconFlow Rerank API (使用 httpx)
                self.client = httpx.AsyncClient(
                    base_url=settings.rerank_base_url,
                    headers={"Authorization": f"Bearer {settings.rerank_api_key}"},
                    timeout=30.0,
                    follow_redirects=True,
                )
                logger.info(
                    f"✅ 重排序服务初始化成功: {self.provider} ({settings.rerank_model})"
                )

            elif self.provider == "none" or self.provider is None:
                # 禁用重排序
                logger.info("ℹ️  重排序服务已禁用")
                self.client = None

            else:
                logger.warning("⚠️  不支持的重排序提供商: {}，已禁用", self.provider)
                self.client = None

        except Exception as e:
            logger.warning("⚠️  重排序服务初始化失败: {}，已禁用", e)
            self.client = None

    async def rerank(
        self,
        query: str,
        docs: list[dict[str, any]],
        top_k: int | None = None,
        score_threshold: float | None = None,
    ) -> list[dict[str, any]]:
        """对检索结果重新排序

        Args:
            query: 查询文本
            docs: 待重排序的文档列表
            top_k: 返回前 K 个结果（默认使用配置值）
            score_threshold: 相似度阈值（默认使用配置值）

        Returns:
            重排序后的文档列表，如果所有文档都低于阈值，返回空列表
        """
        if not docs:
            return []

        self._refresh_client()
        settings = get_settings()
        # 使用配置的默认值
        top_k = top_k or settings.rerank_top_k
        score_threshold = score_threshold or settings.rerank_score_threshold

        # 如果重排序服务未启用，直接返回原结果
        if self.client is None:
            logger.debug("重排序服务未启用，返回原始结果")
            return docs[:top_k]

        try:
            if self.provider == "siliconflow":
                return await self._rerank_via_siliconflow(
                    query, docs, top_k, score_threshold
                )
            else:
                return docs[:top_k]

        except Exception as e:
            logger.warning("⚠️  重排序失败: {}，返回原始结果", e)
            return docs[:top_k]

    async def _rerank_via_siliconflow(
        self,
        query: str,
        docs: list[dict[str, any]],
        top_k: int,
        score_threshold: float,
    ) -> list[dict[str, any]]:
        """通过 SiliconFlow Rerank API 重排序"""
        try:
            settings = get_settings()
            # 提取文档内容
            texts = [doc["content"] for doc in docs]

            # 调用 Rerank API
            response = await self.client.post(
                "",
                json={
                    "model": settings.rerank_model,
                    "query": query,
                    "documents": texts,
                    "top_k": min(top_k, len(texts)),
                },
            )

            response.raise_for_status()
            results = response.json()

            # 解析结果
            if "results" not in results:
                logger.warning("Rerank API 返回格式异常")
                return docs[:top_k]

            # 过滤低于阈值的文档
            filtered_results = [
                r
                for r in results["results"]
                if r.get("relevance_score", 0) >= score_threshold
            ]

            if not filtered_results:
                logger.debug("所有文档都低于阈值 {}，返回空列表", score_threshold)
                return []

            # 根据返回的索引重新排序
            reranked_docs = [docs[r["index"]] for r in filtered_results[:top_k]]

            logger.debug(
                f"✅ 重排序完成: {len(docs)} -> {len(reranked_docs)} "
                f"(阈值: {score_threshold})"
            )
            return reranked_docs

        except httpx.HTTPError as e:
            logger.warning("SiliconFlow Rerank API 请求失败: {}", e)
            return docs[:top_k]
        except Exception as e:
            logger.warning("SiliconFlow 重排序失败: {}", e)
            return docs[:top_k]

    async def close(self):
        """关闭客户端连接"""
        clients = [self.client, *self._retired_clients]
        for client in clients:
            if client and hasattr(client, "aclose"):
                await client.aclose()
        self._retired_clients.clear()
        logger.debug("重排序服务客户端已关闭")


# 全局单例
_embedding_service_instance: EmbeddingService | None = None
_reranker_service_instance: RerankerService | None = None

# 线程锁，确保单例初始化的线程安全
_embedding_service_lock = threading.Lock()
_reranker_service_lock = threading.Lock()


def get_embedding_service() -> EmbeddingService:
    """获取嵌入服务单例（线程安全）"""
    global _embedding_service_instance
    if _embedding_service_instance is None:
        with _embedding_service_lock:
            # 双重检查锁定
            if _embedding_service_instance is None:
                _embedding_service_instance = EmbeddingService()
    return _embedding_service_instance


def get_reranker_service() -> RerankerService:
    """获取重排序服务单例（线程安全）"""
    global _reranker_service_instance
    if _reranker_service_instance is None:
        with _reranker_service_lock:
            # 双重检查锁定
            if _reranker_service_instance is None:
                _reranker_service_instance = RerankerService()
    return _reranker_service_instance


async def close_embedding_service():
    """关闭嵌入服务实例，释放资源"""
    global _embedding_service_instance
    if _embedding_service_instance is not None:
        await _embedding_service_instance.close()
        _embedding_service_instance = None


async def close_reranker_service():
    """关闭重排序服务实例，释放资源"""
    global _reranker_service_instance
    if _reranker_service_instance is not None:
        await _reranker_service_instance.close()
        _reranker_service_instance = None
