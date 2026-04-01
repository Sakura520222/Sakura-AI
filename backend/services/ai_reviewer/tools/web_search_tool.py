"""Web 搜索工具处理器

为 AI 审查员提供互联网搜索能力，用于查找文档、最佳实践等。
"""

from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from backend.core.config import get_settings


class WebSearchToolHandler:
    """Web 搜索工具处理器

    优先从 AppConfig 数据库读取配置（支持 WebUI 动态修改），
    未找到时回退到环境变量配置。
    """

    # 配置键名到 Settings 属性的映射
    _CONFIG_MAP = {
        "web_search_enabled": "web_search_enabled",
        "web_search_provider": "web_search_provider",
        "web_search_api_key": "web_search_api_key",
        "web_search_max_results": "web_search_max_results",
        "web_search_max_content_length": "web_search_max_content_length",
        "web_search_timeout": "web_search_timeout",
    }

    _CONFIG_CACHE_TTL = 60  # 配置缓存有效期（秒）

    def __init__(self) -> None:
        """初始化 Web 搜索工具"""
        settings = get_settings()
        # 从环境变量加载默认值
        self._provider: str = settings.web_search_provider
        self._api_key: str = settings.web_search_api_key
        self._max_results: int = settings.web_search_max_results
        self._max_content_length: int = settings.web_search_max_content_length
        self._timeout: int = settings.web_search_timeout
        self._last_config_load: float = 0.0

    async def _load_config(self) -> None:
        """从数据库加载配置（覆盖环境变量默认值），带 TTL 缓存"""
        import time

        if time.time() - self._last_config_load < self._CONFIG_CACHE_TTL:
            return

        try:
            from backend.models.database import AppConfig, async_session
            from sqlalchemy import select

            if async_session is None:
                return

            async with async_session() as session:
                keys = list(self._CONFIG_MAP.keys())
                result = await session.execute(
                    select(AppConfig).where(AppConfig.key_name.in_(keys))
                )
                configs = result.scalars().all()
                config_values = {c.key_name: c.key_value for c in configs}

            self._last_config_load = time.time()

            if not config_values:
                return

            if config_values.get("web_search_provider"):
                self._provider = config_values["web_search_provider"]
            if config_values.get("web_search_api_key"):
                self._api_key = config_values["web_search_api_key"]
            if config_values.get("web_search_max_results"):
                self._max_results = int(config_values["web_search_max_results"])
            if config_values.get("web_search_max_content_length"):
                self._max_content_length = int(
                    config_values["web_search_max_content_length"]
                )
            if config_values.get("web_search_timeout"):
                self._timeout = int(config_values["web_search_timeout"])

        except Exception as e:
            logger.debug(f"从数据库加载 Web 搜索配置失败，使用环境变量默认值: {e}")

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def max_results(self) -> int:
        return self._max_results

    @property
    def max_content_length(self) -> int:
        return self._max_content_length

    @property
    def timeout(self) -> int:
        return self._timeout

    async def search_web(
        self,
        query: str,
        top_k: Optional[int] = None,
    ) -> Dict[str, Any]:
        """执行 Web 搜索

        Args:
            query: 搜索查询关键词
            top_k: 返回结果数量（默认使用配置值）

        Returns:
            搜索结果字典
        """
        await self._load_config()
        max_results = top_k or self.max_results

        try:
            if self.provider == "tavily":
                results = await self._search_tavily(query, max_results)
            else:
                # 默认使用 DuckDuckGo
                results = await self._search_duckduckgo(query, max_results)

            return {
                "query": query,
                "results": results,
                "count": len(results),
                "provider": self.provider,
            }

        except Exception as e:
            logger.error(f"Web 搜索失败: {e}", exc_info=True)
            return {
                "query": query,
                "results": [],
                "count": 0,
                "error": str(e),
                "provider": self.provider,
            }

    async def _search_tavily(self, query: str, max_results: int) -> List[Dict]:
        """使用 Tavily API 搜索

        Args:
            query: 搜索查询
            max_results: 最大结果数

        Returns:
            搜索结果列表
        """
        if not self.api_key:
            logger.warning("Tavily API Key 未配置，无法执行搜索")
            return []

        url = "https://api.tavily.com/search"
        payload = {
            "query": query,
            "max_results": max_results,
            "include_answer": True,
            "api_key": self.api_key,
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

        results = []
        # 如果有 AI 总结，作为第一个结果
        if data.get("answer"):
            results.append(
                {
                    "title": "AI 摘要",
                    "url": "",
                    "content": self._truncate(data["answer"]),
                }
            )

        for item in data.get("results", []):
            results.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "content": self._truncate(item.get("content", "")),
                }
            )

        return results

    async def _search_duckduckgo(self, query: str, max_results: int) -> List[Dict]:
        """使用 DuckDuckGo 搜索（免费，无需 API Key）

        Args:
            query: 搜索查询
            max_results: 最大结果数

        Returns:
            搜索结果列表
        """
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            logger.warning(
                "duckduckgo-search 包未安装，无法执行搜索。"
                "请运行: pip install duckduckgo-search"
            )
            return []

        results = []
        try:
            with DDGS() as ddgs:
                for item in ddgs.text(query, max_results=max_results):
                    results.append(
                        {
                            "title": item.get("title", ""),
                            "url": item.get("href", ""),
                            "content": self._truncate(item.get("body", "")),
                        }
                    )
        except Exception as e:
            logger.error(f"DuckDuckGo 搜索出错: {e}")
            return []

        return results

    def _truncate(self, text: str) -> str:
        """截断文本到配置的最大长度"""
        if len(text) <= self.max_content_length:
            return text
        return text[: self.max_content_length] + "..."
