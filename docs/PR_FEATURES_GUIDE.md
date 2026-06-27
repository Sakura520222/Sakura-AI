# PR 功能指南

本文档说明 Sakura AI Reviewer 中与 Pull Request 内容增强和审查稳定性相关的功能，覆盖 PR 变更总结、PR 依赖图、自动审查开关、大型 PR compact diff 以及 AI API 超时治理。

## 功能概览

### PR 变更总结

启用 `enable_pr_summary` 后，系统会在审查流程中为 PR 生成变更摘要，并写入 PR 描述。PR 后续更新时，摘要会基于新的变更内容重新生成或增量更新，帮助维护者快速理解本次变更范围。

适合场景：

- PR 描述较短或缺少背景说明。
- 维护者希望在进入代码细节前先了解变更重点。
- 大型 PR 需要快速概览改动模块和影响面。

### PR 自动审查开关

`enable_auto_review` 控制 GitHub PR webhook 是否在 `opened`、`synchronize`、`reopened` 等事件中自动创建审查任务。关闭后，系统不会自动入队新 PR 审查，但仍保留以下触发方式：

- PR 评论命令触发，例如 `/full-review`。
- WebUI 或管理入口手动触发。
- 其他显式调用审查服务的内部流程。

该开关适合在成本控制、灰度发布、只希望人工挑选 PR 审查时使用。

### PR 依赖图

启用 `enable_pr_dependency_graph` 后，系统会分析变更文件之间的 import / 模块依赖关系，并生成 Mermaid 图写入 PR 描述。依赖图内容由固定 marker 管理，重复运行时会替换旧图，而不是不断追加重复内容。

增量审查（PR 后续推送新提交触发）时，依赖图会基于上一轮的图叠加更新：图节点覆盖 PR 全量代码文件，本轮仅拉取变更文件内容解析新增依赖，历史依赖节点与边从上一轮图中保留并合并，避免为历史文件重复发起 GitHub API 调用。AI 模式与静态模式均支持该增量合并行为。

依赖图支持两种模式：

| 模式 | 配置值 | 说明 | 适合场景 |
| --- | --- | --- | --- |
| AI 模式 | `ai` | 调用模型分析变更文件与依赖关系，生成 Mermaid 图 | 需要语义解释、依赖关系较复杂或静态 import 不足以表达影响面的 PR |
| 静态模式 | `static` | 使用静态 import / module 解析生成 Mermaid 图，不额外调用模型 | 希望降低 AI 成本、提高输出稳定性，或以代码级 import 关系为主的 PR |

### 大型 PR compact diff

当初始 PR diff 接近模型安全上下文阈值时，审查器会自动切换到 compact diff 模式。该模式不会把完整 diff 一次性放入初始 prompt，而是提供：

- 变更文件列表。
- 文件状态与增删行统计。
- `list_changed_files()` 与 `get_file_diff(file_path)` 工具说明。

AI 会先基于文件概览制定审查重点，再按需读取具体文件 diff 或完整文件内容。这样可以避免大型 PR 初始 prompt 超限，并把 token 用在高风险文件和关键逻辑上。

compact diff 与上下文压缩配合使用：前者降低初始 prompt 大小，后者处理多轮工具调用后的历史膨胀。详细说明见 [模型上下文管理](MODEL_CONTEXT_FEATURE.md)。

### AI API 超时治理

PR 审查通常包含模型调用、工具调用、摘要生成、依赖图生成等多个步骤。为避免单次请求或重试循环无限等待，系统提供两类超时配置：

| 配置项 | 说明 |
| --- | --- |
| `ai_api_timeout_seconds` | 单次 AI HTTP 请求超时 |
| `ai_api_total_timeout_seconds` | 一次 AI 调用在重试循环中的总耗时上限 |

当模型服务响应慢、网络不稳定或大型 PR 触发长输出时，可适当调大总超时；当需要快速失败和释放队列资源时，可适当调小。

## 配置项

在 WebUI 配置管理中可配置以下选项：

| 配置项 | 默认/示例 | 说明 |
| --- | --- | --- |
| `enable_pr_summary` | `false` | 是否启用 PR 变更总结 |
| `enable_auto_review` | `true` | 是否让 PR webhook 自动触发审查任务 |
| `enable_pr_dependency_graph` | `false` | 是否启用 PR 依赖图 |
| `pr_dependency_graph_mode` | `ai` | 依赖图生成模式，可选 `ai` 或 `static` |
| `pr_dependency_graph_max_nodes` | `25` | 依赖图最大节点数，避免图过大影响阅读 |
| `pr_dependency_graph_max_files` | `50` | 参与依赖分析的最大文件数 |
| `model_context_window` | 自动/手动 | 模型上下文窗口，用于判断 compact diff 与上下文压缩阈值 |
| `context_safety_threshold` | 默认值 | 安全上下文比例，用于预留输出和工具调用空间 |
| `enable_context_compression` | `true` | 是否启用多轮审查历史压缩 |
| `context_compression_threshold` | 默认值 | 压缩触发阈值 |
| `ai_api_timeout_seconds` | 默认值 | 单次 AI 请求超时 |
| `ai_api_total_timeout_seconds` | 默认值 | 单次 AI 调用重试循环总超时 |

## 使用建议

- 如果主要目标是低成本和稳定输出，优先使用 `static` 模式。
- 如果 PR 涉及动态导入、框架约定、跨语言关系或需要更高层语义理解，可使用 `ai` 模式。
- 对大型 PR，适当降低 `pr_dependency_graph_max_nodes` 和 `pr_dependency_graph_max_files`，避免 PR 描述过长。
- 对大型 PR，保持 compact diff 和上下文压缩开启，避免初始 diff 或多轮工具调用占满上下文。
- 在成本敏感环境中，可关闭 `enable_auto_review`，改为人工选择重要 PR 审查。
- 如果模型服务偶发慢响应，可先调整 `ai_api_total_timeout_seconds`，避免重试循环过早终止。
- PR 变更总结和依赖图都会修改 PR 描述，建议避免手动删除 Sakura marker 块；如需移除功能，请在 WebUI 中关闭对应开关。

## 常见问题

### 开启后没有生成依赖图

可能原因：

- `enable_pr_dependency_graph` 未启用。
- 变更文件数量超过 `pr_dependency_graph_max_files` 后被截断，剩余文件没有可解析依赖。
- 静态模式下未识别到 import / module 关系。
- AI 模式下模型返回内容为空或不是有效 Mermaid 图。

### Mermaid 图没有显示

可能原因：

- 模型输出不是有效 Mermaid 语法。
- 依赖图节点过多或名称包含异常字符，系统校验后跳过注入。
- GitHub 页面渲染 Mermaid 时出现临时问题，可刷新页面或减少节点数。

### PR 没有自动开始审查

可能原因：

- `enable_auto_review` 已关闭。
- PR 事件不是系统监听的自动触发事件。
- 队列、配额或权限检查阻止了任务创建。
- GitHub App webhook 或安装权限异常。

可通过 PR 评论命令或 WebUI 手动触发一次，确认审查流程本身是否正常。

### 大型 PR 审查提示上下文不足

处理建议：

- 检查模型上下文窗口是否识别正确。
- 确认上下文压缩已启用。
- 使用上下文窗口更大的模型。
- 将极大 PR 拆分为多个较小 PR。

### 依赖图成本如何控制

- `static` 模式不额外调用模型，通常成本最低。
- `ai` 模式会产生模型调用成本，适合需要语义补充的 PR。
- 使用辅助模型配置可以降低摘要、依赖图等轻量任务成本。

## 实现参考

- `backend/services/ai_reviewer/pr_dependency_graph.py`：`PRDependencyGraphService` 负责依赖图生成与 PR body 更新。
- `backend/services/ai_reviewer/pr_summary.py`：`PRSummaryService` 负责 PR 变更总结相关逻辑。
- `backend/services/ai_reviewer/compact_diff.py`：大型 PR compact diff 判断、工具扩展与 diff 工具处理。
- `backend/services/ai_reviewer/prompt_builder.py`：普通 prompt 与 compact prompt 构建。
- `backend/services/ai_reviewer/api_client.py`：AI API 请求、重试与超时控制。
- `backend/api/webhook.py`：PR webhook 自动审查触发与 `enable_auto_review` 检查。
- `backend/core/config.py`：定义 PR 功能、上下文和超时相关动态配置项。
