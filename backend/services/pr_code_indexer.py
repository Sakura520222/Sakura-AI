"""PR代码索引器

负责在PR审查时自动索引变更的代码文件
"""

from typing import Dict, Any, Optional
from loguru import logger

from backend.services.code_index_service import get_code_index_service
from backend.core.github_app import GitHubAppClient


class PRCodeIndexer:
    """PR代码索引器

    在PR审查时自动索引变更的代码文件
    """

    def __init__(self):
        self.code_index_service = get_code_index_service()
        self.github_app = GitHubAppClient()

    async def index_pr_changes(
        self,
        repo_full_name: str,
        pr_number: int,
        install_id: int,
    ) -> Dict[str, Any]:
        """索引PR的变更文件

        Args:
            repo_full_name: 仓库名称
            pr_number: PR编号
            install_id: GitHub App安装ID

        Returns:
            索引结果统计
        """
        try:
            logger.info(f"开始索引PR #{pr_number}的代码变更，仓库: {repo_full_name}")

            # 解析仓库所有者名称
            owner, repo_name = repo_full_name.split("/")

            # 获取GitHub客户端
            client = self.github_app.get_repo_client(owner, repo_name)
            if not client:
                logger.error(f"无法获取仓库 {repo_full_name} 的GitHub客户端")
                return {
                    "indexed": 0,
                    "skipped": 0,
                    "failed": 0,
                    "total_chunks": 0,
                    "error": "无法获取GitHub客户端",
                }

            # 获取仓库和PR
            repo_api = client.get_repo(repo_full_name)
            pr = repo_api.get_pull(pr_number)
            commit_sha = pr.head.sha

            # 获取PR的文件列表
            files = pr.get_files()
            file_list = []

            for file in files:
                # 只索引代码文件
                if self._is_code_file(file.filename):
                    file_info = {
                        "path": file.filename,
                        "status": file.status,
                    }

                    # 对于新增和修改的文件，获取内容
                    if file.status in ("added", "modified"):
                        try:
                            # 尝试从PR的HEAD分支获取文件内容
                            content_file = repo_api.get_contents(
                                file.filename, ref=pr.head.sha
                            )
                            if content_file:
                                # 解码base64内容
                                import base64

                                content = base64.b64decode(content_file.content).decode(
                                    "utf-8", errors="ignore"
                                )
                                file_info["content"] = content
                        except Exception as e:
                            logger.warning(f"无法获取文件 {file.filename} 的内容: {e}")

                    file_list.append(file_info)

            if not file_list:
                logger.info(f"PR #{pr_number} 没有需要索引的代码文件")
                return {"indexed": 0, "skipped": 0, "failed": 0, "total_chunks": 0}

            # 执行索引
            result = await self.code_index_service.index_pr_changes(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                files=file_list,
                commit_sha=commit_sha,
            )

            logger.info(
                f"PR #{pr_number} 代码索引完成: "
                f"索引={result['indexed']}, 跳过={result['skipped']}, "
                f"失败={result['failed']}, 代码块={result['total_chunks']}"
            )

            return result

        except Exception as e:
            logger.error(f"索引PR #{pr_number} 代码失败: {e}", exc_info=True)
            return {
                "indexed": 0,
                "skipped": 0,
                "failed": 0,
                "total_chunks": 0,
                "error": str(e),
            }

    def _is_code_file(self, file_path: str) -> bool:
        """判断是否为代码文件

        Args:
            file_path: 文件路径

        Returns:
            是否为代码文件
        """
        from backend.services.code_parser_service import CodeParserService

        # 获取文件扩展名
        from pathlib import Path

        ext = Path(file_path).suffix.lower()

        # 检查是否在支持的语言列表中
        for extensions in CodeParserService.LANGUAGE_MAP.values():
            if ext in extensions:
                return True

        return False

    async def cleanup_pr_index(
        self,
        repo_full_name: str,
        pr_number: int,
    ) -> bool:
        """清理PR的代码索引

        当PR关闭或合并后，可以选择清理该PR的临时索引

        Args:
            repo_full_name: 仓库名称
            pr_number: PR编号

        Returns:
            是否清理成功
        """
        try:
            # 注意：根据设计，代码索引是永久保存的
            # 这里只提供清理选项，实际使用时可以配置是否清理

            # 如果需要清理，可以调用：
            # deleted_count = await self.code_index_service.vector_store.delete_by_pr(
            #     repo_full_name, pr_number
            # )

            logger.info(f"PR #{pr_number} 的代码索引将保留（永久保存策略）")
            return True

        except Exception as e:
            logger.error(f"清理PR #{pr_number} 代码索引失败: {e}")
            return False


# 全局单例
_pr_code_indexer_instance: Optional[PRCodeIndexer] = None


def get_pr_code_indexer() -> PRCodeIndexer:
    """获取PR代码索引器单例"""
    global _pr_code_indexer_instance
    if _pr_code_indexer_instance is None:
        _pr_code_indexer_instance = PRCodeIndexer()
    return _pr_code_indexer_instance
