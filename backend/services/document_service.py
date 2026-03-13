"""文档管理服务

负责扫描、解析和处理项目文档：
- 扫描 .sakura/ 文件夹
- Markdown 文档解析
- 智能分块（基于标题 + 代码块保护）
- 文件 Hash 计算（增量更新）
"""

from typing import List, Dict, Any, Optional
from pathlib import Path
from loguru import logger
import hashlib
import re

from backend.core.config import get_settings

settings = get_settings()


class DocumentService:
    """文档管理服务

    处理文档的扫描、解析、分块和预处理。
    """

    def __init__(self):
        """初始化文档服务"""
        self.chunk_size = settings.chunk_size
        self.chunk_overlap = settings.chunk_overlap
        self.max_chunks_per_doc = settings.max_chunks_per_doc

    async def scan_sakura_directory(self, repo_path: str) -> List[Dict[str, Any]]:
        """扫描 .sakura/ 文件夹，获取所有 Markdown 文件

        Args:
            repo_path: 仓库根目录路径

        Returns:
            文件信息列表，每个文件包含：
                - file_path: 相对路径
                - full_path: 绝对路径
                - file_size: 文件大小
                - file_hash: MD5 Hash
        """
        sakura_dir = Path(repo_path) / ".sakura"

        if not sakura_dir.exists():
            logger.info(f"仓库中不存在 .sakura/ 文件夹: {repo_path}")
            return []

        if not sakura_dir.is_dir():
            logger.warning(f".sakura 不是目录: {sakura_dir}")
            return []

        # 递归查找所有 .md 文件
        md_files = list(sakura_dir.rglob("*.md"))

        if not md_files:
            logger.info(f".sakura/ 文件夹中没有 Markdown 文件: {repo_path}")
            return []

        # 收集文件信息
        files_info = []
        for md_file in md_files:
            try:
                relative_path = str(md_file.relative_to(repo_path))
                file_size = md_file.stat().st_size
                file_hash = await self.calculate_file_hash(str(md_file))

                files_info.append(
                    {
                        "file_path": relative_path,
                        "full_path": str(md_file),
                        "file_size": file_size,
                        "file_hash": file_hash,
                    }
                )

            except Exception as e:
                logger.warning(f"读取文件失败 {md_file}: {e}")

        logger.info(f"📄 扫描到 {len(files_info)} 个 Markdown 文件")
        return files_info

    async def calculate_file_hash(self, file_path: str) -> str:
        """计算文件的 MD5 Hash

        Args:
            file_path: 文件路径

        Returns:
            MD5 Hash 字符串（16进制）
        """
        try:
            md5_hash = hashlib.md5()

            with open(file_path, "rb") as f:
                # 分块读取大文件
                for chunk in iter(lambda: f.read(4096), b""):
                    md5_hash.update(chunk)

            return md5_hash.hexdigest()

        except Exception as e:
            logger.error(f"计算文件 Hash 失败 {file_path}: {e}")
            return ""

    async def parse_markdown_documents(
        self, files_info: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """解析 Markdown 文档

        Args:
            files_info: 文件信息列表

        Returns:
            解析后的文档列表，每个文档包含：
                - file_path: 文件路径
                - content: 原始内容
                - metadata: 元数据
        """
        documents = []

        for file_info in files_info:
            try:
                # 读取文件内容
                with open(file_info["full_path"], "r", encoding="utf-8") as f:
                    content = f.read()

                # 提取元数据
                metadata = {
                    "file_path": file_info["file_path"],
                    "file_size": file_info["file_size"],
                    "file_hash": file_info["file_hash"],
                    "title": self._extract_title(content, file_info["file_path"]),
                }

                documents.append(
                    {
                        "file_path": file_info["file_path"],
                        "content": content,
                        "metadata": metadata,
                    }
                )

            except Exception as e:
                logger.warning(f"解析文档失败 {file_info['file_path']}: {e}")

        logger.info(f"✅ 成功解析 {len(documents)} 个文档")
        return documents

    def _extract_title(self, content: str, file_path: str) -> str:
        """从文档中提取标题

        优先级：
        1. 第一个 # 标题
        2. 文件名

        Args:
            content: 文档内容
            file_path: 文件路径

        Returns:
            标题字符串
        """
        # 尝试提取第一个 # 标题
        match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        if match:
            return match.group(1).strip()

        # 使用文件名作为标题
        return Path(file_path).stem

    async def chunk_document_by_headers(
        self, content: str, metadata: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """基于 Markdown 标题进行智能分块

        特性：
        - 按标题（#、##、###）切分
        - 保护代码块不被截断
        - 超长块进一步细分

        Args:
            content: 文档内容
            metadata: 文档元数据

        Returns:
            分块列表，每个块包含：
                - content: 块内容
                - metadata: 元数据（包含标题、层级等）
        """
        chunks = []
        lines = content.split("\n")
        current_chunk = []
        current_header = "文档开头"
        current_header_level = 0
        in_code_block = False
        code_fence = None

        for line in lines:
            # 检测代码块开始/结束
            if line.startswith("```"):
                if not in_code_block:
                    in_code_block = True
                    code_fence = line[:3]  # 记住 fence 类型
                elif line[:3] == code_fence:
                    in_code_block = False
                    code_fence = None

            # 检测标题（不在代码块内）
            is_header = False
            header_level = 0
            if not in_code_block:
                header_match = re.match(r"^(#{1,6})\s+(.+)$", line)
                if header_match:
                    is_header = True
                    header_level = len(header_match.group(1))
                    header_title = header_match.group(2).strip()

            # 如果遇到标题且当前块不为空
            if is_header and current_chunk:
                # 保存当前块
                chunk_content = "\n".join(current_chunk).strip()
                if chunk_content:
                    chunks.append(
                        {
                            "content": chunk_content,
                            "metadata": {
                                **metadata,
                                "header": current_header,
                                "header_level": current_header_level,
                            },
                        }
                    )

                # 开始新块
                current_chunk = [line]
                current_header = header_title
                current_header_level = header_level
            else:
                current_chunk.append(line)

        # 保存最后一块
        if current_chunk:
            chunk_content = "\n".join(current_chunk).strip()
            if chunk_content:
                chunks.append(
                    {
                        "content": chunk_content,
                        "metadata": {
                            **metadata,
                            "header": current_header,
                            "header_level": current_header_level,
                        },
                    }
                )

        # 处理超长块
        chunks = await self._split_long_chunks(chunks, metadata)

        logger.debug(f"📦 文档已分块: {len(chunks)} 个块")
        return chunks

    async def _split_long_chunks(
        self, chunks: List[Dict[str, Any]], metadata: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """分割超长的块

        Args:
            chunks: 原始块列表
            metadata: 文档元数据

        Returns:
            处理后的块列表
        """
        result = []

        for chunk in chunks:
            content = chunk["content"]
            chunk_metadata = chunk["metadata"]

            # 如果块长度小于阈值，直接保留
            if len(content) <= self.chunk_size:
                result.append(chunk)
                continue

            # 分割超长块
            logger.debug(f"分割超长块: {len(content)} 字符")

            # 按段落分割
            paragraphs = content.split("\n\n")
            current_chunk = ""

            for para in paragraphs:
                # 如果单个段落就超过阈值，强制分割
                if len(para) > self.chunk_size:
                    # 按句子分割
                    sentences = re.split(r"(?<=[.!?。！？])\s+", para)
                    for sentence in sentences:
                        if len(current_chunk) + len(sentence) > self.chunk_size:
                            if current_chunk:
                                result.append(
                                    {
                                        "content": current_chunk.strip(),
                                        "metadata": chunk_metadata,
                                    }
                                )
                                current_chunk = sentence + "\n\n"
                            else:
                                # 单个句子就超过阈值，强制添加
                                result.append(
                                    {
                                        "content": sentence.strip(),
                                        "metadata": chunk_metadata,
                                    }
                                )
                        else:
                            current_chunk += sentence + "\n\n"
                else:
                    # 检查是否超过阈值
                    if len(current_chunk) + len(para) > self.chunk_size:
                        if current_chunk:
                            result.append(
                                {
                                    "content": current_chunk.strip(),
                                    "metadata": chunk_metadata,
                                }
                            )
                            current_chunk = para + "\n\n"
                        else:
                            # 单个段落就超过阈值
                            result.append(
                                {
                                    "content": para.strip(),
                                    "metadata": chunk_metadata,
                                }
                            )
                    else:
                        current_chunk += para + "\n\n"

            # 添加最后一块
            if current_chunk:
                result.append(
                    {
                        "content": current_chunk.strip(),
                        "metadata": chunk_metadata,
                    }
                )

        # 检查是否超过最大块数限制
        if len(result) > self.max_chunks_per_doc:
            logger.warning(
                f"文档块数 {len(result)} 超过限制 {self.max_chunks_per_doc}，将截断"
            )
            result = result[: self.max_chunks_per_doc]

        return result

    async def prepare_documents_for_indexing(
        self, documents: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """准备用于索引的文档

        将文档分块并添加索引所需的元数据。

        Args:
            documents: 文档列表

        Returns:
            准备好索引的块列表
        """
        all_chunks = []

        for doc in documents:
            try:
                # 分块
                chunks = await self.chunk_document_by_headers(
                    doc["content"], doc["metadata"]
                )

                # 为每个块添加唯一 ID
                for i, chunk in enumerate(chunks):
                    chunk_id = f"{doc['metadata']['file_path']}#{i}"
                    all_chunks.append(
                        {
                            "id": chunk_id,
                            "content": chunk["content"],
                            "metadata": chunk["metadata"],
                        }
                    )

            except Exception as e:
                logger.warning(f"处理文档失败 {doc['metadata'].get('file_path')}: {e}")

        logger.info(f"📦 准备了 {len(all_chunks)} 个文档块用于索引")
        return all_chunks


# 全局单例
_document_service_instance: Optional[DocumentService] = None


def get_document_service() -> DocumentService:
    """获取文档服务单例"""
    global _document_service_instance
    if _document_service_instance is None:
        _document_service_instance = DocumentService()
    return _document_service_instance
