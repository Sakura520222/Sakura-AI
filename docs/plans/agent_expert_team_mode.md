# Agent 专家团队模式

Agent 专家团队模式用于把已发现的 Issue 或仓库扫描问题转化为受控的自动修复任务。超级管理员在 WebUI 中手动挑选候选任务，系统在隔离 Git 工作区内运行内置 Agent，完成代码修改、验证、审查和 PR 创建。

## 目标

- 从 Issue 分析、手动指定 Issue 和仓库扫描发现中筛选高价值任务。
- 使用双 Agent 协作完成计划、代码修改、内部审查和验证。
- 在受控工作区中执行文件工具和允许的 shell 命令。
- 创建普通或 Draft PR，将后续质量门禁交给 Sakura PR 审查和人工审查。
- 不自动合并 PR，不开放自动定时执行。

## 当前实现范围

- `super_admin` 可访问完整 Agent Team / Agent Skills 管理能力；普通登录用户可使用受限的任务创建、查看、重试、取消和反馈入口。
- 候选任务来自已完成的 Issue 分析、超级管理员手动指定的 GitHub Issue、普通用户手动指定的本人仓库 Issue 和仓库扫描发现。
- 手动 Issue 支持 `https://github.com/owner/repo/issues/123`、`github.com/owner/repo/issues/123` 和 `owner/repo#123` 等引用格式。
- Issue 评论支持 `/agent` 命令：仓库管理员或写权限协作者可将已分析 Issue 或 Sakura 仓库扫描报告 Issue 委派给 Agent Team，可附加 `base:<branch>` 指定基础分支。
- 支持 AI 候选筛选，帮助超级管理员优先选择高价值任务。
- 仅手动启动或评论命令触发任务，不提供定时或无人值守自动执行入口。
- 支持任务列表、任务详情、工作区管理、配置管理和运行状态展示。
- 支持复用主 AI 配置，也支持独立 Agent AI 配置。
- 支持受控文件工具、工作区内搜索、项目识别、diff 检查、文件回退、shell 黑名单安全策略验证命令和 Agent Skills。
- 支持 Agent 上下文压缩、会话 checkpoint 和任务恢复。
- 支持按配置自动安装项目依赖，并运行安全策略允许的验证命令。
- 支持创建 PR / Draft PR，但不会自动合并。
- 支持 AI 生成 Conventional Commits 风格 PR 标题，并在 GitHub 422 响应时重试创建 PR。

## AI 配置模式

Agent Team 通过 `agent_team_model_provider` 控制模型来源：

| 配置值 | 行为 |
| --- | --- |
| `main` | 复用主 AI 配置（主 API Base、API Key、模型等） |
| 其他 Agent provider | 使用独立 Agent AI 配置，例如 `agent_team_api_base`、`agent_team_api_key`、`agent_team_model` |

可按角色配置模型：

- `agent_team_model`：默认 Agent 模型。
- `agent_team_review_model`：专业审查 Agent 模型。
- `agent_team_summary_model`：摘要或轻量任务模型。

这种设计允许小团队直接复用主 AI，也允许生产环境为自动修复配置独立模型和配额。

## 上下文压缩与任务恢复

Agent Team 会记录任务会话与消息 checkpoint，用于任务恢复和问题排查：

- `ConversationCheckpointService` 持久化会话进度，并更新任务的 `last_checkpoint_at`。
- `agent_team_enable_context_compression` 开启后，系统在上下文接近阈值时压缩旧消息。
- `agent_team_context_compression_threshold` 控制压缩触发阈值。
- `agent_team_context_compression_keep_rounds` 控制压缩时保留最近多少轮完整对话。
- `agent_team_context_summary_max_tokens` 控制摘要长度。

上下文压缩只影响后续模型输入，不会删除已持久化的 checkpoint。任务异常中断后，可基于 checkpoint 继续分析、编辑或审查。

## 两个内置角色

### 全栈专家（FullStackExpertAgent）

- 理解 Issue / Scan 任务背景。
- 制定实现计划。
- 在受控工作区内修改代码。
- 使用允许的工具读取、搜索和编辑文件。
- 选择并运行安全策略允许的验证命令。
- 根据内部审查、Sakura PR Review 和人工审查反馈继续迭代。
- 可用工具轮数由 `agent_team_max_tool_rounds` 控制。

### 专业审查（ProfessionalReviewAgent）

- 在推送或创建 PR 前审查计划和 diff。
- 识别安全、架构、可维护性、测试覆盖和回归风险。
- 决定是否允许进入验证、推送或 PR 创建阶段。
- 对外部审查反馈进行复核，避免无意义循环。
- 可用工具轮数由 `agent_team_reviewer_max_tool_rounds` 控制。

## 工作流

典型任务流：

```text
候选发现/手动 Issue → 超级管理员筛选 → 启动任务 → 准备工作区 → 项目识别 → 计划 → 编辑 → diff 检查 → 内部审查 → 依赖安装/验证 → 推送分支 → 创建 PR → 外部审查/人工审查 → 迭代或完成
```

推荐状态机：

```text
candidate -> queued -> planning -> cloning -> editing -> self_reviewing -> validating -> pushing -> pr_opened -> external_reviewing -> iterating -> waiting_human -> completed | failed | cancelled | abandoned
```

所有状态迁移应集中校验，避免越权跳转。

## 权限与配额

Agent Team 的入口按用户角色分层：

- **超级管理员**：可访问候选筛选、工作区管理、配置管理、Skills 管理和所有任务运维入口。
- **管理员**：可使用登录态下的任务入口，并跳过普通用户仓库归属限制。
- **普通用户**：只能从本人 GitHub 用户名匹配的仓库创建或重试任务，且仓库必须匹配 `agent_team_repo_allowlist`；任务创建和重试会消耗 Agent 配额。
- **Issue 评论 `/agent`**：要求评论者在 GitHub 仓库中具有 `admin` 或 `write` 权限；系统按仓库所有者对应的已注册用户检查 Agent 配额，仓库所有者为管理员或超级管理员时跳过扣减。

普通用户入口先校验仓库权限，再扣减配额；如果 Issue 尚未完成 AI 分析且也不是 Sakura 扫描报告 Issue，`/agent` 会提示先执行 `/analyze`。

## 工作区与 Git 分支

- Agent Team 在 `agent_team_workspace_root` 下创建隔离工作区。
- 每个任务使用独立仓库目录和分支，分支前缀为 `sakura-agent`。
- 工作区准备过程包含 clone / fetch / checkout。
- 文件工具必须限制在工作区内，禁止越权访问服务运行目录或宿主机其他路径。
- PR 创建后，后续 Review 和人工反馈通过既有 GitHub / Sakura 流程处理。
- PR 标题可由 AI 根据任务、diff 和验证结果生成，默认遵循 Conventional Commits 风格。
- GitHub 返回 422 等可恢复错误时，PR 创建服务会调整参数并重试。

## 受控工具

Agent 可使用受控工具完成代码修改和验证：

- 文件读取、写入、精确替换、按行替换、插入和目录查看。
- `glob` 与 `search_in_files` 用于工作区内文件定位和内容搜索。
- `check_changes` 查看当前工作区累积 diff 和 Git 状态。
- `detect_project` 识别 Python、Node、Java 等项目类型、依赖文件和可用验证命令。
- `revert_file` 将指定文件回退到基线版本，便于撤销错误修改。
- `read_sakura_docs`、`list_sakura_directory`、`read_sakura_memory` 读取 Sakura 项目知识与历史反思。
- 黑名单安全策略下的 shell 命令验证，例如测试、lint 或项目允许的构建命令。
- `use_skill` 按需加载已启用 Agent Skill 的完整说明。
- `finish_task` 和 `submit_review` 用于结束实现或提交审查结论。

安全约束：

- 所有文件路径必须解析到工作区内。
- Shell 命令在工作区内执行。
- 验证命令受默认黑名单与 `agent_team_test_command_blocklist` 控制。
- 最大修改文件数和 diff 行数受护栏配置控制。
- GitHub token、AI API Key 等敏感信息不得写入日志或 PR 内容。

## 自动依赖与验证

当 `agent_team_auto_install_deps=true` 时，系统可根据项目识别结果自动运行受控依赖安装命令，例如 Python 的 `pip install -r requirements.txt` 或 Node 的包管理器安装命令。验证阶段仍遵循命令安全策略：

- `agent_team_run_tests` 控制是否运行验证命令。
- 默认黑名单与 `agent_team_test_command_blocklist` 控制额外拦截的高危命令。
- 项目识别结果可作为 Agent 选择验证命令的依据，但不能绕过命令安全策略。
- 内置 Ruff Skill 会提示 Agent 优先使用 `ruff check`、`ruff check --fix` 和 `ruff format` 处理 Python lint/format 问题。

## Agent Skills

Agent Skills 用于为 Agent 提供可复用的任务知识和操作指南。

当前支持：

- 内置 `ruff-lint` Skill，展示名为 `Ruff Lint & Format`。
- 上传单个 `SKILL.md`。
- 上传 ZIP 技能包。
- 从 GitHub blob/raw `SKILL.md` 安装。
- 在 WebUI 中启用、禁用和删除技能。
- Agent 通过工具按需读取技能完整内容。

Skills 只向 Agent 注入说明和操作流程，不会扩大 Agent 的工具权限。所有文件写入、shell 执行和 Git 操作仍受 Agent Team 的受控工具与命令安全策略约束。

相关配置：

| 配置项 | 说明 |
| --- | --- |
| `agent_team_skills_enabled` | 是否允许 Agent 使用 Skills |
| `agent_team_skills_root` | Skills 本地存储根目录 |

## WebUI

Agent Team WebUI 提供：

- 候选任务预览和筛选（管理员能力）。
- 通过 GitHub Issue 链接或 `owner/repo#123` 手动创建任务。
- 普通用户 Agent 配额展示、仓库权限校验和任务重试入口。
- AI 候选筛选入口（管理员能力）。
- 任务列表和任务详情。
- 工作区列表和清理入口。
- Agent Team 配置分组：基础、AI、上下文压缩、护栏、验证、Skills。
- 启动、取消、重试等管理动作。

Agent Skills WebUI 提供：

- Skill 上传。
- 从 GitHub 安装 Skill。
- 启用 / 禁用 Skill。
- 删除 Skill。

配置、候选筛选、工作区清理和 Skills 管理仍仅限 `super_admin`，普通用户只能访问受限任务入口；关键管理员动作应记录审计日志。

## 与现有 PR 审查联动

Agent 创建 PR 后，仍通过现有 webhook 进入 Sakura PR 审查流程。现有 PR 审查作为外部质量门禁和反馈来源。Agent 子系统可读取 `PRReview`、`ReviewComment` 和 GitHub review comments 后决定是否进入下一轮迭代。

该闭环的边界是：

- Agent 可以继续提交修复 commit。
- Agent 可以更新 PR。
- Agent 不会自动合并 PR。
- 是否接受和合并仍由维护者或仓库规则决定。

## 关键配置项

| 配置项 | 说明 |
| --- | --- |
| `agent_team_enabled` | 启用 Agent Team 功能 |
| `agent_team_workspace_root` | 隔离工作区根目录 |
| `agent_team_repo_allowlist` | 允许 Agent Team 操作的仓库列表；普通用户入口还要求仓库 owner 与当前 GitHub 用户名一致 |
| `agent_team_model_provider` | 模型配置来源：主 AI 或独立 Agent 配置 |
| `agent_team_max_tool_rounds` | 全栈专家最大工具调用轮数 |
| `agent_team_reviewer_max_tool_rounds` | 专业审查 Agent 最大工具调用轮数 |
| `agent_team_enable_context_compression` | 启用上下文压缩 |
| `agent_team_context_compression_threshold` | 上下文压缩触发阈值 |
| `agent_team_auto_install_deps` | 自动安装项目依赖 |
| `agent_team_run_tests` | 是否运行验证命令 |
| `agent_team_test_command_blocklist` | 额外拦截的 Shell 命令黑名单 |
| `agent_team_draft_pr` | 创建 Draft PR |
| `agent_team_skills_enabled` | 启用 Agent Skills |
| `agent_team_skills_root` | Skills 本地存储根目录 |

## 相关实现

- `backend/services/agent_team/`：Agent Team 核心服务。
- `backend/api/webhook.py`：Issue 评论 `/agent` 命令处理、权限检查、扫描报告 Issue 匹配与任务提交。
- `backend/services/agent_team/candidate_service.py`：候选任务收集、手动 Issue 任务创建与 AI 筛选。
- `backend/services/agent_team/git_workspace_service.py`：Git 工作区准备、分支与 PR 前置操作。
- `backend/services/agent_team/tools/`：工作区内受控工具。
- `backend/services/agent_team/tools/git_diff_tool.py`：`check_changes` 工具。
- `backend/services/agent_team/tools/project_detect_tool.py`：`detect_project` 工具。
- `backend/services/agent_team/tools/revert_file_tool.py`：`revert_file` 工具。
- `backend/services/agent_team/tools/sakura_docs_tool.py`：Sakura 文档目录工具。
- `backend/services/agent_team/tools/sakura_memory_tool.py`：Sakura 反思读取工具。
- `backend/services/agent_team/shell_executor.py`：受控 shell 执行。
- `backend/services/agent_team/skill_service.py`：Skills 安装、启停和索引。
- `backend/services/agent_team/builtin_skills.py`：内置 Agent Skills。
- `backend/services/agent_team/context_compressor.py`：上下文压缩。
- `backend/services/agent_team/conversation_checkpoint.py`：会话 checkpoint。
- `backend/services/agent_team/pr_service.py`：PR 标题生成与创建。
- `backend/models/agent_team_models.py`：Agent Team 数据模型。
- `backend/models/agent_skill_models.py`：Agent Skills 数据模型。
- `backend/webui/routes/agent_team.py`：Agent Team WebUI 路由。
- `backend/webui/routes/agent_skills.py`：Agent Skills WebUI 路由。

## 后续方向

- 更细粒度的任务优先级策略。
- 更完善的外部审查反馈归因。
- 更丰富的 Skill 元数据和评估机制。
- 与 Sakura 记忆反思系统的长期经验沉淀。
