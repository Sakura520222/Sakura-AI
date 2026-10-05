# Agent 仓库规则与 Skills

本文说明 Issue #628 Phase 2 的仓库上下文行为。配置与凭据就绪后，`/agent` 继续自主执行；没有逐工具审批、额外审批 Agent 或运行中必须点击继续的授权步骤。文件、Shell、Git 和网络操作继续经过现有工具及 Sandbox，临时依赖出网复用 #604。

## 自主执行与循环自检

模型轮次、工具累计次数和任务步数不设新增预算。只有明确 `finish_task` 才能成功结束；用户取消、服务/API 真实错误或限流、外部进程终止以及无法安全恢复的系统错误按真实原因处理。不会按次数强制总结或切换便宜模型。

连续 10 次相同工具、相同规范化参数和相同结果，或连续相同纯文本，会触发一次带审计 metadata 的策略自检提示。任务继续执行；新操作、结果或真实用户指导重置检测。恢复保留已提示状态。检测只比较可观察的完全相同内容，不声称识别所有语义死循环，也不把它用作累计工作量限制。

## 仓库规则

Agent 将下列文件作为不可信仓库数据放入 `user` 上下文，不会将它们升级为 system 指令、授予工具或改变执行环境：

```text
CLAUDE.md                 # 可选兼容，优先级低于同目录 AGENTS.md
AGENTS.md
.sakura/AGENTS.md
.sakura/rules/*.md        # 按文件名排序
backend/AGENTS.md         # 只作用于 backend 及其子目录
backend/services/AGENTS.md
```

根级规则先加载，之后按目标目录由浅到深加载；同目录中 `AGENTS.md` 在 `CLAUDE.md` 后应用，更具体的规则只在自己的目录范围内覆盖通用规则。`.sakura/AGENTS.md` 和 `.sakura/rules` 是全仓库补充规则。

工具批次会依据其支持的单路径和路径数组收集规则。同一文件在一个快照中只保留一次；访问新目录时替换当前作用域快照，旧目录的规则不会无限追加。规则内容更新或删除后重新读取，恢复会话时也依据当前工作区刷新。历史 checkpoint 保持为执行记录；发送给模型的仓库上下文使用当前快照，避免历史规则再次混入压缩后的输入。

在执行尚未交付给模型的新作用域操作前，运行时会自动返回上下文并重试模型循环；这个步骤不需要管理员确认。Shell 无法仅凭任意命令字符串静态确定全部目标，因此扫描仓库中的适用规则。扫描跳过 VCS、依赖、缓存和构建目录；直接文件操作仍按其实际目标加载适用规则。

读取拒绝路径穿越、越界、符号链接、硬链接、特殊文件及敏感路径间接读取。读取失败或格式损坏时给出明确诊断，不把截断后的规则冒充完整规则。

## Skills 开关与来源

`agent_team_skills_enabled` 控制 DB/管理员安装 Skills 和仓库 Skills 的运行时发现、上下文展示与 `use_skill` 正文读取。关闭后，不得通过缓存、直接工具调用或恢复历史重新激活 Skill；独立的 Repository Instructions 仍然加载。

这个开关控制 Skill 子系统，不改变普通文件工具对工作区数据的访问，也不替代 Sandbox。普通文件读取得到的文本依旧是不可信数据，不会自动激活 Skill 或扩大权限。

仓库 Skills 自动发现以下目录中的元数据：

```text
.agents/skills/<slug>/SKILL.md
.sakura/skills/<slug>/SKILL.md
```

相同 slug 的优先级为：已启用的 DB Skill、`.sakura/skills`、`.agents/skills`。不同 Skill 的正文不会在启动时批量加载。

三种操作分开处理：

| 操作 | 读取内容 |
|---|---|
| 发现 | frontmatter 元数据，用于列出名称、用途和位置 |
| `use_skill(list_files=true)` | 目录项与所需元数据，不完整加载每个文件正文，也不激活工作流 |
| `use_skill(slug=..., file=...)` | 明确请求的正文或附件；省略 `file` 时读取 `SKILL.md` |

正文缓存不能绕过开关、文件安全校验或当前限制。文件内容更新后重新读取并失效旧结果，不能仅依据长度或时间戳认定内容没有变化。参数替换仍按既有 `$ARGUMENTS` / 命名参数规则处理，不把参数作为模板代码执行。

## 工作流限制与恢复

`allowed-tools` / `allowed_tools` 只能限制当前工作流使用现有工具，不能注册新工具、关闭 Sandbox 或授予网络能力。例如：

```yaml
---
name: inspect-source
description: Inspect code and Git status
allowed-tools:
  - Read
  - Shell(git status)
  - Bash(git diff:*)
---
Read the relevant files, inspect the diff, and report the evidence.
```

支持现有运行时工具名以及已有格式中的 `Read`、`Shell`、`Bash` 别名。`Shell(git status)` 表示一条精确的简单命令；`Bash(git status:*)` 和 `Bash(git *)` 表示命令 token 前缀。受约束选择器不接受串联命令、管道、重定向或命令替换，不能被降级为不受约束的 `run_command`。不支持或损坏的选择器明确报错，不会静默忽略。裸 `run_command` 保留既有 Shell 语义；选择器匹配不是操作系统隔离的替代品。

成功加载正文时记录实际生效的工作流限制。运行时将版本化的历史限制与工具结果一起原子持久化；恢复时和当前限制取交集。元数据放宽、删除或损坏不能让恢复自动扩大权限，工具正文中自称的“授权”也不能替代运行时记录。

工作流完成后，模型可自主调用 `use_skill`，传入相同 `slug` 和 `end_skill=true` 结束该工作流。这个操作不加载正文，只恢复原本已有的运行时访问范围；即使 Skills 已关闭，也保留必要的状态清理。历史限制丢失或损坏时不会猜测出更宽权限；现有工作流的安全结束不需要人工批准。

## 配置和读取行为

在全局配置页 `/config` 的 Agent 组使用既有 `agent_team_skills_enabled` 开关和 `agent_team_skills_root` 安装目录设置。配置是部署及功能选择，不是逐次工具授权。

按用户最新要求，此前新增的九项配置及其运行时限制已全部撤销：模型总轮数、工具总次数、只读并发数、连续重复工具轮次数，以及仓库单文件字节数、上下文总字节数、扫描项数、Skill 数量和元数据字节数。没有用隐藏计数或硬编码阈值替代；旧 `app_config` 行即使仍在数据库中也不再被这条执行链读取或在页面展示，不做破坏性数据库清理。

仓库规则使用当前作用域快照替换旧快照并去重；仓库 Skill 发现只读取元数据头，目录列表只枚举目录项，正文和附件按需读取。仓库内容不新增大小、数量限制，也不静默截断。

管理员安装的 DB Skills 沿用此任务前既有的 `MAX_SKILL_BYTES`：每文件 **512 KiB**，包括附件；原安装目录总量校验也保留。该既有契约与已删除的仓库默认 64 KiB 限制相互独立。

这些规则不限制真实用户指导的原文，不删除 checkpoint 中的原始审计记录，也不改变 Phase 1 的完成、取消和恢复协议。同进程、同事件循环中的工作区互斥已经验证；跨 worker 互斥保证仍待核实。

## English summary

Repository instructions remain untrusted user-level data. Root/global rules precede ancestor-to-descendant directory rules; a current, deduplicated scope snapshot replaces prior scopes and refreshes file changes, deletions and resume. No new repository byte, scan-entry, Skill-count or metadata-size caps remain.

`agent_team_skills_enabled` gates administrator and repository Skill discovery, model projection and explicit body access, including cache and restored-history paths. It does not disable independent `AGENTS.md` instructions or change ordinary sandboxed file access. Enabled DB Skills take precedence over `.sakura/skills`, then `.agents/skills` for identical slugs.

Discovery reads metadata, listings enumerate resources, and explicit `use_skill` calls read the requested body or attachment. DB Skills retain the existing 512 KiB per-file contract; the added repository 64 KiB cap has been removed. Unsupported selectors and violations of the existing DB Skill file contract are explicit errors. Runtime tool names and documented `Read`/`Shell`/`Bash` forms preserve exact or simple-command-prefix constraints without granting new tools or network access.

Runtime-owned historical workflow ceilings are checkpointed atomically and intersected with current restrictions on resume. Metadata changes cannot silently expand them. `end_skill=true` is an autonomous cleanup operation, never an approval request. No MCP, subagent, unified capability engine, hook or plugin-management expansion is part of this Phase 2 delivery.

The nine newly introduced runtime/configuration limits have been removed, including model/tool totals, parallel-read counts and repeated-tool-round thresholds. Persisted legacy values are ignored. Workspace exclusion, cancellation and explicit finish_task completion remain enforced.
