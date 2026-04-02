"""PR分析服务"""

import asyncio
from typing import Dict, List, Optional, Tuple, TypedDict

try:
    from typing import NotRequired
except ImportError:
    from typing_extensions import NotRequired
from dataclasses import dataclass
from loguru import logger

from backend.core.config import get_settings, get_strategy_config
from backend.core.github_app import GitHubAppClient


class CommitInfo(TypedDict):
    """提交信息"""

    sha: str
    title: str
    message: str
    body: NotRequired[str]  # commit 正文，可能为空
    author: str


settings = get_settings()


@dataclass
class PRFileInfo:
    """文件变更信息"""

    path: str
    status: str  # added, modified, deleted, renamed
    additions: int
    deletions: int
    changes: int
    patch: Optional[str] = None
    is_code_file: bool = False


@dataclass
class PRAnalysis:
    """PR分析结果"""

    pr_id: int
    pr_number: int
    repo_full_name: str

    # 统计信息
    total_files: int
    total_additions: int
    total_deletions: int
    total_changes: int

    # 代码文件统计
    code_files: List[PRFileInfo]
    code_file_count: int
    code_changes: int

    # 策略判断
    strategy: str
    should_skip: bool
    skip_reason: Optional[str] = None

    # Diff 安全区：每个文件的变更行号白名单
    # 格式：{"file_path.py": {10, 15, 20, 25}, "another.py": {5, 10}}
    changed_lines_map: Dict[str, set] = None

    # Hunk 边界：每个文件的 hunk 行范围列表，用于检测跨 hunk 多行评论
    # 格式：{"file.py": [(10, 20), (50, 65)], ...}
    hunk_boundaries: Dict[str, List[Tuple[int, int]]] = None

    # 增量审查标记
    is_incremental: bool = False

    # 增量审查时的新提交信息
    new_commits: Optional[List[CommitInfo]] = None


class PRAnalyzer:
    """PR分析器"""

    def __init__(self):
        self.github_app = GitHubAppClient()

    async def analyze_pr(self, pr_info: Dict[str, any]) -> PRAnalysis:
        """分析PR并返回分析结果（将 PyGithub 同步调用移到线程池避免阻塞事件循环）"""
        return await asyncio.to_thread(self._analyze_pr_sync, pr_info)

    def _analyze_pr_sync(self, pr_info: Dict[str, any]) -> PRAnalysis:
        """同步执行 PR 分析（在线程池中运行）"""
        try:
            # 获取GitHub客户端
            client = self.github_app.get_repo_client(
                pr_info["repo_owner"], pr_info["repo_name"]
            )
            if not client:
                raise Exception("无法获取GitHub客户端")

            # 获取仓库和PR
            repo = client.get_repo(pr_info["repo_full_name"])
            pr = repo.get_pull(pr_info["pr_number"])

            logger.info(
                f"开始分析PR: {pr_info['repo_full_name']}#{pr_info['pr_number']}"
            )

            # 判断是否为增量审查（synchronize 事件且 before SHA 有效）
            action = pr_info.get("action")
            before_sha = pr_info.get("before")
            after_sha = pr_info.get("after")
            is_incremental = (
                action == "synchronize"
                and before_sha
                and after_sha
                and before_sha != "0" * 40
            )

            # 获取文件变更
            new_commits = None
            if is_incremental:
                # 增量审查：只获取两次提交之间的变更
                comparison = repo.compare(before_sha, after_sha)
                file_list = list(comparison.files)

                # 提取新提交的标题和消息
                new_commits = []
                for commit in comparison.commits:
                    commit_msg = commit.commit.message.strip()
                    # 取第一行作为标题，其余作为正文
                    first_newline = commit_msg.find("\n")
                    title = (
                        commit_msg[:first_newline]
                        if first_newline != -1
                        else commit_msg
                    )
                    body = (
                        commit_msg[first_newline + 1 :].strip()
                        if first_newline != -1
                        else ""
                    )
                    new_commits.append(
                        {
                            "sha": commit.sha[:8],
                            "message": commit_msg,
                            "title": title,
                            "body": body,
                            "author": commit.commit.author.name
                            if commit.commit.author
                            else "Unknown",
                        }
                    )

                logger.info(
                    f"增量审查模式: 对比 {before_sha[:8]}... → {after_sha[:8]}..., "
                    f"变更文件数: {len(file_list)}, 新提交数: {len(new_commits)}"
                )
            else:
                # 全量审查
                files = pr.get_files()
                file_list = list(files)

            # 分析文件
            code_files = []
            total_additions = 0
            total_deletions = 0
            total_changes = 0

            for file in file_list:
                file_info = PRFileInfo(
                    path=file.filename,
                    status=file.status,
                    additions=file.additions,
                    deletions=file.deletions,
                    changes=file.changes,
                    patch=file.patch if hasattr(file, "patch") else None,
                    is_code_file=get_strategy_config().is_code_file(file.filename),
                )

                # 检查是否应该跳过
                if get_strategy_config().should_skip_file(file.filename):
                    logger.debug(f"跳过文件: {file.filename}")
                    continue

                total_additions += file.additions
                total_deletions += file.deletions
                total_changes += file.changes

                # 只收集代码文件
                if file_info.is_code_file:
                    code_files.append(file_info)

            # 计算代码变更
            code_changes = sum(f.changes for f in code_files)

            # 判断是否应该跳过审查
            should_skip, skip_reason = self._should_skip_review(
                len(code_files), code_changes, len(file_list)
            )

            # 确定审查策略
            if should_skip:
                strategy = "skip"
            else:
                strategy = get_strategy_config().determine_strategy(
                    len(code_files), code_changes
                )

            # 提取 diff 安全区白名单和 hunk 边界
            changed_lines_map, hunk_boundaries = self._extract_changed_lines(code_files)

            analysis = PRAnalysis(
                pr_id=pr_info["pr_id"],
                pr_number=pr_info["pr_number"],
                repo_full_name=pr_info["repo_full_name"],
                total_files=len(file_list),
                total_additions=total_additions,
                total_deletions=total_deletions,
                total_changes=total_changes,
                code_files=code_files,
                code_file_count=len(code_files),
                code_changes=code_changes,
                strategy=strategy,
                should_skip=should_skip,
                skip_reason=skip_reason,
                changed_lines_map=changed_lines_map,
                hunk_boundaries=hunk_boundaries,
                is_incremental=is_incremental,
                new_commits=new_commits,
            )

            logger.info(
                f"PR分析完成: {pr_info['repo_full_name']}#{pr_info['pr_number']}, "
                f"文件数: {len(code_files)}, 变更行数: {code_changes}, "
                f"策略: {strategy}"
            )

            return analysis

        except Exception as e:
            logger.error(f"分析PR时出错: {e}", exc_info=True)
            raise

    def _extract_changed_lines(
        self, code_files: List[PRFileInfo]
    ) -> Tuple[Dict[str, set], Dict[str, List[Tuple[int, int]]]]:
        """从文件 patch 中提取变更的行号（Diff 安全区）和 hunk 边界

        解析 unified diff 格式，提取所有变更行的行号和每个 hunk 的行范围。
        安全区白名单用于验证 AI 给出的行号是否在 diff 范围内，
        hunk 边界用于检测跨 hunk 的多行评论（GitHub API 不允许）。

        Args:
            code_files: 代码文件列表

        Returns:
            元组：(changed_lines, hunk_boundaries)
            - changed_lines: 字典，key 为文件路径，value 为变更行号的集合
            - hunk_boundaries: 字典，key 为文件路径，value 为 hunk (起始行, 结束行) 列表
        """
        import re

        changed_lines = {}
        hunk_boundaries = {}

        for file_info in code_files:
            if not file_info.patch:
                continue

            logger.info(f"🔍 开始解析 {file_info.path} 的 patch")

            # 解析 patch 提取行号
            # unified diff 格式：
            # @@ -old_start,old_count +new_start,new_count @@
            # +added_line
            # -removed_line
            lines = file_info.patch.split("\n")
            file_changed_lines = set()

            i = 0
            hunk_count = 0

            while i < len(lines):
                line = lines[i]

                # 匹配 hunk header
                # 例如：@@ -10,5 +10,7 @@ 或 @@ -1 +1,2 @@
                hunk_match = re.match(
                    r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@", line
                )
                if hunk_match:
                    hunk_count += 1

                    # 提取新旧文件的起始行号和行数
                    old_start = int(hunk_match.group(1))
                    old_count = int(hunk_match.group(2)) if hunk_match.group(2) else 1
                    new_start = int(hunk_match.group(3))
                    new_count = int(hunk_match.group(4)) if hunk_match.group(4) else 1

                    logger.info(
                        f"  📦 Hunk #{hunk_count}: 原文件第{old_start}-{old_start + old_count - 1}行 → PR后第{new_start}-{new_start + new_count - 1}行"
                    )

                    current_line = new_start
                    hunk_start = new_start
                    lines_in_hunk = 0
                    added_lines = 0
                    removed_lines = 0
                    context_lines = 0

                    # 向后读取 hunk 的内容
                    i += 1
                    while i < len(lines):
                        hunk_line = lines[i]

                        # 遇到新的 hunk header，结束当前 hunk
                        if hunk_line.startswith("@@"):
                            break

                        lines_in_hunk += 1

                        # 提取变更的行号（包含上下文行，给 AI 更多评论空间）
                        if hunk_line.startswith("+") and not hunk_line.startswith(
                            "+++"
                        ):
                            # 新增行
                            file_changed_lines.add(current_line)
                            added_lines += 1
                            logger.debug(f"    + 第{current_line}行: {hunk_line[:50]}")
                            current_line += 1
                        elif hunk_line.startswith("-") and not hunk_line.startswith(
                            "---"
                        ):
                            # 删除行，不记录行号（因为这是旧文件的行号）
                            removed_lines += 1
                            logger.debug(f"    - 删除原文件行: {hunk_line[:50]}")
                            current_line += 0  # 删除行不增加 PR 后文件的行号
                        elif not hunk_line.startswith("\\"):
                            # 上下文行（不是 \ No newline at end of file）
                            # 也添加上下文行，给 AI 更多评论空间
                            file_changed_lines.add(current_line)
                            context_lines += 1
                            logger.debug(
                                f"      第{current_line}行 (上下文): {hunk_line[:50]}"
                            )
                            current_line += 1

                        i += 1

                    logger.info(
                        f"  ✓ Hunk #{hunk_count} 解析完成: +{added_lines} -{removed_lines} 行, 包含{context_lines}行上下文, PR后行号范围: {new_start}-{current_line - 1}"
                    )

                    # 记录 hunk 边界
                    hunk_end = current_line - 1
                    if file_info.path not in hunk_boundaries:
                        hunk_boundaries[file_info.path] = []
                    hunk_boundaries[file_info.path].append((hunk_start, hunk_end))

                    continue

                i += 1

            if file_changed_lines:
                changed_lines[file_info.path] = file_changed_lines
                sorted_lines = sorted(file_changed_lines)
                logger.info(
                    f"✅ 文件 {file_info.path} 共 {hunk_count} 个 hunk, 提取行号 {len(sorted_lines)} 个: {sorted_lines[:15]}{'...' if len(sorted_lines) > 15 else ''}"
                )
            else:
                logger.warning(f"⚠️  文件 {file_info.path} 未提取到任何行号")

        logger.info(f"🎯 构建 Diff 安全区完成，覆盖 {len(changed_lines)} 个文件")
        return changed_lines, hunk_boundaries

    def _should_skip_review(
        self, code_file_count: int, code_changes: int, total_files: int
    ) -> Tuple[bool, Optional[str]]:
        """判断是否应该跳过审查"""
        # 检查是否有代码文件
        if code_file_count == 0:
            return True, "没有代码文件变更"

        # 检查变更是否过小
        if code_changes == 0:
            return True, "没有代码变更"

        # 检查是否超过最大限制
        if code_file_count > settings.max_file_count:
            return (
                True,
                f"文件数超过限制 ({code_file_count} > {settings.max_file_count})",
            )

        if code_changes > settings.max_line_count:
            return (
                True,
                f"变更行数超过限制 ({code_changes} > {settings.max_line_count})",
            )

        return False, None

    async def get_project_structure(self, repo: any, max_files: int = 500) -> List[str]:
        """获取项目的目录结构（将 PyGithub 同步调用移到线程池）"""
        return await asyncio.to_thread(
            self._get_project_structure_sync, repo, max_files
        )

    def _get_project_structure_sync(self, repo: any, max_files: int = 500) -> List[str]:
        """获取项目的目录结构

        Args:
            repo: GitHub仓库对象
            max_files: 最大文件数限制

        Returns:
            目录结构列表
        """
        try:
            # 获取仓库的Git树
            tree = repo.get_git_tree(repo.default_branch, recursive=True)

            # 获取跳过路径配置
            skip_paths = get_strategy_config().get_file_filters().get("skip_paths", [])

            structure = []
            file_count = 0

            # 按路径排序并格式化
            for item in sorted(tree.tree, key=lambda x: x.path):
                if file_count >= max_files:
                    structure.append(
                        f"... (还有 {len(tree.tree) - max_files} 个文件未显示)"
                    )
                    break

                # 检查是否应该跳过该路径
                should_skip = False
                for skip_path in skip_paths:
                    if item.path.startswith(skip_path.rstrip("/")):
                        should_skip = True
                        break

                if should_skip:
                    continue

                if item.type == "tree":
                    structure.append(f"📁 {item.path}/")
                else:
                    structure.append(f"📄 {item.path}")
                    file_count += 1

            logger.info(
                f"获取项目结构完成，共 {min(len(tree.tree), max_files)} 个项目（已过滤skip_paths）"
            )
            return structure

        except Exception as e:
            logger.error(f"获取项目结构失败: {e}", exc_info=True)
            return []

    async def prepare_review_context(
        self, analysis: PRAnalysis, pr: any
    ) -> Dict[str, any]:
        """准备审查上下文（将 PyGithub 同步调用移到线程池）"""
        return await asyncio.to_thread(self._prepare_review_context_sync, analysis, pr)

    def _prepare_review_context_sync(
        self, analysis: PRAnalysis, pr: any
    ) -> Dict[str, any]:
        """准备审查上下文

        优化说明：
        - 移除冗余字段（strategy_name, tools_available 可在需要时再获取）
        - 统一 patch 截断逻辑
        - 减少数据重复传递
        """
        try:
            # 根据策略准备不同级别的上下文
            strategy_name = analysis.strategy

            # 获取仓库对象 - 使用 pr.base.repo 而不是 pr.repository
            repo = pr.base.repo

            # 获取项目结构
            project_structure = self._get_project_structure_sync(repo)

            # 构建 context，只包含必要信息
            context = {
                "strategy": strategy_name,
                "files": [],
                "project_structure": project_structure,
                "changed_lines_map": analysis.changed_lines_map or {},
                "analysis": analysis,  # 传递整个 analysis 对象，避免重复提取字段
            }

            # 对于小型PR，包含完整的patch
            if strategy_name in ["quick", "standard"]:
                for file_info in analysis.code_files:
                    file_context = {
                        "path": file_info.path,
                        "status": file_info.status,
                        "changes": file_info.changes,
                        "additions": file_info.additions,
                        "deletions": file_info.deletions,
                    }

                    # 统一的 patch 截断逻辑
                    if file_info.patch:
                        file_context["patch"] = self._truncate_patch(
                            file_info.patch, max_lines=500, max_chars=3000
                        )

                    context["files"].append(file_context)

            # 对于大型PR（deep策略），只包含主要文件
            elif strategy_name == "deep":
                # 分批处理
                batch_config = get_strategy_config().get_batch_config()
                max_files_per_batch = batch_config.get("max_files_per_batch", 10)

                # 对文件按重要性排序（变更量大的优先）
                sorted_files = sorted(
                    analysis.code_files, key=lambda f: f.changes, reverse=True
                )

                for file_info in sorted_files[:max_files_per_batch]:
                    file_context = {
                        "path": file_info.path,
                        "status": file_info.status,
                        "changes": file_info.changes,
                    }

                    # 统一的 patch 截断逻辑（更严格的限制）
                    if file_info.patch:
                        file_context["patch"] = self._truncate_patch(
                            file_info.patch, max_lines=300, max_chars=2000
                        )

                    context["files"].append(file_context)

                if len(analysis.code_files) > max_files_per_batch:
                    context["remaining_files"] = (
                        len(analysis.code_files) - max_files_per_batch
                    )

            # 对于超大PR，只包含概览
            elif strategy_name == "large":
                context["file_summary"] = [
                    {"path": f.path, "changes": f.changes, "status": f.status}
                    for f in analysis.code_files[:20]
                ]
                if len(analysis.code_files) > 20:
                    context["remaining_files"] = len(analysis.code_files) - 20

            return context

        except Exception as e:
            logger.error(f"准备审查上下文时出错: {e}", exc_info=True)
            raise

    def _truncate_patch(
        self, patch: str, max_lines: int = 500, max_chars: int = 3000
    ) -> str:
        """返回完整的 patch（已去除截断限制）

        Args:
            patch: 原始 patch 内容
            max_lines: 参数保留（已废弃），用于兼容性
            max_chars: 参数保留（已废弃），用于兼容性

        Returns:
            完整的 patch
        """
        # 直接返回完整 patch，不做任何截断
        # 分批处理机制会处理超大 PR 的上下文管理
        return patch
