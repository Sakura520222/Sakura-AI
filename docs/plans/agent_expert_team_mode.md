# Agent 专家团队模式实施计划

## 目标

Agent 专家团队模式是一个独立于 PR 审查、Issue 分析、仓库扫描的新系统。它由超级管理员在 WebUI 中手动启动，从已完成的 Issue 分析和仓库扫描发现中挑选高价值任务，使用专用 AI 配置驱动两个内置角色自动修改代码、创建 PR，并根据 Sakura AI Review 与人工审查反馈迭代。

## 当前阶段范围

- 仅超级管理员可访问和使用。
- 仅手动触发候选筛选与任务启动。
- 不开放自动定时执行。
- 不自动合并 PR。
- 不直接复用主 AI 配置；使用独立 Agent 专用 AI 配置。
- 两角色固定为：全栈专家、专业审查。

## 两角色职责

### 全栈专家（FullStackExpertAgent）

- 理解 Issue/Scan 任务背景。
- 制定实现方案。
- 在受控工作区内修改代码。
- 选择并运行允许的验证命令。
- 根据内部审查、Sakura AI Review、人类审查反馈继续修复。

### 专业审查（ProfessionalReviewAgent）

- 在 push 前审查全栈专家的计划和 diff。
- 识别安全、架构、可维护性、测试覆盖风险。
- 决定是否允许进入验证/推送阶段。
- 对外部审查反馈进行复核，避免无意义循环。

## 状态机

`candidate -> queued -> planning -> cloning -> editing -> self_reviewing -> validating -> pushing -> pr_opened -> external_reviewing -> iterating -> waiting_human -> completed | failed | cancelled | abandoned`

所有状态迁移必须集中校验，避免越权跳转。

## 与现有 PR 审查联动

Agent 创建 PR 后，仍通过现有 webhook 进入 Sakura PR 审查流程。现有 PR 审查作为外部质量门禁和反馈来源。Agent 子系统读取 `PRReview`、`ReviewComment`、GitHub review comments 后决定是否进入下一轮迭代。

## 安全边界

- 工作区隔离，所有文件工具限制在工作区内。
- 独立工作区固定为 `./workplace/<GitHub用户名>/<仓库名>/`，目录纳入 `.gitignore`。
- Shell 命令在仓库工作区内执行，环境继承项目部署后的 Python 虚拟环境。
- 测试命令只允许白名单。
- 默认禁止修改敏感路径，如密钥、部署配置、CI 配置。
- 限制最大修改文件数和 diff 行数。
- GitHub token 和 Agent 专用 API Key 不写日志、不回显明文。

## WebUI

- 独立页面：任务列表、候选筛选、任务详情、专用 AI 配置。
- 入口仅 `super_admin` 可见。
- 路由依赖必须使用 `require_super_admin`。
- 所有配置变更、启动、取消、重试动作写入管理员操作日志。

## 分阶段交付

1. 模型、配置、WebUI 入口与候选预览。
2. 两角色 dry-run：生成计划和 diff，不推送。
3. PR 创建与外部审查反馈闭环。
4. Sakura 记忆反思与完整迭代治理。