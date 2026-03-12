"""嵌入服务

支持多种嵌入模型提供商：
- SiliconFlow (默认): BAAI/bge-m3
- OpenAI: text-embedding-3-small/large
- Ollama: 本地模型
- HuggingFace: 本地模型
"""

from typing import List, Optional, Dict
from loguru import logger
from openai import AsyncOpenAI
import httpx

from backend.core.config import get_settings

settings = get_settings()


class EmbeddingService:
    """嵌入服务

    将文本转换为向量嵌入，支持多种提供商。
    """

    def __init__(self):
        """初始化嵌入服务"""
        self.provider = settings.embedding_provider.lower()
        self.client = None
        self._init_client()

    def _init_client(self):
        """根据配置初始化对应的客户端"""
        try:
            if self.provider == "siliconflow":
                # SiliconFlow 使用 OpenAI 兼容 API
                self.client = AsyncOpenAI(
                    base_url=settings.embedding_base_url,
                    api_key=settings.embedding_api_key,
                )
                logger.info(
                    f"✅ 嵌入服务初始化成功: {self.provider} ({settings.embedding_model})"
                )

            elif self.provider == "openai":
                # OpenAI 官方 API
                self.client = AsyncOpenAI(
                    base_url=settings.embedding_base_url,
                    api_key=settings.embedding_api_key,
                )
                logger.info(
                    f"✅ 嵌入服务初始化成功: {self.provider} ({settings.embedding_model})"
                )

            elif self.provider == "ollama":
                # Ollama 本地 API（也兼容 OpenAI 格式）
                self.client = AsyncOpenAI(
                    base_url=settings.embedding_base_url,
                    api_key=settings.embedding_api_key or "ollama",  # Ollama 不需要 key
                )
                logger.info(f"✅ 嵌入服务初始化成功: {self.provider}")

            elif self.provider == "hf" or self.provider == "huggingface":
                # HuggingFace 本地模型（使用 sentence-transformers）
                logger.info("🔄 嵌入服务使用 HuggingFace 本地模型（按需加载）")

            else:
                raise ValueError(f"不支持的嵌入提供商: {self.provider}")

        except Exception as e:
            logger.error(f"❌ 嵌入服务初始化失败: {e}")
            raise

    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """批量生成文本嵌入向量

        Args:
            texts: 文本列表

        Returns:
            嵌入向量列表，每个向量是一个 float 数组
        """
        if not texts:
            return []

        try:
            if self.provider in ["siliconflow", "openai", "ollama"]:
                # 使用 OpenAI 兼容 API
                return await self._embed_via_openai_api(texts)

            elif self.provider in ["hf", "huggingface"]:
                # 使用 HuggingFace 本地模型
                return await self._embed_via_huggingface(texts)

            else:
                raise ValueError(f"不支持的嵌入提供商: {self.provider}")

        except Exception as e:
            logger.error(f"❌ 生成嵌入向量失败: {e}")
            raise

    async def _embed_via_openai_api(self, texts: List[str]) -> List[List[float]]:
        """通过 OpenAI 兼容 API 生成嵌入（支持批处理）

        支持：SiliconFlow、OpenAI、Ollama
        """
        try:
            batch_size = settings.embedding_batch_size
            all_embeddings = []

            # 分批处理（API 有批次大小限制）
            total_batches = (len(texts) + batch_size - 1) // batch_size
            for i in range(0, len(texts), batch_size):
                batch = texts[i : i + batch_size]
                batch_num = i // batch_size + 1

                logger.debug(
                    f"正在处理批次 {batch_num}/{total_batches}: {len(batch)} 个文本"
                )

                response = await self.client.embeddings.create(
                    model=settings.embedding_model,
                    input=batch,
                )

                # 提取嵌入向量
                batch_embeddings = [item.embedding for item in response.data]
                all_embeddings.extend(batch_embeddings)

            logger.debug(f"✅ 成功生成 {len(all_embeddings)} 个嵌入向量")
            return all_embeddings

        except Exception as e:
            logger.error(f"❌ OpenAI API 嵌入失败: {e}")
            raise

    async def _embed_via_huggingface(self, texts: List[str]) -> List[List[float]]:
        """通过 HuggingFace 本地模型生成嵌入

        使用 sentence-transformers 库。
        """
        try:
            from sentence_transformers import SentenceTransformer

            # 懒加载模型（只在第一次使用时加载）
            if not hasattr(self, "_hf_model"):
                logger.info(f"🔄 加载 HuggingFace 模型: {settings.embedding_model}")
                self._hf_model = SentenceTransformer(settings.embedding_model)
                logger.info("✅ HuggingFace 模型加载完成")

            # 生成嵌入
            embeddings = self._hf_model.encode(texts, convert_to_numpy=True)
            embeddings = embeddings.tolist()

            logger.debug(f"✅ 成功生成 {len(embeddings)} 个嵌入向量")
            return embeddings

        except ImportError:
            logger.error(
                "❌ sentence-transformers 未安装，请运行: pip install sentence-transformers"
            )
            raise RuntimeError("sentence-transformers 未安装")
        except Exception as e:
            logger.error(f"❌ HuggingFace 嵌入失败: {e}")
            raise

    async def embed_query(self, query: str) -> List[float]:
        """生成查询文本的嵌入向量

        Args:
            query: 查询文本

        Returns:
            嵌入向量
        """
        embeddings = await self.embed_texts([query])
        return embeddings[0] if embeddings else []


class RerankerService:
    """重排序服务

    使用重排序模型对检索结果进行重新评分和排序。
    支持 SiliconFlow Rerank API。
    """

    def __init__(self):
        """初始化重排序服务"""
        self.provider = settings.rerank_provider.lower()
        self.client = None
        self._init_client()

    def _init_client(self):
        """根据配置初始化对应的客户端"""
        try:
            if self.provider == "siliconflow":
                # SiliconFlow Rerank API (使用 httpx)
                self.client = httpx.AsyncClient(
                    base_url=settings.rerank_base_url,
                    headers={"Authorization": f"Bearer {settings.rerank_api_key}"},
                    timeout=30.0,
                )
                logger.info(
                    f"✅ 重排序服务初始化成功: {self.provider} ({settings.rerank_model})"
                )

            elif self.provider == "none" or self.provider is None:
                # 禁用重排序
                logger.info("ℹ️  重排序服务已禁用")
                self.client = None

            else:
                logger.warning(f"⚠️  不支持的重排序提供商: {self.provider}，已禁用")
                self.client = None

        except Exception as e:
            logger.warning(f"⚠️  重排序服务初始化失败: {e}，已禁用")
            self.client = None

    async def rerank(
        self,
        query: str,
        docs: List[Dict[str, any]],
        top_k: Optional[int] = None,
        score_threshold: Optional[float] = None,
    ) -> List[Dict[str, any]]:
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
            logger.warning(f"⚠️  重排序失败: {e}，返回原始结果")
            return docs[:top_k]

    async def _rerank_via_siliconflow(
        self,
        query: str,
        docs: List[Dict[str, any]],
        top_k: int,
        score_threshold: float,
    ) -> List[Dict[str, any]]:
        """通过 SiliconFlow Rerank API 重排序"""
        try:
            # 提取文档内容
            texts = [doc["content"] for doc in docs]

            # 调用 Rerank API
            response = await self.client.post(
                "/",
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
                logger.debug(f"所有文档都低于阈值 {score_threshold}，返回空列表")
                return []

            # 根据返回的索引重新排序
            reranked_docs = [docs[r["index"]] for r in filtered_results[:top_k]]

            logger.debug(
                f"✅ 重排序完成: {len(docs)} -> {len(reranked_docs)} "
                f"(阈值: {score_threshold})"
            )
            return reranked_docs

        except httpx.HTTPError as e:
            logger.warning(f"SiliconFlow Rerank API 请求失败: {e}")
            return docs[:top_k]
        except Exception as e:
            logger.warning(f"SiliconFlow 重排序失败: {e}")
            return docs[:top_k]

    async def close(self):
        """关闭客户端连接"""
        if self.client:
            await self.client.aclose()
            logger.debug("重排序服务客户端已关闭")


# 全局单例
_embedding_service_instance: Optional[EmbeddingService] = None
_reranker_service_instance: Optional[RerankerService] = None


def get_embedding_service() -> EmbeddingService:
    """获取嵌入服务单例"""
    global _embedding_service_instance
    if _embedding_service_instance is None:
        _embedding_service_instance = EmbeddingService()
    return _embedding_service_instance


def get_reranker_service() -> RerankerService:
    """获取重排序服务单例"""
    global _reranker_service_instance
    if _reranker_service_instance is None:
        _reranker_service_instance = RerankerService()
    return _reranker_service_instance
