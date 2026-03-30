"""搜索工具处理器

从原 ai_reviewer.py 迁移的搜索工具相关方法：
- _tool_search_project_docs (2695-2768行)
- _tool_search_code_context (2770-2855行)
"""

from typing import Any, Dict, List, Optional

from loguru import logger

from backend.core.config import get_settings


class SearchToolHandler:
    """搜索工具处理器

    负责处理文档搜索和代码上下文搜索工具调用。
    """

    async def search_project_docs(
        self, query: str, top_k: int, repo: Any, pr: Any
    ) -> Dict[str, Any]:
        """检索项目文档的工具实现

        Args:
            query: 检索查询
            top_k: 返回结果数量
            repo: GitHub仓库对象
            pr: GitHub PR对象

        Returns:
            检索结果字典
        """
        try:
            settings = get_settings()

            # 检查 RAG 功能是否启用
            if not settings.enable_rag:
                return {
                    "query": query,
                    "error": "文档检索功能未启用",
                    "hint": "请联系管理员启用 RAG 功能",
                }

            # 导入 RAG 服务（延迟导入避免循环依赖）
            from backend.services.rag_service import get_rag_service

            rag_service = get_rag_service()

            # 构造仓库名称
            repo_full_name = f"{repo.owner.login}/{repo.name}"

            # 执行检索
            logger.info(
                f"🔍 检索项目文档: {repo_full_name}, query: {query[:50]}..."
            )
            docs = await rag_service.search_relevant_docs(
                repo_full_name=repo_full_name,
                query=query,
                top_k=top_k,
                use_rerank=True,
            )

            if not docs:
                return {
                    "query": query,
                    "results": [],
                    "message": "未找到相关文档",
                    "hint": "项目文档库中可能不包含该主题的规范，建议基于通用最佳实践进行审查",
                }

            # 格式化返回结果
            formatted_results = []
            for doc in docs:
                formatted_results.append(
                    {
                        "content": doc["content"],
                        "metadata": doc["metadata"],
                        "source": doc["metadata"].get("file_path", "unknown"),
                    }
                )

            logger.info(f"✅ 检索到 {len(formatted_results)} 个相关文档")

            return {
                "query": query,
                "results": formatted_results,
                "count": len(formatted_results),
            }

        except Exception as e:
            logger.error(f"文档检索失败: {e}", exc_info=True)
            return {
                "query": query,
                "error": f"检索失败: {str(e)}",
                "hint": "可能是向量数据库未初始化或配置错误，请检查系统状态",
            }

    async def search_code_context(
        self,
        query: str,
        language: Optional[str],
        file_path: Optional[str],
        top_k: int,
        repo: Any,
        pr: Any,
    ) -> Dict[str, Any]:
        """检索代码上下文的工具实现

        Args:
            query: 检索查询
            language: 编程语言过滤（可选）
            file_path: 文件路径过滤（可选）
            top_k: 返回结果数量
            repo: GitHub仓库对象
            pr: GitHub PR对象

        Returns:
            检索结果字典
        """
        try:
            # 导入代码索引服务（延迟导入避免循环依赖）
            from backend.services.code_index_service import get_code_index_service

            code_index_service = get_code_index_service()

            # 构造仓库名称
            repo_full_name = f"{repo.owner.login}/{repo.name}"

            # 执行检索
            logger.info(
                f"🔍 检索代码上下文: {repo_full_name}, query: {query[:50]}..., "
                f"language: {language or 'all'}, file: {file_path or 'all'}"
            )

            results = await code_index_service.search_code_context(
                repo_full_name=repo_full_name,
                query=query,
                top_k=top_k,
                language=language,
                file_path=file_path,
                pr_number=pr.number if pr else None,
            )

            if not results:
                return {
                    "query": query,
                    "results": [],
                    "message": "未找到相关代码片段",
                    "hint": "代码库中可能不包含相关实现，或该文件尚未被索引。可以尝试使用 read_file 查看具体文件。",
                }

            # 格式化返回结果
            formatted_results = []
            for result in results:
                metadata = result.get("metadata", {})
                formatted_results.append(
                    {
                        "content": result["content"],
                        "file_path": metadata.get("file_path", "unknown"),
                        "language": metadata.get("language", "unknown"),
                        "start_line": metadata.get("start_line"),
                        "end_line": metadata.get("end_line"),
                        "function_name": metadata.get("function_name"),
                        "class_name": metadata.get("class_name"),
                        "distance": result.get("distance"),
                    }
                )

            logger.info(f"✅ 检索到 {len(formatted_results)} 个相关代码片段")

            return {
                "query": query,
                "results": formatted_results,
                "count": len(formatted_results),
            }

        except Exception as e:
            logger.error(f"代码上下文检索失败: {e}", exc_info=True)
            return {
                "query": query,
                "error": f"检索失败: {str(e)}",
                "hint": "可能是代码索引未初始化或配置错误，请检查系统状态",
            }
