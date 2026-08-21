# Agent Skills 系统实现提取（Python 版参考）

> 从 Claude Code Skills 系统提取的设计说明与 Python 参考实现，覆盖 Skill 加载、调用、搜索、权限与安全边界。

← [文档索引](README.md) · [README](../README.md)

---

本文档提取当前仓库中 Claude Code 的 **Skills** 系统实现方式，并整理为适合 Python Agent 项目复刻的设计说明与参考代码。

Skills 的本质不是“普通 prompt 模板文件”，而是一套建立在 `Command` 与 `Tool` 系统之上的可发现、可授权、可延迟加载、可动态激活、可作为子 Agent 执行的能力扩展机制。

> Sakura 当前实现补充：本项目已在 Agent Team 中实现 Skills 管理与调用能力，当前定位为“给 Agent 注入可复用说明和操作流程”。Skills 不会扩大 Agent 的文件、shell 或 Git 权限，所有实际操作仍受 Agent Team 受控工具和白名单限制。

> 主要参考源码：
>
> - `src/skills/loadSkillsDir.ts`
> - `src/commands.ts`
> - `src/types/command.ts`
> - `packages/builtin-tools/src/tools/SkillTool/SkillTool.ts`
> - `packages/builtin-tools/src/tools/SkillTool/prompt.ts`
> - `packages/builtin-tools/src/tools/DiscoverSkillsTool/DiscoverSkillsTool.ts`
> - `src/utils/processUserInput/processSlashCommand.tsx`
> - `src/utils/forkedAgent.ts`
> - `src/bootstrap/state.ts`
> - `src/utils/hooks/registerSkillHooks.ts`
> - `src/skills/bundledSkills.ts`
> - `src/skills/bundled/index.ts`
> - `src/skills/mcpSkills.ts`
> - `src/skills/mcpSkillBuilders.ts`
> - `src/services/skillSearch/localSearch.ts`
> - `src/services/skillSearch/prefetch.ts`
> - `src/components/skills/SkillsMenu.tsx`

---

## 1. 总体设计

Claude Code 的 Skills 系统由四层组成：

```text
SKILL.md / bundled / plugin / MCP resource
  -> Skill loader 解析 frontmatter 和 markdown body
  -> 转成 PromptCommand
  -> getCommands / getSkillToolCommands 聚合
  -> SkillTool 暴露给模型调用
  -> processPromptSlashCommand 展开为 meta user message
  -> query loop 继续执行 skill prompt 中的指令
```

关键设计点：

1. **Skill 复用 Command 抽象**：Skill 最终是 `type: 'prompt'` 的 `Command`。
2. **模型不能直接读取全部 Skill 内容**：系统 prompt 只列出名称与短描述，完整 `SKILL.md` 只有调用 `Skill` tool 后才注入上下文。
3. **Skill 调用走权限系统**：有危险字段、hooks、allowed-tools 等能力时需要用户授权。
4. **支持 inline 与 fork**：默认在当前对话展开；`context: fork` 时在子 Agent 中执行。
5. **支持动态发现**：文件工具触碰某些路径时，可以发现下层 `.claude/skills` 并激活 `paths` 条件技能。
6. **支持搜索与预加载**：`DiscoverSkills` 和 turn-zero prefetch 用 TF-IDF 搜索相关技能。
7. **支持 compaction 保存**：已调用 Skill 的内容保存到 session state，压缩后可恢复。

### 1.1 Sakura Agent Team 当前落地范围

Sakura 的 Agent Skills 功能面向超级管理员开放，主要用于增强 Agent Team 的任务执行知识：

- WebUI 上传单个 `SKILL.md`。
- WebUI 上传 ZIP 技能包。
- 从 GitHub blob/raw `SKILL.md` 安装。
- 在 WebUI 中启用、禁用和删除 Skill。
- Agent 通过 `use_skill` 工具按需读取完整 Skill 内容。
- 通过 `agent_team_skills_enabled` 控制是否允许 Agent 使用 Skills。
- 通过 `agent_team_skills_root` 配置 Skills 本地存储根目录。
- 内置 `ruff-lint` Skill（展示名 `Ruff Lint & Format`），指导 Agent 使用 `ruff check`、`ruff check --fix` 和 `ruff format`。

Sakura 当前的 Skills 更接近“延迟加载的任务说明书”，不实现额外工具授权扩展；即便 Skill 文档中提到 shell 命令，执行时仍必须经过 Agent Team 的命令白名单与工作区安全边界。

---

## 2. Skill 文件格式

标准目录结构：

```text
.claude/
  skills/
    commit/
      SKILL.md
    review-pr/
      SKILL.md
```

`/skills/` 目录只支持 `skill-name/SKILL.md` 格式，不支持单个 `.md` 文件。

示例：

```markdown
---
name: commit
description: Create a clean git commit for the current change.
allowed-tools:
  - Bash(git status:*)
  - Bash(git diff:*)
  - Bash(git add:*)
  - Bash(git commit:*)
when_to_use: Use when the user asks to commit current changes.
argument-hint: "[commit message]"
arguments:
  - message
context: inline
---

# Commit Workflow

Create a commit using this message if provided:

`$message`

## Steps

1. Inspect `git status`.
2. Inspect `git diff`.
3. Stage only relevant files.
4. Commit with a concise Conventional Commit message.
```

### 2.1 frontmatter 字段

核心字段来自 `parseSkillFrontmatterFields()`：

| 字段 | 类型 | 作用 |
|---|---|---|
| `name` | string | 展示名。目录名才是真正 skill name；`name` 主要用于 UI 展示。 |
| `description` | string | 短描述，用于列表和搜索。缺失时从 Markdown 内容提取。 |
| `allowed-tools` | string/list | Skill 运行时额外允许的工具权限。 |
| `argument-hint` | string | UI 中展示的参数提示。 |
| `arguments` | string/list | 命名参数，用于 `$arg_name` 替换。 |
| `when_to_use` | string | 详细触发条件，搜索和模型判断使用。 |
| `version` | string | Skill 版本。 |
| `model` | string | 调用 Skill 后可覆盖主循环模型。`inherit` 表示不覆盖。 |
| `disable-model-invocation` | boolean | 禁止模型通过 `SkillTool` 调用，只允许用户 slash command 调用。 |
| `user-invocable` | boolean | 用户是否能通过 `/<skill>` 调用；默认 true。 |
| `hooks` | object | 调用 Skill 后注册 session hooks。 |
| `context` | `inline`/`fork` | `fork` 表示用子 Agent 执行。省略为 inline。 |
| `agent` | string | fork 时使用的 agent 类型。 |
| `effort` | string/int | effort override。 |
| `shell` | object | Markdown 内 shell injection 的配置。 |
| `paths` | string/list | 条件 Skill，仅在触碰匹配路径后激活。 |

---

## 3. TypeScript 调用链还原

### 3.1 加载阶段

`src/skills/loadSkillsDir.ts` 负责把 `SKILL.md` 转为 `Command`：

```text
getSkillDirCommands(cwd)
  -> loadSkillsFromSkillsDir(managed/user/project/additional)
  -> loadSkillsFromCommandsDir(cwd)       # legacy /commands
  -> dedupe by realpath
  -> split conditional skills by paths
  -> return unconditional prompt commands
```

加载来源：

| 来源 | 路径/入口 | `source` | `loadedFrom` |
|---|---|---|---|
| 用户全局 | `~/.claude/skills/<name>/SKILL.md` | `userSettings` | `skills` |
| 项目 | `.claude/skills/<name>/SKILL.md` | `projectSettings` | `skills` |
| 管理策略 | managed `.claude/skills` | `policySettings` | `skills` |
| 额外目录 | `--add-dir/.claude/skills` | `projectSettings` | `skills` |
| 旧 commands | `commands/**.md` / `commands/**/SKILL.md` | setting source | `commands_DEPRECATED` |
| bundled | 程序内注册 | `bundled` | `bundled` |
| plugin | 插件系统 | `plugin` | `plugin` |
| MCP | `skill://` resource | `mcp` | `mcp` |

### 3.2 命令聚合阶段

`src/commands.ts` 将 skills 合并到命令列表：

```text
getSkills(cwd)
  -> getSkillDirCommands(cwd)
  -> getPluginSkills()
  -> getBundledSkills()
  -> getBuiltinPluginSkillCommands()

getCommands(cwd)
  -> built-in commands + local commands + skills
  -> getDynamicSkills()
  -> filter enabled / availability

getSkillToolCommands(cwd)
  -> prompt commands
  -> not disableModelInvocation
  -> source != builtin
  -> loadedFrom in skills/bundled/commands or has description/whenToUse
```

### 3.3 模型可见提示阶段

`SkillTool.prompt()` 使用 `packages/builtin-tools/src/tools/SkillTool/prompt.ts` 生成工具说明。关键约束：

- 用户提到 `/commit`、`/review-pr` 等 slash command 时，应理解为 skill。
- 如果匹配某个 skill，调用 `Skill` tool 是 **阻塞要求**，必须先调用再回答。
- 看到当前 turn 里已有 `<command-name>` 标签，说明 skill 已经加载，不要重复调用。
- Skill 列表只放名称和短描述，描述预算约为上下文窗口的 1%。

### 3.4 调用阶段

`SkillTool` 的生命周期：

```text
LLM tool_use: Skill({ skill, args? })
  -> validateInput
     - trim name
     - 去掉开头 /
     - 查找 Command
     - 禁止 disableModelInvocation
     - 必须是 prompt command
  -> checkPermissions
     - deny rules
     - remote canonical skill allow
     - allow rules
     - safe properties auto allow
     - otherwise ask user
  -> call
     - recordSkillUsage
     - context=fork: executeForkedSkill
     - default inline: processPromptSlashCommand
  -> mapToolResultToToolResultBlockParam
```

inline 调用实际会生成新的 meta user message：

```text
Skill tool result: Launching skill: commit
newMessages:
  - hidden command metadata message
  - hidden meta user message containing expanded SKILL.md
  - optional attachment messages
  - command_permissions attachment
```

之后主 query loop 会继续，把 skill 内容当成当前上下文中的用户指令处理。

### 3.5 Prompt 展开阶段

`createSkillCommand().getPromptForCommand()` 做以下处理：

1. 如果有 `baseDir`，在正文前加：`Base directory for this skill: <baseDir>`。
2. 替换 `$ARGUMENTS` 和命名参数 `$name`。
3. 替换 `${CLAUDE_SKILL_DIR}` 为技能目录。
4. 替换 `${CLAUDE_SESSION_ID}` 为当前会话 ID。
5. 非 MCP skills 才执行 Markdown 内 shell injection。
6. 返回 `{ type: 'text', text: finalContent }`。

MCP skills 是远程不可信内容，因此不会执行 Markdown 中的 shell injection。

---

## 4. Python 版推荐架构

建议拆成以下模块：

```text
agent_skills/
  models.py              # SkillCommand / SkillFrontmatter / SkillResult
  frontmatter.py         # YAML frontmatter parser
  loader.py              # 文件系统 loader
  registry.py            # skill registry + command aggregation
  skill_tool.py          # LLM tool implementation
  search.py              # TF-IDF search
  discovery.py           # dynamic discovery + conditional activation
  hooks.py               # session hook manager
  bundled.py             # bundled skill registration/extraction
  mcp.py                 # MCP skill:// resources
  state.py               # invoked skills preservation
```

依赖建议：

```text
pydantic>=2
pyyaml>=6
pathspec>=0.12   # 可选：更接近 gitignore-style paths 匹配
```

---

## 5. Python 核心数据模型

```python
# agent_skills/models.py
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

ContentBlock = dict[str, Any]
ExecutionContext = Literal["inline", "fork"]
SkillSource = Literal[
    "userSettings",
    "projectSettings",
    "policySettings",
    "plugin",
    "bundled",
    "mcp",
]
LoadedFrom = Literal[
    "skills",
    "commands_DEPRECATED",
    "plugin",
    "managed",
    "bundled",
    "mcp",
]


@dataclass
class SkillFrontmatter:
    display_name: str | None = None
    description: str = ""
    has_user_specified_description: bool = False
    allowed_tools: list[str] = field(default_factory=list)
    argument_hint: str | None = None
    argument_names: list[str] = field(default_factory=list)
    when_to_use: str | None = None
    version: str | None = None
    model: str | None = None
    disable_model_invocation: bool = False
    user_invocable: bool = True
    hooks: dict[str, Any] | None = None
    context: ExecutionContext = "inline"
    agent: str | None = None
    effort: str | int | None = None
    shell: dict[str, Any] | None = None
    paths: list[str] | None = None


@dataclass
class SkillCommand:
    name: str
    description: str
    markdown_content: str
    source: SkillSource
    loaded_from: LoadedFrom
    base_dir: Path | None = None
    display_name: str | None = None
    has_user_specified_description: bool = False
    allowed_tools: list[str] = field(default_factory=list)
    argument_hint: str | None = None
    argument_names: list[str] = field(default_factory=list)
    when_to_use: str | None = None
    version: str | None = None
    model: str | None = None
    disable_model_invocation: bool = False
    user_invocable: bool = True
    hooks: dict[str, Any] | None = None
    context: ExecutionContext = "inline"
    agent: str | None = None
    effort: str | int | None = None
    shell: dict[str, Any] | None = None
    paths: list[str] | None = None
    content_length: int = 0

    @property
    def is_hidden(self) -> bool:
        return not self.user_invocable

    def user_facing_name(self) -> str:
        return self.display_name or self.name


@dataclass
class ToolUseContext:
    cwd: Path
    session_id: str
    messages: list[dict[str, Any]]
    app_state: dict[str, Any]
    agent_id: str | None = None
    discovered_skill_names: set[str] = field(default_factory=set)
```

---

## 6. frontmatter 解析

```python
# agent_skills/frontmatter.py
from __future__ import annotations

import re
from typing import Any

import yaml

from .models import SkillFrontmatter


FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def parse_markdown_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw
    data = yaml.safe_load(match.group(1)) or {}
    if not isinstance(data, dict):
        data = {}
    return data, raw[match.end() :]


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value).strip()]


def extract_description_from_markdown(body: str, fallback_label: str) -> str:
    for line in body.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:300]
    return fallback_label


def parse_argument_names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip().lstrip("$") for p in re.split(r"[,\s]+", value) if p.strip()]
    if isinstance(value, list):
        return [str(p).strip().lstrip("$") for p in value if str(p).strip()]
    return []


def parse_skill_paths(value: Any) -> list[str] | None:
    patterns = as_string_list(value)
    normalized: list[str] = []
    for pattern in patterns:
        p = pattern.strip()
        if p.endswith("/**"):
            p = p[:-3]
        if p:
            normalized.append(p)
    if not normalized or all(p == "**" for p in normalized):
        return None
    return normalized


def parse_skill_frontmatter(
    data: dict[str, Any],
    body: str,
    resolved_name: str,
    fallback_label: str = "Skill",
) -> SkillFrontmatter:
    has_description = isinstance(data.get("description"), str)
    description = (
        str(data["description"])
        if has_description
        else extract_description_from_markdown(body, fallback_label)
    )

    model_raw = data.get("model")
    model = None if model_raw in (None, "inherit") else str(model_raw)

    context = "fork" if data.get("context") == "fork" else "inline"

    return SkillFrontmatter(
        display_name=str(data["name"]) if data.get("name") is not None else None,
        description=description,
        has_user_specified_description=has_description,
        allowed_tools=as_string_list(data.get("allowed-tools")),
        argument_hint=str(data["argument-hint"])
        if data.get("argument-hint") is not None
        else None,
        argument_names=parse_argument_names(data.get("arguments")),
        when_to_use=str(data["when_to_use"])
        if data.get("when_to_use") is not None
        else None,
        version=str(data["version"]) if data.get("version") is not None else None,
        model=model,
        disable_model_invocation=parse_bool(
            data.get("disable-model-invocation"), False
        ),
        user_invocable=parse_bool(data.get("user-invocable"), True),
        hooks=data.get("hooks") if isinstance(data.get("hooks"), dict) else None,
        context=context,
        agent=str(data["agent"]) if data.get("agent") is not None else None,
        effort=data.get("effort"),
        shell=data.get("shell") if isinstance(data.get("shell"), dict) else None,
        paths=parse_skill_paths(data.get("paths")),
    )
```

---

## 7. 参数替换与 prompt 展开

对应 TypeScript 的 `createSkillCommand().getPromptForCommand()`。

```python
# agent_skills/expand.py
from __future__ import annotations

import re
from pathlib import Path

from .models import SkillCommand, ToolUseContext


def substitute_arguments(content: str, args: str, arg_names: list[str]) -> str:
    # 兼容 Claude Code 常见 `$ARGUMENTS` 形式。
    content = content.replace("$ARGUMENTS", args)

    if not arg_names:
        return content

    # 简化版：按空格切分命名参数。生产环境可换成 shell-like parser。
    values = args.split()
    mapping = {
        name: values[i] if i < len(values) else "" for i, name in enumerate(arg_names)
    }
    for name, value in mapping.items():
        content = content.replace(f"${name}", value)
        content = content.replace("${" + name + "}", value)
    return content


SHELL_INLINE_RE = re.compile(r"!`([^`]+)`")


async def execute_shell_injections(
    content: str, skill: SkillCommand, context: ToolUseContext
) -> str:
    """可选能力：执行 !`cmd` 并替换为输出。

    注意：Claude Code 对 MCP skills 禁止 shell injection。Python 项目建议默认关闭，
    只在本地可信 skill 且命令命中 allowed-tools 时开启。
    """
    if skill.loaded_from == "mcp":
        return content
    # 这里给出安全占位：生产实现应接入上一份工具文档中的 BashTool 权限系统。
    return content


async def expand_skill_prompt(
    skill: SkillCommand, args: str, context: ToolUseContext
) -> list[dict]:
    final = skill.markdown_content

    if skill.base_dir:
        final = f"Base directory for this skill: {skill.base_dir}\n\n{final}"

    final = substitute_arguments(final, args, skill.argument_names)

    if skill.base_dir:
        skill_dir = str(skill.base_dir).replace("\\", "/")
        final = final.replace("${CLAUDE_SKILL_DIR}", skill_dir)

    final = final.replace("${CLAUDE_SESSION_ID}", context.session_id)
    final = await execute_shell_injections(final, skill, context)

    return [{"type": "text", "text": final}]
```

---

## 8. 文件系统 Skill Loader

```python
# agent_skills/loader.py
from __future__ import annotations

from pathlib import Path

from .frontmatter import parse_markdown_frontmatter, parse_skill_frontmatter
from .models import LoadedFrom, SkillCommand, SkillSource


def create_skill_command(
    *,
    skill_name: str,
    raw_markdown: str,
    source: SkillSource,
    loaded_from: LoadedFrom,
    base_dir: Path | None,
    fallback_label: str = "Skill",
) -> SkillCommand:
    frontmatter, body = parse_markdown_frontmatter(raw_markdown)
    parsed = parse_skill_frontmatter(frontmatter, body, skill_name, fallback_label)
    return SkillCommand(
        name=skill_name,
        display_name=parsed.display_name,
        description=parsed.description,
        has_user_specified_description=parsed.has_user_specified_description,
        markdown_content=body,
        allowed_tools=parsed.allowed_tools,
        argument_hint=parsed.argument_hint,
        argument_names=parsed.argument_names,
        when_to_use=parsed.when_to_use,
        version=parsed.version,
        model=parsed.model,
        disable_model_invocation=parsed.disable_model_invocation,
        user_invocable=parsed.user_invocable,
        hooks=parsed.hooks,
        context=parsed.context,
        agent=parsed.agent,
        effort=parsed.effort,
        shell=parsed.shell,
        paths=parsed.paths,
        source=source,
        loaded_from=loaded_from,
        base_dir=base_dir,
        content_length=len(body),
    )


def load_skills_from_skills_dir(
    base_path: Path, source: SkillSource
) -> list[tuple[SkillCommand, Path]]:
    """只支持 skill-name/SKILL.md。"""
    if not base_path.exists() or not base_path.is_dir():
        return []

    results: list[tuple[SkillCommand, Path]] = []
    for entry in base_path.iterdir():
        if not entry.is_dir():
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.exists():
            continue
        raw = skill_file.read_text(encoding="utf-8")
        skill = create_skill_command(
            skill_name=entry.name,
            raw_markdown=raw,
            source=source,
            loaded_from="skills",
            base_dir=entry,
        )
        results.append((skill, skill_file))
    return results


def load_legacy_commands_dir(
    commands_dir: Path, source: SkillSource
) -> list[tuple[SkillCommand, Path]]:
    """兼容旧 commands 目录：支持 name.md 和 name/SKILL.md。"""
    if not commands_dir.exists() or not commands_dir.is_dir():
        return []

    results: list[tuple[SkillCommand, Path]] = []
    for path in commands_dir.rglob("*.md"):
        if path.name.lower() == "skill.md":
            name = path.parent.name
            base_dir = path.parent
        else:
            name = path.stem
            base_dir = None
        raw = path.read_text(encoding="utf-8")
        skill = create_skill_command(
            skill_name=name,
            raw_markdown=raw,
            source=source,
            loaded_from="commands_DEPRECATED",
            base_dir=base_dir,
            fallback_label="Custom command",
        )
        results.append((skill, path))
    return results
```

---

## 9. Registry、去重与条件 Skill

```python
# agent_skills/registry.py
from __future__ import annotations

from pathlib import Path

from .loader import load_legacy_commands_dir, load_skills_from_skills_dir
from .models import SkillCommand


class SkillRegistry:
    def __init__(self, cwd: Path, user_home: Path, managed_root: Path | None = None):
        self.cwd = cwd
        self.user_home = user_home
        self.managed_root = managed_root
        self.commands: dict[str, SkillCommand] = {}
        self.dynamic_skills: dict[str, SkillCommand] = {}
        self.conditional_skills: dict[str, SkillCommand] = {}
        self.activated_conditional: set[str] = set()

    def load_all(self) -> None:
        loaded: list[tuple[SkillCommand, Path]] = []

        if self.managed_root:
            loaded += load_skills_from_skills_dir(
                self.managed_root / ".claude" / "skills", "policySettings"
            )

        loaded += load_skills_from_skills_dir(
            self.user_home / ".claude" / "skills", "userSettings"
        )
        loaded += load_skills_from_skills_dir(
            self.cwd / ".claude" / "skills", "projectSettings"
        )
        loaded += load_legacy_commands_dir(
            self.cwd / ".claude" / "commands", "projectSettings"
        )

        # realpath 去重：同一文件通过 symlink/重复父目录出现时 first-wins。
        seen_files: set[Path] = set()
        for skill, file_path in loaded:
            try:
                identity = file_path.resolve()
            except OSError:
                identity = file_path
            if identity in seen_files:
                continue
            seen_files.add(identity)

            if skill.paths and skill.name not in self.activated_conditional:
                self.conditional_skills[skill.name] = skill
            else:
                self.commands[skill.name] = skill

    def get_skill_tool_commands(self) -> list[SkillCommand]:
        result: list[SkillCommand] = []
        for skill in [*self.commands.values(), *self.dynamic_skills.values()]:
            if skill.disable_model_invocation:
                continue
            if skill.loaded_from in {"skills", "bundled", "commands_DEPRECATED"}:
                result.append(skill)
            elif skill.has_user_specified_description or skill.when_to_use:
                result.append(skill)
        return result

    def find(self, name: str) -> SkillCommand | None:
        normalized = name[1:] if name.startswith("/") else name
        return self.dynamic_skills.get(normalized) or self.commands.get(normalized)

    def add_dynamic_skills(self, skills: list[SkillCommand]) -> None:
        for skill in skills:
            if skill.name not in self.commands:
                self.dynamic_skills[skill.name] = skill
```

---

## 10. 动态发现与 paths 条件激活

Claude Code 在 FileRead/FileWrite/FileEdit 等文件工具中会调用：

- `discoverSkillDirsForPaths(filePaths, cwd)`
- `addSkillDirectories(dirs)`
- `activateConditionalSkillsForPaths(filePaths, cwd)`

语义：

1. 从被触碰文件的父目录向上走到 cwd，但不包含 cwd 本身。
2. 查找每级目录下的 `.claude/skills`。
3. 已检查过的路径用 set 记住，避免反复 stat。
4. 跳过 gitignored 路径下的 skills。
5. 更深目录的 skills 优先级更高。
6. `paths` 条件 skill 初始不暴露，直到文件路径匹配后才加入 dynamic skills。

Python 参考：

```python
# agent_skills/discovery.py
from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

from .loader import load_skills_from_skills_dir
from .registry import SkillRegistry


class DynamicSkillDiscovery:
    def __init__(self, registry: SkillRegistry):
        self.registry = registry
        self.checked_dirs: set[Path] = set()

    def discover_skill_dirs_for_paths(self, file_paths: list[Path]) -> list[Path]:
        cwd = self.registry.cwd.resolve()
        new_dirs: list[Path] = []

        for file_path in file_paths:
            current = file_path.resolve().parent
            while str(current).startswith(str(cwd) + str(Path("/"))):
                skill_dir = current / ".claude" / "skills"
                if skill_dir not in self.checked_dirs:
                    self.checked_dirs.add(skill_dir)
                    if skill_dir.exists() and skill_dir.is_dir():
                        # 生产环境建议调用 git check-ignore 跳过 gitignored 路径。
                        new_dirs.append(skill_dir)
                if current == current.parent:
                    break
                current = current.parent

        return sorted(new_dirs, key=lambda p: len(p.parts), reverse=True)

    def add_skill_directories(self, dirs: list[Path]) -> list[str]:
        added: list[str] = []
        # shallower first, deeper later覆盖。
        for skill_dir in reversed(dirs):
            loaded = load_skills_from_skills_dir(skill_dir, "projectSettings")
            for skill, _path in loaded:
                if skill.name not in self.registry.dynamic_skills:
                    added.append(skill.name)
                self.registry.dynamic_skills[skill.name] = skill
        return added

    def activate_conditional_skills_for_paths(
        self, file_paths: list[Path]
    ) -> list[str]:
        activated: list[str] = []
        cwd = self.registry.cwd.resolve()

        for name, skill in list(self.registry.conditional_skills.items()):
            if not skill.paths:
                continue
            for file_path in file_paths:
                try:
                    rel = file_path.resolve().relative_to(cwd).as_posix()
                except ValueError:
                    continue
                if any(
                    fnmatch(rel, pattern) or rel.startswith(pattern.rstrip("/") + "/")
                    for pattern in skill.paths
                ):
                    self.registry.dynamic_skills[name] = skill
                    self.registry.conditional_skills.pop(name, None)
                    self.registry.activated_conditional.add(name)
                    activated.append(name)
                    break
        return activated
```

---

## 11. SkillTool 实现

### 11.1 权限模型

`SkillTool.checkPermissions()` 的策略：

1. deny rule 优先。
2. allow rule 命中则允许。
3. 只有 safe properties 的 skill 自动允许。
4. 其他情况 ask。

safe properties 是白名单：新增未知属性默认不安全，需要用户确认。这是一个重要安全设计。

Python 参考：

```python
# agent_skills/skill_tool.py
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from .expand import expand_skill_prompt
from .models import SkillCommand, ToolUseContext
from .registry import SkillRegistry
from .state import InvokedSkillStore


PermissionBehavior = Literal["allow", "deny", "ask"]


@dataclass
class PermissionDecision:
    behavior: PermissionBehavior
    message: str | None = None
    updated_input: dict[str, Any] | None = None


SAFE_SKILL_FIELDS = {
    "name",
    "description",
    "markdown_content",
    "source",
    "loaded_from",
    "base_dir",
    "display_name",
    "has_user_specified_description",
    "argument_hint",
    "argument_names",
    "when_to_use",
    "version",
    "model",
    "disable_model_invocation",
    "user_invocable",
    "context",
    "agent",
    "effort",
    "paths",
    "content_length",
}


def skill_has_only_safe_properties(skill: SkillCommand) -> bool:
    data = skill.__dict__
    for key, value in data.items():
        if key in SAFE_SKILL_FIELDS:
            continue
        if value in (None, "", [], {}):
            continue
        return False
    return True


class SkillTool:
    name = "Skill"

    def __init__(self, registry: SkillRegistry, invoked_store: InvokedSkillStore):
        self.registry = registry
        self.invoked_store = invoked_store

    def validate_input(self, skill_name: str) -> str:
        trimmed = skill_name.strip()
        if not trimmed:
            raise ValueError("Invalid skill format")
        command_name = trimmed[1:] if trimmed.startswith("/") else trimmed
        command = self.registry.find(command_name)
        if command is None:
            raise ValueError(f"Unknown skill: {command_name}")
        if command.disable_model_invocation:
            raise ValueError(f"Skill {command_name} cannot be used by model")
        return command_name

    def check_permissions(
        self, command_name: str, args: str | None, context: ToolUseContext
    ) -> PermissionDecision:
        skill = self.registry.find(command_name)
        if skill is None:
            return PermissionDecision("deny", f"Unknown skill: {command_name}")

        permissions = context.app_state.get("skill_permissions", {})
        deny = set(permissions.get("deny", []))
        allow = set(permissions.get("allow", []))

        def matches(rule: str) -> bool:
            rule = rule[1:] if rule.startswith("/") else rule
            if rule == command_name:
                return True
            if rule.endswith(":*") and command_name.startswith(rule[:-2]):
                return True
            return False

        if any(matches(rule) for rule in deny):
            return PermissionDecision(
                "deny", "Skill execution blocked by permission rules"
            )
        if any(matches(rule) for rule in allow):
            return PermissionDecision(
                "allow", updated_input={"skill": command_name, "args": args}
            )
        if skill_has_only_safe_properties(skill):
            return PermissionDecision(
                "allow", updated_input={"skill": command_name, "args": args}
            )

        return PermissionDecision(
            "ask",
            f"Execute skill: {command_name}",
            updated_input={"skill": command_name, "args": args},
        )

    async def call(
        self, skill_name: str, args: str | None, context: ToolUseContext
    ) -> dict[str, Any]:
        command_name = self.validate_input(skill_name)
        skill = self.registry.find(command_name)
        assert skill is not None

        if skill.context == "fork":
            return await self._execute_forked(skill, args or "", context)

        blocks = await expand_skill_prompt(skill, args or "", context)
        content = "\n\n".join(
            block.get("text", "") for block in blocks if block.get("type") == "text"
        )

        skill_path = (
            str(skill.base_dir / "SKILL.md")
            if skill.base_dir
            else f"{skill.source}:{skill.name}"
        )
        self.invoked_store.add(skill.name, skill_path, content, context.agent_id)

        if skill.hooks:
            context.app_state.setdefault("pending_skill_hooks", []).append(
                {
                    "skill": skill.name,
                    "hooks": skill.hooks,
                    "skill_root": str(skill.base_dir) if skill.base_dir else None,
                }
            )

        # new_messages 等价于 Claude Code 的 meta user message。
        new_messages = [
            {"type": "user", "is_meta": True, "content": content},
            {
                "type": "attachment",
                "attachment_type": "command_permissions",
                "allowed_tools": skill.allowed_tools,
                "model": skill.model,
            },
        ]

        return {
            "success": True,
            "commandName": command_name,
            "status": "inline",
            "allowedTools": skill.allowed_tools or None,
            "model": skill.model,
            "effort": skill.effort,
            "newMessages": new_messages,
            "toolResult": f"Launching skill: {command_name}",
        }

    async def _execute_forked(
        self, skill: SkillCommand, args: str, context: ToolUseContext
    ) -> dict[str, Any]:
        agent_id = f"agent_{uuid.uuid4().hex}"
        blocks = await expand_skill_prompt(skill, args, context)
        prompt = "\n".join(block.get("text", "") for block in blocks)

        # 生产实现应调用你的 Agent runner，并传入：
        # - prompt messages
        # - skill.allowed_tools 注入后的权限上下文
        # - skill.model / skill.agent / skill.effort
        result_text = await run_sub_agent_placeholder(prompt, skill, context, agent_id)

        self.invoked_store.clear_for_agent(agent_id)
        return {
            "success": True,
            "commandName": skill.name,
            "status": "forked",
            "agentId": agent_id,
            "result": result_text,
            "toolResult": f'Skill "{skill.name}" completed (forked execution).\n\nResult:\n{result_text}',
        }


async def run_sub_agent_placeholder(
    prompt: str, skill: SkillCommand, context: ToolUseContext, agent_id: str
) -> str:
    return "Skill execution completed"
```

---

## 12. Hook 注册

Claude Code 的 `registerSkillHooks()` 会在 skill 被调用时，把 frontmatter 中的 hooks 注册成 session hooks。如果 hook 配置了 `once: true`，成功执行后自动移除。

Python 参考：

```python
# agent_skills/hooks.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


HOOK_EVENTS = ["PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"]


@dataclass
class SessionHook:
    event: str
    matcher: str
    hook: dict[str, Any]
    skill_name: str
    skill_root: str | None = None


class HookManager:
    def __init__(self) -> None:
        self.hooks: dict[str, list[SessionHook]] = {event: [] for event in HOOK_EVENTS}

    def register_skill_hooks(
        self,
        hooks_config: dict[str, Any],
        skill_name: str,
        skill_root: str | None = None,
    ) -> int:
        count = 0
        for event in HOOK_EVENTS:
            matchers = hooks_config.get(event)
            if not matchers:
                continue
            for matcher in matchers:
                matcher_text = matcher.get("matcher", "")
                for hook in matcher.get("hooks", []):
                    self.hooks[event].append(
                        SessionHook(event, matcher_text, hook, skill_name, skill_root)
                    )
                    count += 1
        return count

    def remove_once_hook_after_success(self, session_hook: SessionHook) -> None:
        if session_hook.hook.get("once"):
            self.hooks[session_hook.event] = [
                h for h in self.hooks[session_hook.event] if h is not session_hook
            ]
```

---

## 13. 已调用 Skill 保存与 compaction 恢复

`src/bootstrap/state.ts` 中保存：

- `skillName`
- `skillPath`
- `content`
- `invokedAt`
- `agentId`

这样 compact 后可把已经调用过的 Skill 内容恢复回上下文。`agentId` 防止不同子 Agent 的 skill 内容串线。

Python 参考：

```python
# agent_skills/state.py
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class InvokedSkillInfo:
    skill_name: str
    skill_path: str
    content: str
    invoked_at: float
    agent_id: str | None


class InvokedSkillStore:
    def __init__(self) -> None:
        self._items: dict[str, InvokedSkillInfo] = {}

    def add(
        self,
        skill_name: str,
        skill_path: str,
        content: str,
        agent_id: str | None = None,
    ) -> None:
        key = f"{agent_id or ''}:{skill_name}"
        self._items[key] = InvokedSkillInfo(
            skill_name, skill_path, content, time.time(), agent_id
        )

    def for_agent(self, agent_id: str | None) -> dict[str, InvokedSkillInfo]:
        return {k: v for k, v in self._items.items() if v.agent_id == agent_id}

    def clear(self, preserved_agent_ids: set[str] | None = None) -> None:
        if not preserved_agent_ids:
            self._items.clear()
            return
        for key, value in list(self._items.items()):
            if value.agent_id is None or value.agent_id not in preserved_agent_ids:
                self._items.pop(key, None)

    def clear_for_agent(self, agent_id: str) -> None:
        for key, value in list(self._items.items()):
            if value.agent_id == agent_id:
                self._items.pop(key, None)
```

---

## 14. Bundled Skills

Bundled skills 是程序内注册的内置技能。源码入口：

- `src/skills/bundledSkills.ts`
- `src/skills/bundled/index.ts`
- 示例：`src/skills/bundled/skillify.ts`、`src/skills/bundled/verify.ts`

特点：

1. 启动时调用 `registerBundledSkill()` 注册。
2. 可以携带 `files`，首次调用时解压到临时目录。
3. 解压目录使用进程级 nonce，目录 0700，文件 0600。
4. 禁止绝对路径与 `..` traversal。
5. 写入使用 exclusive create，Unix 下叠加 `O_NOFOLLOW`。

Python 参考：

```python
# agent_skills/bundled.py
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .models import SkillCommand, ToolUseContext


PromptFactory = Callable[[str, ToolUseContext], Awaitable[list[dict]]]


@dataclass
class BundledSkillDefinition:
    name: str
    description: str
    get_prompt: PromptFactory
    allowed_tools: list[str] = field(default_factory=list)
    when_to_use: str | None = None
    argument_hint: str | None = None
    files: dict[str, str] = field(default_factory=dict)
    user_invocable: bool = True
    disable_model_invocation: bool = False
    context: str = "inline"
    agent: str | None = None


class BundledSkillRegistry:
    def __init__(self, root: Path | None = None) -> None:
        nonce = secrets.token_hex(12)
        self.root = root or (
            Path(os.getenv("TMPDIR", "/tmp")) / f"agent-skills-{nonce}"
        )
        self.skills: dict[str, SkillCommand] = {}

    def register(self, definition: BundledSkillDefinition) -> SkillCommand:
        skill_root = self.root / definition.name if definition.files else None

        skill = SkillCommand(
            name=definition.name,
            description=definition.description,
            markdown_content="",
            source="bundled",
            loaded_from="bundled",
            base_dir=skill_root,
            allowed_tools=definition.allowed_tools,
            when_to_use=definition.when_to_use,
            argument_hint=definition.argument_hint,
            disable_model_invocation=definition.disable_model_invocation,
            user_invocable=definition.user_invocable,
            context="fork" if definition.context == "fork" else "inline",
            agent=definition.agent,
            has_user_specified_description=True,
        )
        self.skills[skill.name] = skill
        return skill

    def extract_files(self, skill_name: str, files: dict[str, str]) -> Path:
        base = self.root / skill_name
        base.mkdir(parents=True, mode=0o700, exist_ok=True)
        for rel, content in files.items():
            target = self._safe_target(base, rel)
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
        return base

    def _safe_target(self, base: Path, rel: str) -> Path:
        target = (base / rel).resolve()
        if not str(target).startswith(str(base.resolve()) + os.sep):
            raise ValueError(f"bundled skill file path escapes skill dir: {rel}")
        return target
```

---

## 15. MCP Skills

`src/skills/mcpSkills.ts` 会把 MCP server 暴露的 `skill://` resources 转成 `SkillCommand`。

流程：

```text
connected MCP client
  -> client.capabilities.resources 必须存在
  -> resources/list
  -> filter uri startsWith skill://
  -> resources/read(uri)
  -> 提取 text content
  -> parse frontmatter
  -> skill name = mcp__<server>__<rawName>
  -> createSkillCommand(source='mcp', loadedFrom='mcp')
```

Python 参考：

```python
# agent_skills/mcp.py
from __future__ import annotations

from typing import Any

from .loader import create_skill_command
from .models import SkillCommand


def normalize_name_for_mcp(name: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in name.lower()).strip("_")


async def fetch_mcp_skills_for_client(client: Any) -> list[SkillCommand]:
    if not getattr(client, "connected", False):
        return []
    if not getattr(client, "capabilities", {}).get("resources"):
        return []

    listed = await client.request({"method": "resources/list"})
    resources = listed.get("resources", [])
    skill_resources = [
        r for r in resources if str(r.get("uri", "")).startswith("skill://")
    ]

    commands: list[SkillCommand] = []
    for resource in skill_resources:
        uri = resource["uri"]
        try:
            read_result = await client.request(
                {"method": "resources/read", "params": {"uri": uri}}
            )
            text_parts = [
                c.get("text") for c in read_result.get("contents", []) if c.get("text")
            ]
            text = "\n".join(text_parts)
            if not text:
                continue
            raw_name = uri[len("skill://") :]
            skill_name = f"mcp__{normalize_name_for_mcp(client.name)}__{raw_name}"
            commands.append(
                create_skill_command(
                    skill_name=skill_name,
                    raw_markdown=text,
                    source="mcp",
                    loaded_from="mcp",
                    base_dir=None,
                )
            )
        except Exception:
            continue
    return commands
```

---

## 16. Skill 搜索：TF-IDF 实现

`DiscoverSkillsTool` 和自动预加载使用 `src/services/skillSearch/localSearch.ts`。

核心策略：

- tokenize 支持英文、数字、`-`、`_`，并支持 CJK bigram。
- 停用词过滤。
- 简单 stemming：`ing`、`tion`、`ness`、`ment`、`er`、`s`、`ed`、`ly` 等。
- 字段权重：
  - `name: 3.0`
  - `whenToUse: 2.0`
  - `description: 1.0`
  - `allowedTools: 0.3`
- 文档向量：weighted TF * IDF。
- 查询向量：TF * IDF。
- 相似度：cosine similarity。
- 名称精确包含提升到至少 `0.75`。
- 默认展示阈值：`0.10`。

Python 参考：

```python
# agent_skills/search.py
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable

from .models import SkillCommand


STOP_WORDS = {
    "a",
    "an",
    "the",
    "is",
    "are",
    "to",
    "of",
    "in",
    "for",
    "on",
    "with",
    "and",
    "or",
    "if",
    "this",
    "that",
    "use",
    "using",
    "used",
    "you",
    "your",
}
FIELD_WEIGHT = {
    "name": 3.0,
    "when_to_use": 2.0,
    "description": 1.0,
    "allowed_tools": 0.3,
}


def is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf"


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    lower = text.lower()
    i = 0
    while i < len(lower):
        if is_cjk(lower[i]):
            run = ""
            while i < len(lower) and is_cjk(lower[i]):
                run += lower[i]
                i += 1
            tokens.extend(run[j : j + 2] for j in range(max(0, len(run) - 1)))
        elif re.match(r"[a-z0-9]", lower[i]):
            word = ""
            while i < len(lower) and re.match(r"[a-z0-9\-_]", lower[i]):
                word += lower[i]
                i += 1
            cleaned = word.strip("-_")
            if cleaned and cleaned not in STOP_WORDS:
                tokens.append(cleaned)
        else:
            i += 1
    return tokens


def stem(word: str) -> str:
    if word and is_cjk(word[0]):
        return word
    for suffix, min_len, cut in [
        ("ing", 5, 3),
        ("tion", 5, 4),
        ("ness", 5, 4),
        ("ment", 5, 4),
        ("ers", 4, 1),
        ("er", 4, 2),
        ("es", 4, 2),
        ("ed", 4, 2),
        ("ly", 4, 2),
    ]:
        if word.endswith(suffix) and len(word) > min_len:
            return word[:-cut]
    if word.endswith("s") and len(word) > 3 and not word.endswith("ss"):
        return word[:-1]
    return word


def tokenize_and_stem(text: str) -> list[str]:
    return [stem(t) for t in tokenize(text)]


def compute_weighted_tf(fields: list[tuple[list[str], float]]) -> dict[str, float]:
    weighted: dict[str, float] = {}
    for tokens, weight in fields:
        freq: dict[str, int] = {}
        for token in tokens:
            freq[token] = freq.get(token, 0) + 1
        max_count = max(freq.values(), default=1)
        for term, count in freq.items():
            value = (count / max_count) * weight
            weighted[term] = max(weighted.get(term, 0.0), value)
    return weighted


def compute_idf(token_lists: Iterable[list[str]]) -> dict[str, float]:
    docs = list(token_lists)
    df: dict[str, int] = {}
    for tokens in docs:
        for term in set(tokens):
            df[term] = df.get(term, 0) + 1
    n = len(docs) or 1
    return {term: math.log(n / count) for term, count in df.items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    dot = sum(weight * b.get(term, 0.0) for term, weight in a.items())
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


@dataclass
class SkillIndexEntry:
    skill: SkillCommand
    normalized_name: str
    tokens: list[str]
    vector: dict[str, float]


@dataclass
class SearchResult:
    name: str
    description: str
    score: float
    skill: SkillCommand


class SkillSearchIndex:
    def __init__(self, skills: list[SkillCommand]):
        self.entries: list[SkillIndexEntry] = []
        raw_vectors: list[dict[str, float]] = []
        token_lists: list[list[str]] = []

        for skill in skills:
            name_tokens = tokenize_and_stem(skill.name)
            name_parts = [
                stem(p) for p in re.split(r"[-_]", skill.name.lower()) if len(p) >= 3
            ]
            desc_tokens = tokenize_and_stem(skill.description or "")
            when_tokens = tokenize_and_stem(skill.when_to_use or "")
            tool_tokens = tokenize_and_stem(" ".join(skill.allowed_tools))

            all_tokens = list(
                set(name_tokens + name_parts + desc_tokens + when_tokens + tool_tokens)
            )
            tf = compute_weighted_tf(
                [
                    (name_tokens + name_parts, FIELD_WEIGHT["name"]),
                    (when_tokens, FIELD_WEIGHT["when_to_use"]),
                    (desc_tokens, FIELD_WEIGHT["description"]),
                    (tool_tokens, FIELD_WEIGHT["allowed_tools"]),
                ]
            )
            token_lists.append(all_tokens)
            raw_vectors.append(tf)
            self.entries.append(
                SkillIndexEntry(
                    skill,
                    skill.name.lower().replace("-", " ").replace("_", " "),
                    all_tokens,
                    tf,
                )
            )

        idf = compute_idf(token_lists)
        for entry, vector in zip(self.entries, raw_vectors):
            entry.vector = {
                term: tf * idf.get(term, 0.0) for term, tf in vector.items()
            }
        self.idf = idf

    def search(
        self, query: str, limit: int = 5, min_score: float = 0.10
    ) -> list[SearchResult]:
        query_tokens = tokenize_and_stem(query)
        if not query_tokens:
            return []
        tf = compute_weighted_tf([(query_tokens, 1.0)])
        query_vec = {
            term: value * self.idf.get(term, 0.0) for term, value in tf.items()
        }
        query_lower = query.lower().replace("-", " ").replace("_", " ")

        results: list[SearchResult] = []
        for entry in self.entries:
            score = cosine(query_vec, entry.vector)
            if len(entry.skill.name) >= 4 and entry.normalized_name in query_lower:
                score = max(score, 0.75)
            if score >= min_score:
                results.append(
                    SearchResult(
                        entry.skill.name, entry.skill.description, score, entry.skill
                    )
                )
        return sorted(results, key=lambda r: r.score, reverse=True)[:limit]
```

---

## 17. DiscoverSkillsTool 与自动预加载

### 17.1 DiscoverSkillsTool

`DiscoverSkillsTool` 是只读、并发安全工具，输入：

```json
{
  "description": "deploy a Next.js app to Cloudflare Workers",
  "limit": 5
}
```

输出匹配 skill 名称、描述和 score。

Python 参考：

```python
class DiscoverSkillsTool:
    name = "DiscoverSkills"

    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    def call(self, description: str, limit: int = 5) -> dict:
        index = SkillSearchIndex(self.registry.get_skill_tool_commands())
        results = index.search(description, limit=limit)
        return {
            "count": len(results),
            "results": [
                {"name": r.name, "description": r.description, "score": r.score}
                for r in results
            ],
        }
```

### 17.2 自动预加载

`src/services/skillSearch/prefetch.ts` 做两类预加载：

1. `startSkillDiscoveryPrefetch()`：assistant turn 之间异步搜索。
2. `getTurnZeroSkillDiscovery()`：turn zero 根据用户输入同步搜索。

自动加载策略：

- `AUTO_LOAD_MIN_SCORE` 默认 `0.30`。
- `AUTO_LOAD_LIMIT` 默认 `2`。
- `AUTO_LOAD_MAX_CHARS` 默认 `12000`。
- 自动加载的 skill 内容会调用 `addInvokedSkill()`，让 compact 后保留。

Python 参考：

```python
AUTO_LOAD_MIN_SCORE = 0.30
AUTO_LOAD_LIMIT = 2
AUTO_LOAD_MAX_CHARS = 12_000


def extract_query_from_messages(input_text: str | None, messages: list[dict]) -> str:
    parts: list[str] = []
    if input_text:
        parts.append(input_text)
    for msg in reversed(messages):
        if msg.get("type") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            parts.append(content[:500])
            break
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text" and block.get("text"):
                    parts.append(block["text"][:500])
                    break
            break
    return " ".join(parts)


def build_skill_discovery_attachment(results: list[SearchResult]) -> dict:
    return {
        "type": "skill_discovery",
        "source": "native",
        "skills": [
            {"name": r.name, "description": r.description, "score": r.score}
            for r in results
        ],
    }
```

---

## 18. `/skills` UI 列表

`src/components/skills/SkillsMenu.tsx` 仅负责展示：

- 按来源分组：project、local、user、managed、plugin、MCP。
- 显示 scope tag：local/global/managed。
- 显示 frontmatter token 估算。
- 无 skill 时提示创建 `.claude/skills/` 或 `~/.claude/skills/`。

Python CLI/TUI 项目可以只实现一个文本列表：

```python
def render_skills_list(skills: list[SkillCommand]) -> str:
    groups: dict[str, list[SkillCommand]] = {}
    for skill in skills:
        groups.setdefault(skill.source, []).append(skill)

    lines: list[str] = []
    for source, items in groups.items():
        lines.append(f"## {source} skills")
        for skill in sorted(items, key=lambda s: s.name):
            desc = skill.when_to_use or skill.description
            lines.append(f"- /{skill.name}: {desc}")
        lines.append("")
    return "\n".join(lines)
```

---

## 19. 与 Agent 主循环集成

Python Agent 主循环可以按以下方式接入：

```python
async def handle_model_tool_use(
    tool_name: str, tool_input: dict, context: ToolUseContext
):
    if tool_name == "Skill":
        result = await skill_tool.call(
            tool_input["skill"],
            tool_input.get("args"),
            context,
        )
        # 关键：把 newMessages 插入对话，而不仅仅返回 toolResult。
        context.messages.extend(result.get("newMessages", []))
        return {
            "type": "tool_result",
            "content": result["toolResult"],
        }

    if tool_name == "DiscoverSkills":
        result = discover_skills_tool.call(
            tool_input["description"],
            tool_input.get("limit", 5),
        )
        return {"type": "tool_result", "content": result}
```

启动时：

```python
registry = SkillRegistry(cwd=Path.cwd(), user_home=Path.home())
registry.load_all()

invoked_store = InvokedSkillStore()
skill_tool = SkillTool(registry, invoked_store)
discover_skills_tool = DiscoverSkillsTool(registry)
```

文件工具执行后：

```python
discovery = DynamicSkillDiscovery(registry)


def after_file_operation(paths: list[Path]):
    dirs = discovery.discover_skill_dirs_for_paths(paths)
    discovery.add_skill_directories(dirs)
    discovery.activate_conditional_skills_for_paths(paths)
```

系统 prompt 中加入 skill guidance：

```text
When users ask you to perform tasks, check if any available skills match.
If a skill matches, call the Skill tool before answering.
Available skills:
- commit: Create a clean git commit...
- review-pr: Review a pull request...
```

---

## 20. 安全要点

复刻时建议保留以下安全边界：

1. **Skill 内容延迟注入**：不要在系统 prompt 中放完整 SKILL.md。
2. **未知字段默认不安全**：safe properties allowlist 必须是白名单，不是黑名单。
3. **MCP skills 禁止 shell injection**：远程内容不可执行内联 shell。
4. **bundled files 防 traversal**：拒绝绝对路径和 `..`。
5. **文件写入 exclusive create**：防止 symlink / race 攻击。
6. **动态 discovery 跳过 gitignored 路径**：避免 `node_modules` 等依赖注入 skill。
7. **权限规则 deny 优先**：即使 remote/bundled/safe 也必须先检查 deny。
8. **agentId 隔离 invoked skills**：防止子 Agent 的 skill 内容泄露到主 Agent 或其他 Agent。

---

## 21. 最小可用实现清单

如果只想在 Python 项目中先做 MVP，建议按顺序实现：

1. `SkillCommand` 数据结构。
2. `SKILL.md` frontmatter 解析。
3. `.claude/skills/<name>/SKILL.md` loader。
4. `SkillRegistry.get_skill_tool_commands()`。
5. `SkillTool.validate_input()`。
6. `SkillTool.call()` inline 展开，把 skill 内容插入 meta user message。
7. `allowed-tools` 注入权限上下文。
8. `InvokedSkillStore` 保存已调用 skill。
9. `DiscoverSkillsTool` + TF-IDF 搜索。
10. dynamic discovery / conditional `paths`。
11. fork execution。
12. bundled / MCP / hooks。

---

## 22. 与当前仓库实现的对应关系

| 能力 | TypeScript 文件 | Python 模块建议 |
|---|---|---|
| Skill 类型 | `src/types/command.ts` | `models.py` |
| frontmatter 解析 | `src/skills/loadSkillsDir.ts` | `frontmatter.py` |
| 文件加载 | `src/skills/loadSkillsDir.ts` | `loader.py` |
| 命令聚合 | `src/commands.ts` | `registry.py` |
| 模型调用 Skill | `SkillTool.ts` | `skill_tool.py` |
| Skill prompt 说明 | `SkillTool/prompt.ts` | system prompt builder |
| Slash command 展开 | `processSlashCommand.tsx` | `expand.py` + main loop |
| fork 子 Agent | `src/utils/forkedAgent.ts` | agent runner integration |
| hooks | `registerSkillHooks.ts` | `hooks.py` |
| 已调用保存 | `bootstrap/state.ts` | `state.py` |
| bundled skills | `bundledSkills.ts` | `bundled.py` |
| MCP skills | `mcpSkills.ts` | `mcp.py` |
| skill search | `skillSearch/localSearch.ts` | `search.py` |
| prefetch | `skillSearch/prefetch.ts` | prefetch service |
| `/skills` UI | `SkillsMenu.tsx` | CLI/TUI list |

---

## 23. 关键结论

Claude Code Skills 的核心价值在于：

- **让模型只在需要时加载大段专业知识**，节省上下文。
- **用 frontmatter 声明权限、触发条件、参数、执行模式**，让 Skill 可治理。
- **用 Tool 调用而不是普通 prompt 拼接**，从而接入验证、权限、审计、UI 和结果映射。
- **用 dynamic discovery 和 TF-IDF search**，解决大型项目中技能太多时的发现问题。
- **用 invoked skill state**，解决 compact 后 skill 指令丢失问题。

迁移到 Python 项目时，最重要的是保留这条主链路：

```text
SKILL.md -> SkillCommand -> SkillRegistry -> SkillTool -> meta user message -> Agent loop
```

只要这条链路稳定，再逐步补上 bundled、MCP、hooks、fork、prefetch 等增强能力即可。

---

*最后更新：2026-8-10 · 发现错误？[提 Issue](https://github.com/Sakura520222/Sakura-AI/issues)*