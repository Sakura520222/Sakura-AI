# Agent 文件编辑与操作工具实现提取（Python 版参考）

本文档提取自当前仓库中 Claude Code Agent 的工具系统，重点覆盖文件读取、文件写入、精确编辑、搜索、命令执行等能力，并整理为可迁移到 Python 项目的完整实现思路与参考代码。

> Sakura 当前实现补充：本项目的 Agent Team 已在 `backend/services/agent_team/tools/` 下实现一组工作区受控工具，供全栈修复 Agent 与专业审查 Agent 使用。所有路径必须限制在隔离工作区内，shell 命令采用黑名单安全策略，Skills 只提供说明，不会扩大工具权限。

> 主要参考源码：
>
> - `src/Tool.ts`
> - `src/services/tools/toolExecution.ts`
> - `src/tools.ts`
> - `packages/builtin-tools/src/tools/FileReadTool/FileReadTool.ts`
> - `packages/builtin-tools/src/tools/FileEditTool/FileEditTool.ts`
> - `packages/builtin-tools/src/tools/FileEditTool/utils.ts`
> - `packages/builtin-tools/src/tools/FileWriteTool/FileWriteTool.ts`
> - `packages/builtin-tools/src/tools/GlobTool/GlobTool.ts`
> - `packages/builtin-tools/src/tools/GrepTool/GrepTool.ts`
> - `packages/builtin-tools/src/tools/BashTool/BashTool.tsx`
> - `src/utils/fsOperations.ts`
> - `src/utils/fileRead.ts`
> - `src/utils/permissions/filesystem.ts`

---

## 1. 总体设计

Agent 的“工具”不是直接暴露函数给模型，而是一个带有强约束生命周期的对象：

1. 定义工具元数据：名称、描述、参数 schema、结果 schema、是否只读、是否并发安全。
2. 接收模型发起的工具调用。
3. 解析并校验输入 schema。
4. 执行工具自己的业务校验。
5. 执行权限判断，包括 deny/allow/ask。
6. 执行工具主体逻辑。
7. 将内部结果映射为模型可读的 `tool_result`。
8. 更新运行时状态，例如 `readFileState`，用于防止覆盖用户刚修改的文件。

核心思想可以概括为：

```text
LLM tool_use
  -> schema parse
  -> validateInput
  -> pre hooks / permission
  -> tool.call
  -> mapToolResultToToolResultBlockParam
  -> append tool_result to messages
```

迁移到 Python 项目时，建议保留这个生命周期，而不是把所有逻辑写成简单函数。

### 1.1 Sakura Agent Team 当前工具集

Agent Team 当前内置工具覆盖代码阅读、编辑、验证、审查和项目知识读取：

| 工具 | 作用 |
| --- | --- |
| `read_file` | 读取工作区文件，并记录读取状态用于写前新鲜度校验 |
| `write_file` | 写入或创建工作区文件 |
| `edit_file` | 精确字符串替换，适合小范围修改 |
| `replace_lines` | 按行号替换文件片段 |
| `insert_lines` | 在指定行后插入文本 |
| `list_directory` | 列出工作区目录 |
| `glob` | 按 glob 模式查找文件 |
| `search_in_files` | 在工作区内搜索文件内容 |
| `run_command` | 在工作区内运行 shell 命令，并拦截黑名单高危命令 |
| `check_changes` | 查看当前工作区 Git diff 和状态摘要 |
| `detect_project` | 识别项目类型、依赖文件和候选验证命令 |
| `revert_file` | 将指定文件回退到基线版本 |
| `read_sakura_docs` | 读取 `.sakura/` 下的项目知识文档 |
| `list_sakura_directory` | 浏览 `.sakura/` 目录结构 |
| `read_sakura_memory` | 读取 `.sakura/memory/` 下的历史反思 |
| `use_skill` | 加载已启用 Agent Skill 的完整说明 |
| `finish_task` | 结束实现任务并提交总结 |
| `submit_review` | 专业审查 Agent 提交审查结论 |

这些工具由统一 registry 暴露给模型，执行结果会写入任务消息流。写入类工具在成功后会更新文件状态，避免后续编辑基于过期内容继续覆盖。

---

## 2. 工具接口抽象

### 2.1 TypeScript 中的工具接口要点

`src/Tool.ts` 中的 `Tool` 类型包含以下关键字段：

- `name`: 工具名。
- `description()`: 给模型看的简短描述。
- `prompt()`: 更完整的工具使用说明。
- `inputSchema`: 输入参数 schema。
- `outputSchema`: 输出 schema。
- `validateInput()`: 工具级输入校验。
- `checkPermissions()`: 权限判断。
- `call()`: 真正执行工具。
- `mapToolResultToToolResultBlockParam()`: 将内部结果转为模型消息。
- `isReadOnly()`: 是否只读。
- `isConcurrencySafe()`: 是否可并发执行。
- `getPath()`: 工具操作的路径，用于权限和 UI。
- `backfillObservableInput()`: 给 hook / 权限系统补齐规范化输入，但不污染模型原始输入。

`buildTool()` 会给常用字段提供默认值：

- `isEnabled -> true`
- `isConcurrencySafe -> false`
- `isReadOnly -> false`
- `isDestructive -> false`
- `checkPermissions -> allow`
- `toAutoClassifierInput -> ''`
- `userFacingName -> name`

### 2.2 Python 版接口建议

Python 可以用 `dataclass` + `pydantic` 实现类似结构。

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Optional, Protocol, TypeVar
from pydantic import BaseModel

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT")


class PermissionDecision(BaseModel):
    behavior: str  # "allow" | "deny" | "ask"
    message: str | None = None
    updated_input: Any | None = None


class ValidationResult(BaseModel):
    result: bool
    message: str | None = None
    error_code: int = 0


@dataclass
class ToolContext:
    cwd: str
    abort: Any | None = None
    permission_context: "ToolPermissionContext" | None = None
    read_file_state: "ReadFileState" | None = None
    file_history: Any | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Tool(Protocol[InputT, OutputT]):
    name: str
    input_model: type[InputT]
    max_result_size_chars: int

    def description(self, input: InputT | None = None) -> str: ...
    def prompt(self) -> str: ...
    def is_enabled(self) -> bool: ...
    def is_read_only(self, input: InputT) -> bool: ...
    def is_concurrency_safe(self, input: InputT) -> bool: ...
    def get_path(self, input: InputT) -> str | None: ...
    def validate_input(self, input: InputT, context: ToolContext) -> ValidationResult: ...
    def check_permissions(self, input: InputT, context: ToolContext) -> PermissionDecision: ...
    def call(self, input: InputT, context: ToolContext) -> OutputT: ...
    def map_result(self, output: OutputT, tool_use_id: str) -> dict[str, Any]: ...
```

基础类提供默认实现：

```python
class BaseTool(Generic[InputT, OutputT]):
    name: str = ""
    input_model: type[InputT]
    max_result_size_chars: int = 100_000

    def description(self, input: InputT | None = None) -> str:
        return self.name

    def prompt(self) -> str:
        return self.description(None)

    def is_enabled(self) -> bool:
        return True

    def is_read_only(self, input: InputT) -> bool:
        return False

    def is_concurrency_safe(self, input: InputT) -> bool:
        return False

    def get_path(self, input: InputT) -> str | None:
        return None

    def validate_input(self, input: InputT, context: ToolContext) -> ValidationResult:
        return ValidationResult(result=True)

    def check_permissions(self, input: InputT, context: ToolContext) -> PermissionDecision:
        return PermissionDecision(behavior="allow", updated_input=input)
```

---

## 3. 工具执行管线

`src/services/tools/toolExecution.ts` 中的执行管线可提取为 Python 的统一 executor。

### 3.1 关键流程

1. 根据工具名找到工具。
2. 使用工具的 schema 解析输入。
3. 调用 `validateInput`。
4. 执行 `checkPermissions`。
5. 如果权限不是 allow，返回错误 `tool_result`。
6. 调用 `tool.call()`。
7. 使用 `tool.map_result()` 映射成模型消息。
8. 记录日志、耗时、审计信息。

### 3.2 Python 参考实现

```python
import time
from typing import Any
from pydantic import ValidationError


class ToolExecutor:
    def __init__(self, tools: list[BaseTool[Any, Any]]):
        self.tools = {tool.name: tool for tool in tools if tool.is_enabled()}

    def execute(
        self,
        tool_name: str,
        raw_input: dict[str, Any],
        tool_use_id: str,
        context: ToolContext,
    ) -> dict[str, Any]:
        tool = self.tools.get(tool_name)
        if not tool:
            return self._error(tool_use_id, f"Unknown tool: {tool_name}")

        try:
            parsed = tool.input_model.model_validate(raw_input)
        except ValidationError as exc:
            return self._error(tool_use_id, f"InputValidationError: {exc}")

        validation = tool.validate_input(parsed, context)
        if not validation.result:
            return self._error(tool_use_id, validation.message or "Invalid input")

        decision = tool.check_permissions(parsed, context)
        if decision.behavior != "allow":
            return self._error(tool_use_id, decision.message or "Permission denied")

        final_input = decision.updated_input or parsed
        start = time.time()
        try:
            output = tool.call(final_input, context)
        except Exception as exc:
            return self._error(tool_use_id, f"Tool execution failed: {exc}")
        finally:
            duration_ms = int((time.time() - start) * 1000)
            # 可接入日志、trace、metrics
            _ = duration_ms

        return tool.map_result(output, tool_use_id)

    @staticmethod
    def _error(tool_use_id: str, message: str) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "is_error": True,
            "content": f"<tool_use_error>{message}</tool_use_error>",
        }
```

---

## 4. 文件状态缓存：防止覆盖用户修改

Claude Code 的文件编辑工具有一个非常重要的保护机制：

- `Read` 工具读取文件后，将文件内容、mtime、offset、limit 写入 `readFileState`。
- `Edit` / `Write` 工具写入前检查当前文件 mtime。
- 如果文件在上次读取后被用户、格式化器或其他进程改过，则拒绝写入，要求重新读取。
- Windows 上 mtime 可能变化但内容不变，因此如果是完整读取，还会比较内容作为 fallback。

### 4.1 Python 实现

```python
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ReadFileEntry:
    content: str
    timestamp: float
    offset: int | None = None
    limit: int | None = None
    is_partial_view: bool = False


class ReadFileState:
    def __init__(self) -> None:
        self._items: dict[str, ReadFileEntry] = {}

    def get(self, path: str) -> ReadFileEntry | None:
        return self._items.get(str(Path(path).resolve()))

    def set(
        self,
        path: str,
        content: str,
        offset: int | None = None,
        limit: int | None = None,
        is_partial_view: bool = False,
    ) -> None:
        p = Path(path).resolve()
        self._items[str(p)] = ReadFileEntry(
            content=content,
            timestamp=p.stat().st_mtime,
            offset=offset,
            limit=limit,
            is_partial_view=is_partial_view,
        )
```

### 4.2 写前新鲜度检查

```python
def assert_file_not_modified_since_read(path: str, content_now: str, state: ReadFileState) -> None:
    p = Path(path).resolve()
    last = state.get(str(p))
    if last is None:
        raise RuntimeError("File must be read before editing or writing.")

    current_mtime = p.stat().st_mtime
    if current_mtime > last.timestamp:
        is_full_read = last.offset is None and last.limit is None
        if not is_full_read or content_now != last.content:
            raise RuntimeError(
                "File has been modified since read. Read it again before editing."
            )
```

---

## 5. 路径、安全与权限

### 5.1 关键安全点

从源码中可以提取以下安全规则：

1. 路径统一 expand / normalize。
2. Windows UNC 路径（`\\server\share` 或 `//server/share`）在校验阶段不做 `stat` / `exists`，避免触发 SMB/NTLM 凭据泄露。
3. 权限检查要考虑原始路径、符号链接目标、最终 realpath。
4. 对读、写分别维护 allow / deny / ask 规则。
5. 危险目录或文件需要显式权限，例如 `.git`、`.vscode`、`.idea`、`.claude`、`.gitconfig`、shell rc 文件等。
6. 读取设备文件如 `/dev/zero`、`/dev/random`、`/dev/stdin` 需禁止，避免阻塞或无限输出。

Sakura Agent Team 在此基础上增加了面向自动修复任务的边界：

- 工具上下文绑定到 `agent_team_workspace_root` 下的隔离工作区。
- 文件工具只能访问当前任务工作区内路径。
- 搜索与 glob 会排除依赖目录、构建产物和常见缓存目录，减少噪声与大输出。
- Shell 命令必须在工作区内执行，并受默认黑名单与 `agent_team_test_command_blocklist` 控制。
- `detect_project` 可给出候选依赖安装和验证命令，但不能绕过命令安全策略。
- `check_changes` 使用 Git diff/status 帮助 Agent 和审查 Agent 理解累计变更。
- `revert_file` 用于撤销单文件错误修改，降低自动编辑风险。
- Sakura docs/memory 工具只读访问仓库 `.sakura/` 知识，不提供写入能力。

### 5.2 Python 权限模型

```python
from fnmatch import fnmatch
from pathlib import Path
from pydantic import BaseModel, Field


class ToolPermissionContext(BaseModel):
    read_allow: list[str] = Field(default_factory=list)
    read_deny: list[str] = Field(default_factory=list)
    write_allow: list[str] = Field(default_factory=list)
    write_deny: list[str] = Field(default_factory=list)
    mode: str = "default"  # default | acceptEdits | bypassPermissions | plan


def is_unc_path(path: str) -> bool:
    return path.startswith("\\\\") or path.startswith("//")


def expand_path(path: str, cwd: str | None = None) -> str:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd or Path.cwd()) / p
    return str(p.resolve(strict=False))


def path_matches(patterns: list[str], path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatch(normalized, pat.replace("\\", "/")) for pat in patterns)


def check_read_permission(path: str, ctx: ToolPermissionContext) -> PermissionDecision:
    if path_matches(ctx.read_deny, path):
        return PermissionDecision(behavior="deny", message="Read denied by permission rules")
    if ctx.mode == "bypassPermissions" or path_matches(ctx.read_allow, path):
        return PermissionDecision(behavior="allow")
    return PermissionDecision(behavior="ask", message=f"Allow reading {path}?")


def check_write_permission(path: str, ctx: ToolPermissionContext) -> PermissionDecision:
    if path_matches(ctx.write_deny, path):
        return PermissionDecision(behavior="deny", message="Write denied by permission rules")
    if ctx.mode == "bypassPermissions" or path_matches(ctx.write_allow, path):
        return PermissionDecision(behavior="allow")
    return PermissionDecision(behavior="ask", message=f"Allow editing {path}?")
```

在非交互式 Agent 中，`ask` 通常应当降级为 `deny`，或由上层注入自动审批策略。

---

## 6. Read 工具实现

### 6.1 行为提取

`FileReadTool` 的核心能力：

- 参数：`file_path`、`offset`、`limit`、`pages`。
- 支持文本、图片、PDF、Notebook。
- 对文本读取加行号。
- 对大文件限制 token / size。
- 对重复读取同一文件同一范围且 mtime 未变化时返回 `file_unchanged`，避免重复占用上下文。
- 读取后更新 `readFileState`，供后续 edit/write 防 stale。
- 校验二进制扩展名和阻塞设备文件。

Python 项目如果只需要代码编辑，建议先实现文本读取即可。

### 6.2 Python 参考实现

```python
from pydantic import BaseModel, Field


class ReadInput(BaseModel):
    file_path: str
    offset: int = Field(default=1, ge=1)
    limit: int | None = Field(default=None, ge=1)


class ReadOutput(BaseModel):
    type: str
    file_path: str
    content: str | None = None
    num_lines: int | None = None
    start_line: int | None = None
    total_lines: int | None = None


BLOCKED_DEVICE_PATHS = {
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
    "/dev/stdin", "/dev/tty", "/dev/console",
    "/dev/stdout", "/dev/stderr", "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
}


class ReadTool(BaseTool[ReadInput, ReadOutput]):
    name = "Read"
    input_model = ReadInput
    max_result_size_chars = 100_000

    def description(self, input: ReadInput | None = None) -> str:
        return "Read a file from the local filesystem."

    def is_read_only(self, input: ReadInput) -> bool:
        return True

    def is_concurrency_safe(self, input: ReadInput) -> bool:
        return True

    def get_path(self, input: ReadInput) -> str:
        return expand_path(input.file_path)

    def validate_input(self, input: ReadInput, context: ToolContext) -> ValidationResult:
        full_path = expand_path(input.file_path, context.cwd)
        if full_path in BLOCKED_DEVICE_PATHS:
            return ValidationResult(
                result=False,
                message=f"Cannot read {input.file_path}: device file would block or produce infinite output.",
                error_code=9,
            )
        if is_unc_path(full_path):
            return ValidationResult(result=True)
        suffix = Path(full_path).suffix.lower()
        binary_exts = {".exe", ".dll", ".so", ".dylib", ".zip", ".tar", ".gz"}
        if suffix in binary_exts:
            return ValidationResult(
                result=False,
                message=f"This tool cannot read binary files: {suffix}",
                error_code=4,
            )
        return ValidationResult(result=True)

    def check_permissions(self, input: ReadInput, context: ToolContext) -> PermissionDecision:
        ctx = context.permission_context or ToolPermissionContext(mode="bypassPermissions")
        return check_read_permission(expand_path(input.file_path, context.cwd), ctx)

    def call(self, input: ReadInput, context: ToolContext) -> ReadOutput:
        full_path = expand_path(input.file_path, context.cwd)
        p = Path(full_path)
        if not p.exists():
            raise FileNotFoundError(f"File does not exist: {input.file_path}")

        raw = p.read_text(encoding="utf-8")
        raw = raw.replace("\r\n", "\n")
        lines = raw.splitlines()
        total = len(lines)
        start = input.offset
        end = total if input.limit is None else min(total, start + input.limit - 1)
        selected = lines[start - 1:end]
        content = "\n".join(f"{i}: {line}" for i, line in enumerate(selected, start=start))

        if context.read_file_state:
            context.read_file_state.set(
                full_path,
                raw,
                offset=input.offset,
                limit=input.limit,
                is_partial_view=input.limit is not None,
            )

        return ReadOutput(
            type="text",
            file_path=input.file_path,
            content=content,
            num_lines=len(selected),
            start_line=start,
            total_lines=total,
        )

    def map_result(self, output: ReadOutput, tool_use_id: str) -> dict[str, Any]:
        if output.type == "file_unchanged":
            content = "File unchanged since last read."
        else:
            content = output.content or ""
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
```

---

## 7. Edit 工具实现

### 7.1 行为提取

`FileEditTool` 是最适合 Agent 代码编辑的核心工具。它不是让模型输出 patch，而是让模型提供：

- `file_path`
- `old_string`
- `new_string`
- `replace_all`

然后工具负责：

1. 确保 `old_string != new_string`。
2. 检查文件是否存在。
3. 如果文件不存在，只有 `old_string == ""` 才允许创建。
4. 禁止用该工具直接编辑 `.ipynb`，要求专用 Notebook 工具。
5. 检查 stale：文件是否在 Read 之后被改过。
6. 使用 `findActualString()` 容错匹配：
   - 精确匹配。
   - 弯引号归一化。
   - tab / space 归一化。
   - 两者组合。
7. 如果匹配多处且 `replace_all == false`，拒绝并要求提供更多上下文。
8. 保留原始编码和换行风格。
9. 写入文件。
10. 更新 `readFileState`。
11. 返回 structured patch / diff。

### 7.2 Python 工具代码

```python
import difflib
from pydantic import BaseModel


class EditInput(BaseModel):
    file_path: str
    old_string: str
    new_string: str
    replace_all: bool = False


class EditOutput(BaseModel):
    file_path: str
    old_string: str
    new_string: str
    original_file: str
    updated_file: str
    diff: str
    replace_all: bool


def normalize_quotes(text: str) -> str:
    return (
        text.replace("‘", "'")
        .replace("’", "'")
        .replace("“", '"')
        .replace("”", '"')
    )


def normalize_whitespace(text: str) -> str:
    return text.replace("\t", "    ")


def find_actual_string(file_content: str, search: str) -> str | None:
    if search in file_content:
        return search

    normalized_file = normalize_quotes(file_content)
    normalized_search = normalize_quotes(search)
    idx = normalized_file.find(normalized_search)
    if idx != -1:
        return file_content[idx: idx + len(search)]

    ws_file = normalize_whitespace(file_content)
    ws_search = normalize_whitespace(search)
    idx = ws_file.find(ws_search)
    if idx != -1:
        # 简化版：多数代码场景中长度差异较小；复杂场景可按 TS 版本维护 offset 映射。
        return file_content[idx: idx + len(search)]

    combined_file = normalize_whitespace(normalized_file)
    combined_search = normalize_whitespace(normalized_search)
    idx = combined_file.find(combined_search)
    if idx != -1:
        return file_content[idx: idx + len(search)]

    return None


def detect_line_ending(raw: bytes) -> str:
    sample = raw[:4096]
    return "\r\n" if sample.count(b"\r\n") > sample.count(b"\n") - sample.count(b"\r\n") else "\n"


def read_text_with_metadata(path: str) -> tuple[str, str, str]:
    raw = Path(path).read_bytes()
    encoding = "utf-16-le" if raw.startswith(b"\xff\xfe") else "utf-8"
    line_ending = detect_line_ending(raw)
    text = raw.decode(encoding)
    return text.replace("\r\n", "\n"), encoding, line_ending


def write_text_preserving(path: str, content_lf: str, encoding: str, line_ending: str) -> None:
    final = content_lf.replace("\n", line_ending) if line_ending == "\r\n" else content_lf
    Path(path).write_bytes(final.encode(encoding))


def make_unified_diff(path: str, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


class EditTool(BaseTool[EditInput, EditOutput]):
    name = "Edit"
    input_model = EditInput
    max_result_size_chars = 100_000

    def description(self, input: EditInput | None = None) -> str:
        return "Edit a file by replacing an exact string."

    def get_path(self, input: EditInput) -> str:
        return expand_path(input.file_path)

    def validate_input(self, input: EditInput, context: ToolContext) -> ValidationResult:
        full_path = expand_path(input.file_path, context.cwd)
        if input.old_string == input.new_string:
            return ValidationResult(result=False, message="old_string and new_string are identical", error_code=1)
        if is_unc_path(full_path):
            return ValidationResult(result=True)
        if full_path.endswith(".ipynb"):
            return ValidationResult(result=False, message="Use a notebook-specific edit tool for .ipynb files", error_code=5)
        return ValidationResult(result=True)

    def check_permissions(self, input: EditInput, context: ToolContext) -> PermissionDecision:
        ctx = context.permission_context or ToolPermissionContext(mode="bypassPermissions")
        return check_write_permission(expand_path(input.file_path, context.cwd), ctx)

    def call(self, input: EditInput, context: ToolContext) -> EditOutput:
        full_path = expand_path(input.file_path, context.cwd)
        p = Path(full_path)
        p.parent.mkdir(parents=True, exist_ok=True)

        if p.exists():
            original, encoding, line_ending = read_text_with_metadata(full_path)
            if context.read_file_state:
                assert_file_not_modified_since_read(full_path, original, context.read_file_state)
        else:
            if input.old_string != "":
                raise FileNotFoundError(f"File does not exist: {input.file_path}")
            original, encoding, line_ending = "", "utf-8", "\n"

        if input.old_string == "":
            if original.strip() != "":
                raise RuntimeError("Cannot create new file: file already exists and is not empty")
            updated = input.new_string
            actual_old = ""
        else:
            actual_old = find_actual_string(original, input.old_string)
            if actual_old is None:
                raise RuntimeError(f"String to replace not found in file:\n{input.old_string}")

            matches = original.count(actual_old)
            if matches > 1 and not input.replace_all:
                raise RuntimeError(
                    f"Found {matches} matches. Provide more context or set replace_all=true."
                )
            updated = original.replace(actual_old, input.new_string) if input.replace_all else original.replace(actual_old, input.new_string, 1)

        write_text_preserving(full_path, updated, encoding, line_ending)

        if context.read_file_state:
            context.read_file_state.set(full_path, updated)

        diff = make_unified_diff(input.file_path, original, updated)
        return EditOutput(
            file_path=input.file_path,
            old_string=actual_old,
            new_string=input.new_string,
            original_file=original,
            updated_file=updated,
            diff=diff,
            replace_all=input.replace_all,
        )

    def map_result(self, output: EditOutput, tool_use_id: str) -> dict[str, Any]:
        if output.replace_all:
            content = f"The file {output.file_path} has been updated successfully. All occurrences were replaced."
        else:
            content = f"The file {output.file_path} has been updated successfully."
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
```

---

## 8. Write 工具实现

### 8.1 行为提取

`FileWriteTool` 是整文件写入工具，适合：

- 创建新文件。
- 全量覆盖已有文件。

核心保护：

- 写入前检查 denied path。
- 检查 team memory secrets（项目特定，可替换为 secret scanner）。
- 如果文件上次被读取后发生变化，拒绝覆盖。
- 创建父目录。
- 写入时保留已有编码，但全量写入内容按模型给出的换行处理。
- 写入后更新 `readFileState`。
- 返回 create/update。

### 8.2 Python 参考实现

```python
class WriteInput(BaseModel):
    file_path: str
    content: str


class WriteOutput(BaseModel):
    type: str  # create | update
    file_path: str
    original_file: str | None
    content: str
    diff: str


class WriteTool(BaseTool[WriteInput, WriteOutput]):
    name = "Write"
    input_model = WriteInput
    max_result_size_chars = 100_000

    def description(self, input: WriteInput | None = None) -> str:
        return "Create or overwrite a file."

    def get_path(self, input: WriteInput) -> str:
        return expand_path(input.file_path)

    def check_permissions(self, input: WriteInput, context: ToolContext) -> PermissionDecision:
        ctx = context.permission_context or ToolPermissionContext(mode="bypassPermissions")
        return check_write_permission(expand_path(input.file_path, context.cwd), ctx)

    def call(self, input: WriteInput, context: ToolContext) -> WriteOutput:
        full_path = expand_path(input.file_path, context.cwd)
        p = Path(full_path)
        p.parent.mkdir(parents=True, exist_ok=True)

        if p.exists():
            old_content, encoding, _line_ending = read_text_with_metadata(full_path)
            if context.read_file_state:
                assert_file_not_modified_since_read(full_path, old_content, context.read_file_state)
            kind = "update"
        else:
            old_content, encoding = None, "utf-8"
            kind = "create"

        # 与源码一致：全量写入时尊重模型给出的内容，不采样旧换行风格。
        p.write_bytes(input.content.encode(encoding))

        if context.read_file_state:
            context.read_file_state.set(full_path, input.content)

        diff = make_unified_diff(input.file_path, old_content or "", input.content)
        return WriteOutput(
            type=kind,
            file_path=input.file_path,
            original_file=old_content,
            content=input.content,
            diff=diff,
        )

    def map_result(self, output: WriteOutput, tool_use_id: str) -> dict[str, Any]:
        if output.type == "create":
            content = f"File created successfully at: {output.file_path}"
        else:
            content = f"The file {output.file_path} has been updated successfully."
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
```

---

## 9. Glob 工具实现

### 9.1 行为提取

`GlobTool` 用于按文件名模式查找文件：

- 参数：`pattern`、可选 `path`。
- 默认当前工作目录。
- 校验 path 必须存在且是目录。
- 只读、并发安全。
- 结果限制默认 100。
- 输出相对路径以节省 token。

### 9.2 Python 实现

```python
import glob as pyglob


class GlobInput(BaseModel):
    pattern: str
    path: str | None = None


class GlobOutput(BaseModel):
    duration_ms: int
    num_files: int
    filenames: list[str]
    truncated: bool


class GlobToolPy(BaseTool[GlobInput, GlobOutput]):
    name = "Glob"
    input_model = GlobInput

    def is_read_only(self, input: GlobInput) -> bool:
        return True

    def is_concurrency_safe(self, input: GlobInput) -> bool:
        return True

    def validate_input(self, input: GlobInput, context: ToolContext) -> ValidationResult:
        if input.path:
            full = Path(expand_path(input.path, context.cwd))
            if not full.exists():
                return ValidationResult(result=False, message=f"Directory does not exist: {input.path}", error_code=1)
            if not full.is_dir():
                return ValidationResult(result=False, message=f"Path is not a directory: {input.path}", error_code=2)
        return ValidationResult(result=True)

    def call(self, input: GlobInput, context: ToolContext) -> GlobOutput:
        start = time.time()
        base = Path(expand_path(input.path or context.cwd, context.cwd))
        matches = pyglob.glob(str(base / input.pattern), recursive=True)
        matches = sorted(matches, key=lambda p: Path(p).stat().st_mtime if Path(p).exists() else 0, reverse=True)
        limit = context.extra.get("glob_max_results", 100)
        truncated = len(matches) > limit
        matches = matches[:limit]
        rel = [str(Path(m).resolve().relative_to(Path(context.cwd).resolve())) if str(Path(m).resolve()).startswith(str(Path(context.cwd).resolve())) else m for m in matches]
        return GlobOutput(
            duration_ms=int((time.time() - start) * 1000),
            num_files=len(rel),
            filenames=rel,
            truncated=truncated,
        )

    def map_result(self, output: GlobOutput, tool_use_id: str) -> dict[str, Any]:
        content = "No files found" if not output.filenames else "\n".join(output.filenames)
        if output.truncated:
            content += "\n(Results are truncated. Use a more specific pattern.)"
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
```

---

## 10. Grep 工具实现

### 10.1 行为提取

`GrepTool` 基于 ripgrep：

- 参数：`pattern`、`path`、`glob`、`output_mode`、上下文行数、大小写、类型、分页等。
- 默认 `files_with_matches`。
- 自动排除 `.git`、`.svn`、`.hg` 等 VCS 目录。
- 读取权限中的 ignore pattern 会转为 ripgrep `--glob !pattern`。
- 支持三种输出：
  - `content`
  - `files_with_matches`
  - `count`
- 默认 `head_limit=250`，防止结果塞满上下文。

### 10.2 Python 简化实现

```python
import re


class GrepInput(BaseModel):
    pattern: str
    path: str | None = None
    glob_pattern: str | None = None
    output_mode: str = "files_with_matches"  # content | files_with_matches | count
    case_insensitive: bool = False
    head_limit: int | None = 250
    offset: int = 0


class GrepOutput(BaseModel):
    mode: str
    num_files: int
    filenames: list[str]
    content: str | None = None
    num_matches: int | None = None
    applied_limit: int | None = None
    applied_offset: int | None = None


VCS_EXCLUDES = {".git", ".svn", ".hg", ".bzr", ".jj", ".sl"}


def iter_text_files(root: Path, pattern: str | None = None):
    glob_pat = pattern or "**/*"
    for p in root.glob(glob_pat):
        if not p.is_file():
            continue
        if any(part in VCS_EXCLUDES for part in p.parts):
            continue
        try:
            p.read_text(encoding="utf-8")
        except Exception:
            continue
        yield p


class GrepToolPy(BaseTool[GrepInput, GrepOutput]):
    name = "Grep"
    input_model = GrepInput
    max_result_size_chars = 20_000

    def is_read_only(self, input: GrepInput) -> bool:
        return True

    def is_concurrency_safe(self, input: GrepInput) -> bool:
        return True

    def validate_input(self, input: GrepInput, context: ToolContext) -> ValidationResult:
        base = Path(expand_path(input.path or context.cwd, context.cwd))
        if not base.exists():
            return ValidationResult(result=False, message=f"Path does not exist: {input.path}", error_code=1)
        return ValidationResult(result=True)

    def call(self, input: GrepInput, context: ToolContext) -> GrepOutput:
        flags = re.IGNORECASE if input.case_insensitive else 0
        rx = re.compile(input.pattern, flags)
        base = Path(expand_path(input.path or context.cwd, context.cwd))
        files = list(iter_text_files(base, input.glob_pattern)) if base.is_dir() else [base]

        content_lines: list[str] = []
        matched_files: list[str] = []
        total_matches = 0

        for f in files:
            text = f.read_text(encoding="utf-8", errors="ignore")
            file_matches = 0
            for line_no, line in enumerate(text.splitlines(), start=1):
                if rx.search(line):
                    file_matches += 1
                    total_matches += 1
                    if input.output_mode == "content":
                        content_lines.append(f"{f}:{line_no}:{line}")
            if file_matches:
                matched_files.append(str(f))

        def rel(p: str) -> str:
            try:
                return str(Path(p).resolve().relative_to(Path(context.cwd).resolve()))
            except ValueError:
                return p

        if input.output_mode == "content":
            lines = [rel_line(context.cwd, line) for line in content_lines]
            sliced, applied = apply_limit(lines, input.head_limit, input.offset)
            return GrepOutput(mode="content", num_files=0, filenames=[], content="\n".join(sliced), applied_limit=applied)

        if input.output_mode == "count":
            lines = [rel(f) for f in matched_files]
            sliced, applied = apply_limit(lines, input.head_limit, input.offset)
            return GrepOutput(mode="count", num_files=len(sliced), filenames=[], content="\n".join(sliced), num_matches=total_matches, applied_limit=applied)

        files_rel = [rel(f) for f in matched_files]
        sliced, applied = apply_limit(files_rel, input.head_limit, input.offset)
        return GrepOutput(mode="files_with_matches", num_files=len(sliced), filenames=sliced, applied_limit=applied)

    def map_result(self, output: GrepOutput, tool_use_id: str) -> dict[str, Any]:
        if output.mode == "content":
            content = output.content or "No matches found"
        elif output.mode == "count":
            content = (output.content or "No matches found") + f"\n\nFound {output.num_matches or 0} total occurrences."
        else:
            content = "No files found" if not output.filenames else f"Found {output.num_files} files\n" + "\n".join(output.filenames)
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}


def apply_limit(items: list[str], limit: int | None, offset: int = 0) -> tuple[list[str], int | None]:
    if limit == 0:
        return items[offset:], None
    effective = 250 if limit is None else limit
    sliced = items[offset: offset + effective]
    applied = effective if len(items) - offset > effective else None
    return sliced, applied


def rel_line(cwd: str, line: str) -> str:
    # 简化处理：如果 line 以绝对路径开头，尝试转为相对路径。
    first_colon = line.find(":")
    if first_colon <= 0:
        return line
    path_part = line[:first_colon]
    rest = line[first_colon:]
    try:
        rel = Path(path_part).resolve().relative_to(Path(cwd).resolve())
        return str(rel) + rest
    except Exception:
        return line
```

生产环境建议优先调用 `ripgrep`，因为性能和 `.gitignore` 兼容性明显更好。

---

## 11. Bash / Shell 工具实现

### 11.1 行为提取

`BashTool` 的职责包括：

- 参数：`command`、`timeout`、`description`、`run_in_background`、`dangerouslyDisableSandbox`。
- 输入描述要求模型用简短主动语态描述命令。
- 判断命令是否只读。
- 支持权限规则如 `Bash(git *)`。
- 长命令支持进度输出。
- 大输出截断或持久化。
- 支持后台任务。
- 对 `sed -i` 类编辑命令可走预览/模拟编辑，使用户批准的内容与实际写入一致。
- 在子 Agent 中阻止改变主进程 cwd。
- 对 `sleep N` 这类轮询/等待命令有特殊阻断逻辑。

### 11.2 Python 简化实现

```python
import subprocess


class ShellInput(BaseModel):
    command: str
    timeout: int | None = None
    description: str | None = None
    run_in_background: bool = False


class ShellOutput(BaseModel):
    stdout: str
    stderr: str
    return_code: int
    interrupted: bool = False
    background_task_id: str | None = None


READ_ONLY_COMMANDS = {"ls", "cat", "head", "tail", "grep", "rg", "find", "pwd", "git status", "git diff"}


def looks_read_only(command: str) -> bool:
    stripped = command.strip()
    if any(op in stripped for op in [">", ">>", " rm ", "mv ", "cp ", "chmod ", "chown "]):
        return False
    return any(stripped.startswith(cmd) for cmd in READ_ONLY_COMMANDS)


class ShellTool(BaseTool[ShellInput, ShellOutput]):
    name = "Bash"
    input_model = ShellInput
    max_result_size_chars = 30_000

    def description(self, input: ShellInput | None = None) -> str:
        return input.description if input and input.description else "Run shell command."

    def is_read_only(self, input: ShellInput) -> bool:
        return looks_read_only(input.command)

    def is_concurrency_safe(self, input: ShellInput) -> bool:
        return self.is_read_only(input)

    def validate_input(self, input: ShellInput, context: ToolContext) -> ValidationResult:
        if input.command.strip().startswith("sleep "):
            return ValidationResult(
                result=False,
                message="Avoid long sleep/polling commands. Use a background task or monitor mechanism.",
                error_code=10,
            )
        return ValidationResult(result=True)

    def check_permissions(self, input: ShellInput, context: ToolContext) -> PermissionDecision:
        # 可扩展为 Bash(git *) / deny rm -rf 等规则。
        if self.is_read_only(input):
            return PermissionDecision(behavior="allow")
        ctx = context.permission_context
        if ctx and ctx.mode == "bypassPermissions":
            return PermissionDecision(behavior="allow")
        return PermissionDecision(behavior="ask", message=f"Allow command: {input.command}")

    def call(self, input: ShellInput, context: ToolContext) -> ShellOutput:
        timeout_sec = (input.timeout or 120_000) / 1000
        try:
            proc = subprocess.run(
                input.command,
                cwd=context.cwd,
                shell=True,
                text=True,
                capture_output=True,
                timeout=timeout_sec,
            )
            stdout = truncate_output(proc.stdout, self.max_result_size_chars)
            stderr = truncate_output(proc.stderr, self.max_result_size_chars)
            return ShellOutput(stdout=stdout, stderr=stderr, return_code=proc.returncode)
        except subprocess.TimeoutExpired as exc:
            return ShellOutput(
                stdout=exc.stdout or "",
                stderr=(exc.stderr or "") + "\nCommand timed out",
                return_code=124,
                interrupted=True,
            )

    def map_result(self, output: ShellOutput, tool_use_id: str) -> dict[str, Any]:
        parts = []
        if output.stdout:
            parts.append(output.stdout.strip())
        if output.stderr:
            parts.append(output.stderr.strip())
        if output.return_code != 0:
            parts.append(f"Exit code {output.return_code}")
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": "\n".join(parts),
            "is_error": output.interrupted,
        }


def truncate_output(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = limit // 2
    return text[:keep] + "\n...[truncated]...\n" + text[-keep:]
```

生产环境建议补充：

- 使用 `asyncio.create_subprocess_shell` 实时读取输出并上报 progress。
- 后台任务注册表。
- sandbox / allowlist。
- 命令 AST 解析，而不是简单字符串判断。
- 大输出落盘并只返回 preview。

---

## 12. 工具注册与过滤

`src/tools.ts` 的核心思路：

1. 内置工具集中注册。
2. 按 feature flag / 环境变量条件加载工具。
3. 按 permission deny rule 过滤工具，避免模型看到完全禁止的工具。
4. 简单模式只暴露 `Bash`、`Read`、`Edit`。
5. 合并 MCP 工具时，内置工具优先，按名称去重。

Python 版：

```python
def get_base_tools(simple: bool = False) -> list[BaseTool[Any, Any]]:
    if simple:
        return [ShellTool(), ReadTool(), EditTool()]
    return [ShellTool(), GlobToolPy(), GrepToolPy(), ReadTool(), EditTool(), WriteTool()]


def filter_tools_by_permissions(
    tools: list[BaseTool[Any, Any]],
    permission_context: ToolPermissionContext,
) -> list[BaseTool[Any, Any]]:
    # 如果需要 blanket deny，可以在这里按 tool.name 过滤。
    return [t for t in tools if t.is_enabled()]


def assemble_tool_pool(
    builtins: list[BaseTool[Any, Any]],
    external_tools: list[BaseTool[Any, Any]] | None = None,
) -> list[BaseTool[Any, Any]]:
    result: dict[str, BaseTool[Any, Any]] = {}
    for tool in sorted(builtins, key=lambda t: t.name):
        result.setdefault(tool.name, tool)
    for tool in sorted(external_tools or [], key=lambda t: t.name):
        result.setdefault(tool.name, tool)
    return list(result.values())
```

---

## 13. 对 Python 项目的落地目录建议

推荐结构：

```text
agent_tools/
  __init__.py
  base.py              # Tool / BaseTool / ToolContext / ToolExecutor
  permissions.py       # ToolPermissionContext / read/write/shell 权限
  file_state.py        # ReadFileState
  path_utils.py        # expand_path / is_unc_path / safe realpath
  file_utils.py        # encoding / line ending / diff
  tools/
    read.py
    edit.py
    write.py
    glob.py
    grep.py
    shell.py
```

最小可用集：

- `base.py`
- `file_state.py`
- `permissions.py`
- `tools/read.py`
- `tools/edit.py`
- `tools/write.py`

如果 Agent 要自主探索代码，再加：

- `tools/glob.py`
- `tools/grep.py`

如果 Agent 要运行测试、构建、格式化，再加：

- `tools/shell.py`

---

## 14. 实现时的关键注意事项

### 14.1 不要跳过“读后再写”保护

代码编辑 Agent 最常见的风险是覆盖用户刚改的内容。必须保留：

```text
Read -> cache(content + mtime) -> Edit/Write 前检查 mtime/content -> 写入后刷新 cache
```

### 14.2 Edit 优先于 Write

让模型修改现有文件时优先使用 `Edit(old_string, new_string)`，原因：

- diff 更小。
- 更容易审计。
- 更不容易覆盖整文件。
- 可强制唯一匹配。

`Write` 只用于新文件或确实需要全量覆盖。

### 14.3 路径必须规范化，但结果消息尽量保留模型原始路径

Claude Code 中有一个细节：`backfillObservableInput()` 会给 hook / 权限系统使用规范化路径，但 `call()` 尽量保留模型原始输入，避免 transcript 变化。

Python 项目里可简化为：

- 权限和文件系统操作用 absolute path。
- 返回给模型的消息使用用户/模型传入的原始 path。

### 14.4 Windows UNC 路径不要提前访问

在 Windows 上，`exists()` / `stat()` 访问 UNC 路径可能触发网络认证。校验阶段应当先识别：

```python
path.startswith("\\\\") or path.startswith("//")
```

然后交给权限层处理，不要做文件系统访问。

### 14.5 大输出要截断或落盘

`Read`、`Grep`、`Shell` 都可能产生巨大结果。建议：

- `Read`: 行范围 + 最大字节/最大 token。
- `Grep`: 默认 `head_limit`。
- `Shell`: 截断中间内容，保留开头和结尾；更完整做法是落盘并返回文件路径 + preview。

### 14.6 Shell 工具需要更保守

Shell 是最高风险工具。至少要做到：

- 只读命令自动允许。
- 写操作、删除、网络、权限变更命令需要确认。
- 非交互模式下默认拒绝危险命令。
- 不要允许模型传入内部字段绕过权限。

---

## 15. 最小集成示例

```python
def build_executor(cwd: str) -> tuple[ToolExecutor, ToolContext]:
    read_state = ReadFileState()
    permission = ToolPermissionContext(
        read_allow=["**"],
        write_allow=["**"],
        mode="bypassPermissions",
    )
    context = ToolContext(
        cwd=cwd,
        permission_context=permission,
        read_file_state=read_state,
    )
    tools = [ReadTool(), EditTool(), WriteTool(), GlobToolPy(), GrepToolPy(), ShellTool()]
    return ToolExecutor(tools), context


executor, ctx = build_executor("/path/to/project")

# 1. 先读文件
print(executor.execute("Read", {"file_path": "src/app.py"}, "toolu_1", ctx))

# 2. 再精确替换
print(executor.execute(
    "Edit",
    {
        "file_path": "src/app.py",
        "old_string": "print('hello')",
        "new_string": "print('hello world')",
    },
    "toolu_2",
    ctx,
))
```

---

## 16. 推荐迁移优先级

1. `Tool` 抽象 + `ToolExecutor`。
2. `ReadFileState`。
3. `ReadTool`。
4. `EditTool`。
5. `WriteTool`。
6. `GlobTool` / `GrepTool`。
7. `ShellTool`。
8. 权限规则增强。
9. 大输出落盘。
10. Hook / telemetry / UI。

---

## 17. 与原实现的差异说明

本文给出的 Python 代码是可落地的“等价设计简化版”，相较原仓库省略或简化了：

- React/Ink UI 渲染。
- LSP didChange / didSave 通知。
- VS Code diff 通知。
- file history undo 备份。
- skill 自动发现与激活。
- telemetry / OTel / Langfuse。
- MCP 工具合并。
- Notebook、PDF、图片读取。
- shell sandbox 和完整命令 AST 安全分析。
- 大输出持久化完整协议。

如果 Python 项目需要达到接近 Claude Code 的工程强度，建议按第 16 节继续补齐。
