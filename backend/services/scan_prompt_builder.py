"""仓库扫描 Prompt 构建与结果解析"""

import json
import re
from pathlib import Path
from loguru import logger


# 常见代码文件扩展名（按语言分组）
CODE_EXTENSIONS = (
    # Python
    ".py",
    ".pyi",
    ".pyx",
    ".pyw",
    # JavaScript / TypeScript
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    # Java / Kotlin
    ".java",
    ".kt",
    ".kts",
    # C / C++ / ObjC
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".cxx",
    ".hxx",
    ".m",
    ".mm",
    # Go
    ".go",
    # Rust
    ".rs",
    # Web
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".less",
    ".sass",
    ".vue",
    ".svelte",
    # Shell
    ".sh",
    ".bash",
    ".zsh",
    # Config
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    # Other
    ".lua",
    ".rb",
    ".php",
    ".pl",
    ".r",
    ".swift",
    ".dart",
    ".groovy",
    ".sql",
    ".graphql",
    ".proto",
)


# 跳过的依赖目录（模块级常量，避免循环内重复创建）
_SKIP_DIRS = frozenset(
    {
        "node_modules",
        ".git",
        "__pycache__",
        ".idea",
        ".vscode",
        "venv",
        ".venv",
        "dist",
        "build",
        ".next",
        "target",
        ".gradle",
        "vendor",
        "Pods",
        ".mypy_cache",
        ".pytest_cache",
        "site-packages",
        ".eggs",
        "egg-info",
    }
)


def collect_code_files(repo_path: str, max_files: int = 500) -> list[dict]:
    """从磁盘收集仓库中的代码文件列表

    Args:
        repo_path: 仓库根目录路径
        max_files: 最大文件数

    Returns:
        [{"file_path": "相对路径", "extension": ".py", "size_bytes": 123}, ...]
    """
    repo = Path(repo_path)
    files = []

    for path in repo.rglob("*"):
        if not path.is_file():
            continue

        # 跳过隐藏目录和常见忽略目录
        parts = path.relative_to(repo).parts
        if any(part.startswith(".") for part in parts[:-1]):
            continue

        # 跳过依赖目录
        if any(part in _SKIP_DIRS for part in parts):
            continue

        # 只收集代码文件
        if path.suffix.lower() not in CODE_EXTENSIONS:
            continue

        try:
            stat = path.stat()
            files.append(
                {
                    "file_path": str(path.relative_to(repo)),
                    "extension": path.suffix.lower(),
                    "size_bytes": stat.st_size,
                }
            )
        except OSError:
            continue

        if len(files) >= max_files:
            break

    # 按路径排序
    files.sort(key=lambda f: f["file_path"])
    return files


def build_scan_context(
    repo_name: str,
    repo_path: str,
    file_list: list[dict],
    commit_sha: str | None = None,
) -> dict:
    """构建全仓扫描的 context（模拟 AIReviewer 的审查上下文）

    与 AIReviewer 的 PR context 结构对齐，让它能无缝对接。
    """
    # 拼接项目结构摘要
    structure_lines = []
    extensions = sorted(set(f["extension"] for f in file_list))
    ext_summary = ", ".join(extensions) if extensions else "unknown"

    structure_lines.append("项目根目录: /")
    structure_lines.append(f"编程语言: {ext_summary}")
    structure_lines.append(f"代码文件数: {len(file_list)}")

    # 按目录分组统计
    dir_map: dict[str, int] = {}
    for f in file_list:
        parent = str(Path(f["file_path"]).parent)
        dir_map[parent] = dir_map.get(parent, 0) + 1

    for d in sorted(dir_map.keys())[:50]:
        structure_lines.append(f"  {d}/ ({dir_map[d]} 文件)")
    if len(dir_map) > 50:
        structure_lines.append(f"  ... 共 {len(dir_map)} 个目录")

    # 生成文件树（简化版）
    file_tree_lines = []
    for f in file_list[:200]:
        file_tree_lines.append(f"  {f['file_path']}")

    total_size = sum(f["size_bytes"] for f in file_list)
    size_str = (
        f"{total_size / 1024 / 1024:.1f} MB"
        if total_size > 1024 * 1024
        else f"{total_size / 1024:.1f} KB"
    )

    return {
        "repo_name": repo_name,
        "repo_owner": repo_name.split("/")[0] if "/" in repo_name else "",
        "repo_path": repo_path,
        "commit_sha": commit_sha or "unknown",
        "files": [f["file_path"] for f in file_list],
        "total_files": len(file_list),
        "total_size": size_str,
        "patch": "",  # 扫描无 patch
        "title": f"仓库全量扫描: {repo_name}",
        "body": f"对仓库 {repo_name} 进行全面安全与质量扫描",
        "author": "scan",
        "project_structure": "\n".join(structure_lines),
        "file_tree": "\n".join(file_tree_lines),
        "languages": ext_summary,
        # 额外信息：告诉 AI 这是全仓扫描模式
        "is_full_scan": True,
    }


SCAN_SYSTEM_PROMPT = """你是一个专业的代码安全和质量审计专家。你的任务是**对一个仓库的全部代码进行全面扫描**，而不是审查单个 PR。

## 你的能力

你可以使用以下工具来浏览代码（按需使用，部分工具可能不可用）：

### 始终可用的工具
- `read_file` - 读取任意文件的内容（支持完整读取、行范围读取、内容搜索三种模式）
- `list_directory` - 列出目录内容，了解项目结构
- `search_in_files` - 在仓库中跨文件搜索指定关键词（类似 grep），返回匹配行及上下文
- `get_git_info` - 获取仓库基本信息，包括描述、默认分支、语言统计、分支列表
- `list_commits` - 查看指定分支的提交历史记录

### 按条件可用的工具（取决于仓库配置）
- `search_code_context` - 基于语义搜索相关代码片段（需要代码索引）
- `search_project_docs` - 检索项目指导文档，如编码规范、架构准则（需要知识库索引）
- `search_web` - 搜索互联网获取最新文档和最佳实践（需要启用网络搜索）
- `read_sakura_docs` - 读取项目 .sakura/ 目录中的指导文档（需要启用 sakura 记忆系统）
- `list_sakura_directory` - 列出 .sakura/ 目录的结构（需要启用 sakura 记忆系统）

**你应该主动使用工具浏览仓库代码**，不要仅凭文件列表猜测问题。重点关注以下区域：
1. 配置文件（.env, config.*, settings.*）
2. 认证和授权相关代码
3. API 路由和处理函数
4. 数据库操作
5. 文件操作
6. 密码学相关调用
7. 外部依赖引入

## 扫描维度（必须全部覆盖）

### 1. 安全性 (Security)
- SQL 注入、XSS、CSRF、命令注入、路径遍历
- 硬编码的密钥、密码、Token、API Key
- 不安全的加密算法或随机数生成
- 权限绕过、未授权访问
- 敏感数据泄露（PII、日志中打印敏感信息）
- 不安全的 HTTP 配置（HTTP-only cookie, CORS 过宽）
- 依赖项中的已知漏洞

### 2. 性能 (Performance)
- N+1 查询、缺少分页
- 内存泄漏风险（未关闭的资源）
- 同步阻塞操作
- 低效算法或数据结构
- 缺少缓存的热点代码

### 3. 可靠性 (Reliability)
- 错误处理缺失或过于宽泛的 except
- 未捕获的异常、资源泄漏
- 竞态条件
- 空指针/NoneType 风险
- 边界条件未处理
- 超时设置缺失

### 4. 可维护性 (Maintainability)
- 代码重复
- 过长函数/类（超过 100 行）
- 圈复杂度过高
- 魔法数字/字符串
- 命名不规范
- 过度嵌套

### 5. 架构 (Architecture)
- 循环依赖
- 违反 SOLID 原则
- 抽象层次不当
- 耦合过紧的模块
- 全局状态滥用

## 输出格式

最终输出必须是以下 JSON 格式（在所有工具调用完成后）：
```json
{
  "overall_score": 85,
  "summary": "一句话总结扫描结果",
  "findings": [
    {
      "severity": "critical|major|minor|suggestion",
      "category": "security|performance|reliability|maintainability|architecture",
      "title": "简短问题标题",
      "description": "问题描述",
      "suggestion": "修复建议",
      "file_path": "相关文件路径",
      "line_start": 起始行号,
      "line_end": 结束行号,
      "confidence": 0-100
    }
  ]
}
```

## 重要规则
1. **必须使用工具查看代码**，不要凭文件名猜测
2. 只报告确实存在的问题，不要过度解读
3. severity 应反映实际影响
4. 同一文件同一问题不要重复报告
5. 如果代码质量很高，也要明确说明"""


def parse_scan_result(response_text: str) -> dict:
    """解析 AI 扫描结果"""
    if not response_text or not response_text.strip():
        return {"findings": [], "overall_score": None, "summary": ""}

    # 提取 JSON
    json_match = re.search(
        r"```(?:json)?\s*\n?(.*?)\n?\s*```", response_text, re.DOTALL
    )
    if json_match:
        json_text = json_match.group(1).strip()
    else:
        # 尝试找到 JSON 对象
        obj_match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if obj_match:
            json_text = obj_match.group(0)
        else:
            logger.warning("无法从扫描结果中提取 JSON")
            return {
                "findings": [],
                "overall_score": None,
                "summary": response_text[:200],
            }

    try:
        result = json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.warning(f"解析扫描结果 JSON 失败: {e}")
        return {"findings": [], "overall_score": None, "summary": ""}

    if not isinstance(result, dict):
        return {"findings": [], "overall_score": None, "summary": ""}

    findings = result.get("findings", [])
    if not isinstance(findings, list):
        findings = []

    # 校验规范化
    valid_severities = {"critical", "major", "minor", "suggestion"}
    valid_categories = {
        "security",
        "performance",
        "reliability",
        "maintainability",
        "architecture",
    }

    valid_findings = []
    for f in findings:
        if not isinstance(f, dict) or not f.get("title"):
            continue

        severity = str(f.get("severity", "minor")).lower()
        if severity not in valid_severities:
            severity = "minor"

        category = str(f.get("category", "maintainability")).lower()
        if category not in valid_categories:
            category = "maintainability"

        try:
            confidence = int(f.get("confidence", 50))
            confidence = max(0, min(100, confidence))
        except (ValueError, TypeError):
            confidence = 50

        valid_findings.append(
            {
                "severity": severity,
                "category": category,
                "title": str(f.get("title", ""))[:500],
                "description": str(f.get("description", "")),
                "suggestion": str(f.get("suggestion", "")) or None,
                "file_path": str(f.get("file_path", "")) or None,
                "line_start": _safe_int(f.get("line_start")),
                "line_end": _safe_int(f.get("line_end")),
                "confidence": confidence,
            }
        )

    return {
        "findings": valid_findings,
        "overall_score": _safe_int(result.get("overall_score")),
        "summary": str(result.get("summary", "")),
    }


def _safe_int(value) -> int | None:
    """安全转换为整数"""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None
