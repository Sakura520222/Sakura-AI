# Agent 专家团队模式

Agent 专家团队模式用于把已发现的 Issue 或仓库扫描问题转化为受控的自动修复任务。超级管理员在 WebUI 中手动挑选候选任务，系统在隔离 Git 工作区内运行内置 Agent，完成代码修改、验证、审查和 PR 创建。

## 目标

- 从 Issue 分析和仓库扫描发现中筛选高价值任务。
- 使用双 Agent 协作完成计划、代码修改、内部审查和验证。
- 在受控工作区中执行文件工具和允许的 shell 命令。
- 创建普通或 Draft PR，将后续质量门禁交给 Sakura PR 审查和人工审查。
- 不自动合并 PR，不开放自动定时执行。

## 当前实现范围

- 仅 `super_admin` 可访问 Agent Team 和 Agent Skills WebUI。
- 候选任务来自已完成的 Issue 分析和仓库扫描发现。
- 支持 AI 候选筛选，帮助超级管理员优先选择高价值任务。
- 仅手动启动任务，不提供定时或无人值守自动执行入口。
- 支持任务列表、任务详情、工作区管理、配置管理和运行状态展示。
- 支持复用主 AI 配置，也支持独立 Agent AI 配置。
- 支持受控文件工具、工作区内搜索、shell 白名单验证命令和 Agent Skills。
- 支持创建 PR / Draft PR，但不会自动合并。

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

## 两个内置角色

### 全栈专家（FullStackExpertAgent）

- 理解 Issue / Scan 任务背景。
- 制定实现计划。
- 在受控工作区内修改代码。
- 使用允许的工具读取、搜索和编辑文件。
- 选择并运行白名单内的验证命令。
- 根据内部审查、Sakura PR Review 和人工审查反馈继续迭代。

### 专业审查（ProfessionalReviewAgent）

- 在推送或创建 PR 前审查计划和 diff。
- 识别安全、架构、可维护性、测试覆盖和回归风险。
- 决定是否允许进入验证、推送或 PR 创建阶段。
- 对外部审查反馈进行复核，避免无意义循环。

## 工作流

典型任务流：

```text
候选发现 → 超级管理员筛选 → 启动任务 → 准备工作区 → 计划 → 编辑 → 内部审查 → 验证 → 推送分支 → 创建 PR → 外部审查/人工审查 → 迭代或完成
```

推荐状态机：

```text
candidate -> queued -> planning -> cloning -> editing -> self_reviewing -> validating -> pushing -> pr_opened -> external_reviewing -> iterating -> waiting_human -> completed | failed | cancelled | abandoned
```

所有状态迁移应集中校验，避免越权跳转。

## 工作区与 Git 分支

- Agent Team 在 `agent_team_workspace_root` 下创建隔离工作区。
- 每个任务使用独立仓库目录和分支，分支前缀为 `sakura-agent`。
- 工作区准备过程包含 clone / fetch / checkout。
- 文件工具必须限制在工作区内，禁止越权访问服务运行目录或宿主机其他路径。
- PR 创建后，后续 Review 和人工反馈通过既有 GitHub / Sakura 流程处理。

## 受控工具

Agent 可使用受控工具完成代码修改和验证：

- 文件读取、写入、搜索、目录查看。
- 工作区内 diff / Git 状态检查。
- 白名单 shell 命令验证，例如测试、lint 或项目允许的构建命令。
- `use_skill` 按需加载已启用 Agent Skill 的完整说明。

安全约束：

- 所有文件路径必须解析到工作区内。
- Shell 命令在工作区内执行。
- 验证命令受 `agent_team_test_command_allowlist` 控制。
- 最大修改文件数和 diff 行数受护栏配置控制。
- GitHub token、AI API Key 等敏感信息不得写入日志或 PR 内容。

## Agent Skills

Agent Skills 用于为 Agent 提供可复用的任务知识和操作指南。

当前支持：

- 上传单个 `SKILL.md`。
- 上传 ZIP 技能包。
- 从 GitHub blob/raw `SKILL.md` 安装。
- 在 WebUI 中启用、禁用和删除技能。
- Agent 通过工具按需读取技能完整内容。

相关配置：

| 配置项 | 说明 |
| --- | --- |
| `agent_team_skills_enabled` | 是否允许 Agent 使用 Skills |
| `agent_team_skills_root` | Skills 本地存储根目录 |

## WebUI

Agent Team WebUI 提供：

- 候选任务预览和筛选。
- AI 候选筛选入口。
- 任务列表和任务详情。
- 工作区列表和清理入口。
- Agent Team 配置分组：基础、AI、护栏、Skills。
- 启动、取消、重试等管理动作。

Agent Skills WebUI 提供：

- Skill 上传。
- 从 GitHub 安装 Skill。
- 启用 / 禁用 Skill。
- 删除 Skill。

所有入口仅 `super_admin` 可访问，并应记录关键管理员动作。

## 与现有 PR 审查联动

Agent 创建 PR 后，仍通过现有 webhook 进入 Sakura PR 审查流程。现有 PR 审查作为外部质量门禁和反馈来源。Agent 子系统可读取 `PRReview`、`ReviewComment` 和 GitHub review comments 后决定是否进入下一轮迭代。

该闭环的边界是：

- Agent 可以继续提交修复 commit。
- Agent 可以更新 PR。
- Agent 不会自动合并 PR。
- 是否接受和合并仍由维护者或仓库规则决定。

## 相关实现

- `backend/services/agent_team/`：Agent Team 核心服务。
- `backend/services/agent_team/candidate_service.py`：候选任务收集与 AI 筛选。
- `backend/services/agent_team/git_workspace_service.py`：Git 工作区准备、分支与 PR 前置操作。
- `backend/services/agent_team/file_tools.py`：工作区内文件工具。
- `backend/services/agent_team/shell_executor.py`：受控 shell 执行。
- `backend/services/agent_team/skill_service.py`：Skills 安装、启停和索引。
- `backend/services/agent_team/pr_service.py`：PR 创建。
- `backend/models/agent_team_models.py`：Agent Team 数据模型。
- `backend/models/agent_skill_models.py`：Agent Skills 数据模型。
- `backend/webui/routes/agent_team.py`：Agent Team WebUI 路由。
- `backend/webui/routes/agent_skills.py`：Agent Skills WebUI 路由。

## 后续方向

- 更细粒度的任务优先级策略。
- 更完善的外部审查反馈归因。
- 更丰富的 Skill 元数据和评估机制。
- 与 Sakura 记忆反思系统的长期经验沉淀。
