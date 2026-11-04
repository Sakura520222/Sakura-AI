"""Project Detect 工具 - 自动检测项目类型

分析工作区目录结构，检测编程语言、框架、构建工具和测试框架。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Any

from backend.services.agent_team.repository_context import RepositoryContext
from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult

# 项目标记文件 → 语言/工具链映射
_PROJECT_MARKERS: list[tuple[str, str, str]] = [
    # (marker_file, language, package_manager)
    ("pyproject.toml", "python", "pip"),
    ("setup.py", "python", "pip"),
    ("requirements.txt", "python", "pip"),
    ("package.json", "javascript", "npm"),
    ("Cargo.toml", "rust", "cargo"),
    ("go.mod", "go", "go"),
    ("pom.xml", "java", "maven"),
    ("build.gradle", "java", "gradle"),
    ("Gemfile", "ruby", "bundler"),
    ("composer.json", "php", "composer"),
]

# Node.js 包管理器检测
_NODE_PM_FILES: list[tuple[str, str]] = [
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lockb", "bun"),
    ("package-lock.json", "npm"),
]


def _detect_framework(
    workspace: Path,
    language: str,
    package_json: dict | None,
    cancel_check: Callable[[], None] | None = None,
) -> list[str]:
    """从依赖文件推断框架。"""
    frameworks: list[str] = []
    if language == "python":
        if (workspace / "manage.py").exists():
            frameworks.append("django")
        for name in ("fastapi", "flask", "starlette", "sanic"):
            if _dep_in_python_workspace(workspace, name, cancel_check):
                frameworks.append(name)
    elif language == "javascript" and package_json:
        deps = {
            **package_json.get("dependencies", {}),
            **package_json.get("devDependencies", {}),
        }
        for fw in ("next", "react", "vue", "express", "nest", "nuxt", "svelte"):
            if fw in deps or f"@{fw}/core" in deps:
                frameworks.append(fw)
    return frameworks


def _dep_in_python_workspace(
    workspace: Path,
    dep_name: str,
    cancel_check: Callable[[], None] | None = None,
) -> bool:
    """检查 Python 依赖是否在工作区中声明。"""
    for req_file in (
        "requirements.txt",
        "requirements/base.txt",
        "requirements/production.txt",
    ):
        if cancel_check:
            cancel_check()
        req_path = workspace / req_file
        if req_path.exists():
            if cancel_check:
                cancel_check()
            content = RepositoryContext(workspace).read_text(req_file).lower()
            if dep_name in content:
                return True
    pyproject = workspace / "pyproject.toml"
    if cancel_check:
        cancel_check()
    if pyproject.exists():
        if cancel_check:
            cancel_check()
        content = RepositoryContext(workspace).read_text("pyproject.toml").lower()
        if dep_name in content:
            return True
    return False


def _detect_test_command(
    workspace: Path, language: str, package_json: dict | None
) -> str:
    """推断测试命令。"""
    if language == "python":
        if (workspace / "pytest.ini").exists() or (
            workspace / "pyproject.toml"
        ).exists():
            return "pytest -q"
        return "python -m unittest"
    if language == "javascript" and package_json:
        scripts = package_json.get("scripts", {})
        if "test" in scripts:
            return "npm test"
        deps = {
            **package_json.get("dependencies", {}),
            **package_json.get("devDependencies", {}),
        }
        if "jest" in deps:
            return "npx jest"
        if "vitest" in deps:
            return "npx vitest run"
        return "npm test"
    if language == "go":
        return "go test ./..."
    if language == "rust":
        return "cargo test"
    return ""


# 依赖 venv 中 ruff 启动器的固定相对路径 / fixed launcher paths in dependency venvs
_PYTHON_LINT_LAUNCHERS = (
    ".venv/sandbox/bin/ruff",
    ".venv/local/bin/ruff",
    ".venv/local/Scripts/ruff.exe",
)


def _workspace_tool_exists(workspace: Path, launchers: tuple[str, ...]) -> bool:
    """探测依赖 venv 中是否真实存在工具 / Probe dependency venvs for a launcher."""
    return any((workspace / rel).is_file() for rel in launchers)


def _detect_lint_command(
    workspace: Path, language: str, package_json: dict | None
) -> str:
    """推断代码检查命令（只声明真实存在的能力 / advertise only existing tools)."""
    if language == "python":
        if _workspace_tool_exists(workspace, _PYTHON_LINT_LAUNCHERS):
            return "ruff check"
        return ""
    if language == "javascript" and package_json:
        deps = {
            **package_json.get("dependencies", {}),
            **package_json.get("devDependencies", {}),
        }
        if "eslint" in deps:
            return "npx eslint ."
        return "npm run lint"
    if language == "go":
        return "go vet ./..."
    if language == "rust":
        return "cargo clippy"
    return ""


class DetectProjectTool(BaseTool):
    """检测工作区项目类型和工具链。"""

    name = "detect_project"

    _schema = {
        "type": "function",
        "function": {
            "name": "detect_project",
            "description": (
                "自动检测项目类型（编程语言、框架、测试工具、代码检查工具）。"
                "\n\n使用场景："
                "\n- 开始工作前了解项目结构和技术栈"
                "\n- 确定应使用哪些测试和代码检查命令"
                "\n\n建议在阶段 1（探索阶段）首先调用此工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    }

    def is_read_only(self) -> bool:
        return True

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        detection_cancel = Event()
        operation = asyncio.create_task(
            asyncio.to_thread(self._detect, ctx, detection_cancel)
        )
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # The current filesystem read cannot be interrupted by cancelling
            # its future. Retain the shared workspace barrier until it drains,
            # then stop before admitting another read or returning a result.
            detection_cancel.set()
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            await asyncio.gather(operation, return_exceptions=True)
            raise

    @staticmethod
    def _detect(ctx: ToolContext, detection_cancel: Event | None = None) -> ToolResult:
        def check_cancelled():
            if (detection_cancel is not None and detection_cancel.is_set()) or (
                ctx.cancel_event is not None and ctx.cancel_event.is_set()
            ):
                raise asyncio.CancelledError

        check_cancelled()
        workspace = Path(ctx.workspace)
        detected_languages: list[str] = []
        detected_pm: list[str] = []
        package_json: dict | None = None
        primary_language = ""
        primary_pm = ""

        # 检测标记文件
        for marker, language, pm in _PROJECT_MARKERS:
            check_cancelled()
            if (workspace / marker).exists():
                if language not in detected_languages:
                    detected_languages.append(language)
                if pm not in detected_pm:
                    detected_pm.append(pm)
                if not primary_language:
                    primary_language = language
                    primary_pm = pm

        # 读取 package.json
        check_cancelled()
        if (workspace / "package.json").exists():
            try:
                check_cancelled()
                package_json = _safe_read_json(workspace / "package.json")
            except json.JSONDecodeError:
                pass
        check_cancelled()

        # Node.js 包管理器细化
        if "javascript" in detected_languages:
            for lock_file, pm in _NODE_PM_FILES:
                check_cancelled()
                if (workspace / lock_file).exists():
                    primary_pm = pm
                    break

        check_cancelled()
        if not detected_languages:
            return ToolResult(
                success=True,
                output={"detected": False, "message": "未检测到已知项目类型"},
            )

        # 推断框架和工具
        frameworks = _detect_framework(
            workspace, primary_language, package_json, check_cancelled
        )
        check_cancelled()
        test_command = _detect_test_command(workspace, primary_language, package_json)
        check_cancelled()
        lint_command = _detect_lint_command(workspace, primary_language, package_json)
        check_cancelled()

        result: dict[str, Any] = {
            "detected": True,
            "languages": detected_languages,
            "primary_language": primary_language,
            "package_manager": primary_pm,
        }
        if frameworks:
            result["frameworks"] = frameworks
        if test_command:
            result["test_command"] = test_command
        if lint_command:
            result["lint_command"] = lint_command

        return ToolResult(success=True, output=result)


def _safe_read_json(path: Path) -> dict:
    return json.loads(RepositoryContext(path.parent).read_text(path.name))
