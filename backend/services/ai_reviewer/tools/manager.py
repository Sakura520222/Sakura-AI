"""工具管理器

从原 ai_reviewer.py 迁移的工具管理方法：
- _get_enabled_tools (1388-1474行)
"""

from typing import Any, Dict, List, Optional

from loguru import logger

from backend.core.config import get_settings, get_strategy_config
from backend.services.ai_reviewer.constants import (
    BASE_TOOLS,
    CODE_INDEX_TOOLS,
    RAG_TOOLS,
    TOOL_NAME_TO_DEFINITION,
)


class ToolManager:
    """工具管理器

    负责管理工具的启用/禁用状态，根据全局配置和仓库状态动态决定可用工具。
    """

    def __init__(self):
        """初始化工具管理器"""
        pass

    def get_all_tools_definitions(self) -> List[Dict[str, Any]]:
        """获取所有工具定义

        Returns:
            所有工具的完整定义列表
        """
        return list(TOOL_NAME_TO_DEFINITION.values())

    async def get_enabled_tools(
        self, repo_full_name: Optional[str]
    ) -> List[Dict[str, Any]]:
        """获取启用的工具列表

        根据全局配置和仓库状态动态决定可用工具：
        - read_file: 始终可用
        - list_directory: 始终可用
        - search_project_docs: 仅当 RAG 全局启用且仓库有知识库索引时可用
        - search_code_context: 仅当仓库有代码索引时可用

        Args:
            repo_full_name: 仓库名称 (如 "owner/repo")

        Returns:
            启用的工具列表
        """
        # 基础工具（始终可用）
        base_tools = [
            TOOL_NAME_TO_DEFINITION[name] for name in BASE_TOOLS
            if name in TOOL_NAME_TO_DEFINITION
        ]

        enabled_tools = base_tools.copy()

        # 处理 repo_full_name 为 None 的情况
        if not repo_full_name:
            logger.debug("仓库名称为空，仅使用基础工具")
            return base_tools

        try:
            settings = get_settings()

            # 检查 search_project_docs 工具
            if settings.enable_rag:
                from backend.services.rag_service import get_rag_service

                rag_service = get_rag_service()
                index_status = await rag_service.get_index_status(repo_full_name)

                if (
                    index_status.get("indexed", False)
                    and index_status.get("document_count", 0) > 0
                ):
                    logger.debug(
                        f"仓库 {repo_full_name} 有知识库索引 ({index_status['document_count']} 个文档)，"
                        "启用 search_project_docs 工具"
                    )
                    rag_tool = TOOL_NAME_TO_DEFINITION.get("search_project_docs")
                    if rag_tool:
                        enabled_tools.append(rag_tool)

            # 检查 search_code_context 工具
            if settings.enable_code_index:
                from backend.services.code_index_service import (
                    get_code_index_service,
                )

                code_index_service = get_code_index_service()
                code_count = (
                    await code_index_service.vector_store.get_collection_count(
                        repo_full_name
                    )
                )

                if code_count > 0:
                    logger.debug(
                        f"仓库 {repo_full_name} 有代码索引 ({code_count} 个代码块)，"
                        "启用 search_code_context 工具"
                    )
                    code_tool = TOOL_NAME_TO_DEFINITION.get("search_code_context")
                    if code_tool:
                        enabled_tools.append(code_tool)

        except Exception as e:
            # 索引状态检查失败时，保守策略：仅使用基础工具
            logger.warning(
                f"检查仓库 {repo_full_name} 索引状态失败: {e}，仅使用基础工具"
            )

        return enabled_tools
