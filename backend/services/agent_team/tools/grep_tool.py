"""Grep 工具 - 通过 workspace-scoped 执行器搜索文件内容。"""

from __future__ import annotations

import asyncio
import errno
from pathlib import Path, PurePosixPath
from threading import Event
from typing import Any

from backend.services.agent_team.execution import (
    ExecutionProfile,
    ExecutionRequest,
    execute_request,
    execution_workspace_key,
    resolve_execution_runner,
)
from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult
from backend.services.agent_team.tools.file_utils import (
    read_workspace_text_with_metadata,
)
from backend.utils.search_excludes import SEARCH_EXCLUDES

MAX_GREP_KEYWORD_LENGTH = 500


class GrepTool(BaseTool):
    """搜索工作区内文件内容。"""

    name = "search_in_files"

    _schema = {
        "type": "function",
        "function": {
            "name": "search_in_files",
            "description": (
                "在工作区内搜索指定文本（固定字符串匹配），返回匹配的文件和行内容。"
                "\n\n使用场景："
                "\n- 搜索函数定义、类定义"
                "\n- 查找某个变量的使用位置"
                "\n- 搜索配置项"
                "\n- 确认某个 API 的调用方式"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词（固定字符串匹配；换行分隔多个可选模式，空模式匹配每行）",
                    },
                    "file_extension": {
                        "type": "string",
                        "description": "可选：限定文件后缀，如 .py、.ts",
                    },
                    "output_mode": {
                        "type": "string",
                        "enum": ["files_with_matches", "content"],
                        "description": (
                            "输出模式：files_with_matches 只返回文件名（默认），"
                            "content 返回匹配行内容"
                        ),
                        "default": "files_with_matches",
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "description": "是否按 Unicode casefold 忽略大小写，默认 false",
                        "default": False,
                    },
                },
                "required": ["keyword"],
            },
        },
    }

    def is_read_only(self) -> bool:
        return True

    def validate_input(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        keyword = str(args.get("keyword") or "")
        if not keyword:
            return "缺少 keyword 参数"
        if len(keyword) > MAX_GREP_KEYWORD_LENGTH:
            return f"keyword 不能超过 {MAX_GREP_KEYWORD_LENGTH} 个字符"
        return None

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        keyword = args["keyword"]
        file_ext = args.get("file_extension", "")
        output_mode = args.get("output_mode", "files_with_matches")
        case_insensitive = args.get("case_insensitive", False)

        # The selected READ_ONLY runner remains mandatory. It only enumerates
        # text candidates with a fixed pattern: rejected links cannot influence
        # keyword-dependent match status, counts or backend truncation. Never
        # publish its raw filenames/diagnostics before descriptor admission.
        cmd_parts = ["grep", "-rl", "-Z", "-I"]
        if file_ext:
            cmd_parts.extend(["--include", f"*{file_ext}"])
        for excl in sorted(SEARCH_EXCLUDES):
            cmd_parts.extend(["--exclude-dir", excl])
        cmd_parts.extend(["--", "^", "."])

        try:
            runner = resolve_execution_runner(
                ctx.execution_runner, ctx.workspace, ctx.workspace_service
            )
            request = ExecutionRequest(
                workspace_key=execution_workspace_key(
                    ctx.workspace, ctx.workspace_service
                ),
                argv=tuple(cmd_parts),
                cwd=PurePosixPath("."),
                profile=ExecutionProfile.READ_ONLY,
                timeout_seconds=30,
                cancel_event=ctx.cancel_event,
            )
            self._check_cancelled(ctx)
            result = await execute_request(runner, request)
            if result.cancelled:
                raise asyncio.CancelledError
            self._check_cancelled(ctx)
            if (
                result.timed_out
                or result.infrastructure_error
                or result.returncode not in {0, 1}
            ):
                # Operational failure stays observable without forwarding raw
                # runner diagnostics, which can contain excluded paths/content.
                return ToolResult(
                    success=False,
                    error=f"grep 候选枚举执行失败 (rc={result.returncode}, "
                    f"timed_out={result.timed_out}, "
                    f"infrastructure_error={bool(result.infrastructure_error)})",
                )
            # Backend truncation can cut a filename. Only NUL-terminated records
            # are candidates; whitespace, colons and newlines belong to names.
            candidates = (
                result.stdout.split("\x00")[:-1] if result.returncode == 0 else []
            )
            projection_cancel = Event()
            projection = asyncio.create_task(
                asyncio.to_thread(
                    self._project_matches,
                    candidates,
                    keyword,
                    case_insensitive,
                    output_mode,
                    ctx,
                    projection_cancel,
                )
            )
            try:
                output = await asyncio.shield(projection)
            except asyncio.CancelledError:
                # Cancelling a to_thread future does not stop its worker. Keep
                # the workspace barrier held until the current descriptor read
                # drains, then let the worker stop before the next candidate.
                projection_cancel.set()
                while not projection.done():
                    try:
                        await asyncio.shield(projection)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                await asyncio.gather(projection, return_exceptions=True)
                raise
            self._check_cancelled(ctx)
            output["truncated"] = output["truncated"] or result.output_truncated
            return ToolResult(success=True, output=output)
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"grep 搜索失败: {type(exc).__name__}",
            )

    @staticmethod
    def _check_cancelled(
        ctx: ToolContext, projection_cancel: Event | None = None
    ) -> None:
        if (projection_cancel is not None and projection_cancel.is_set()) or (
            ctx.cancel_event is not None and ctx.cancel_event.is_set()
        ):
            raise asyncio.CancelledError

    def _project_matches(
        self,
        candidates: list[str],
        keyword: str,
        case_insensitive: bool,
        output_mode: str,
        ctx: ToolContext,
        projection_cancel: Event,
    ) -> dict[str, Any]:
        # As with grep -F, newline-separated patterns are alternatives. Empty
        # patterns (including a trailing newline) match every existing line.
        # casefold applies one Unicode rule to every pattern and admitted line.
        patterns = keyword.split("\n")
        if case_insensitive:
            patterns = [pattern.casefold() for pattern in patterns]
        files: set[str] = set()
        matches: list[str] = []
        total = 0
        seen: set[PurePosixPath] = set()
        for candidate in candidates:
            self._check_cancelled(ctx, projection_cancel)
            relative = PurePosixPath(candidate)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                continue
            if relative in seen:
                continue
            seen.add(relative)
            try:
                # Preserve the original lexical path. Resolving internal links
                # here would bypass the nofollow check on candidate components.
                text, _, _, _ = read_workspace_text_with_metadata(
                    ctx.workspace, Path(ctx.workspace) / relative
                )
            except ValueError, UnicodeError:
                # Nonregular, multiply-linked or newly binary candidates do not
                # participate in matching, including query-dependent errors.
                continue
            except OSError as exc:
                if exc.errno in {
                    errno.ENOENT,
                    errno.ENOTDIR,
                    errno.ELOOP,
                    errno.EISDIR,
                    errno.ESTALE,
                    errno.ENXIO,
                }:
                    continue
                raise
            lines = text.split("\n")
            if lines[-1] == "":
                lines.pop()
            for number, line in enumerate(lines, 1):
                self._check_cancelled(ctx, projection_cancel)
                compared = line.casefold() if case_insensitive else line
                if any(pattern in compared for pattern in patterns):
                    total += 1
                    files.add(candidate)
                    if len(matches) < 100:
                        matches.append(f"{candidate}:{number}:{line}")
        if output_mode == "files_with_matches":
            return {
                "files": sorted(files)[:50],
                "num_files": len(files),
                "keyword": keyword,
                "truncated": total > 100 or len(files) > 50,
            }
        return {
            "matches": matches,
            "total": total,
            "keyword": keyword,
            "truncated": total > 100,
        }
