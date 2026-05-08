# PR 功能指南

本文档说明 Sakura AI Reviewer 中与 Pull Request 内容增强相关的功能，重点覆盖 PR 变更总结与 PR 依赖图。

## 功能概览

### PR 变更总结

启用 `enable_pr_summary` 后，系统会在审查流程中为 PR 生成变更摘要，并写入 PR 描述。PR 后续更新时，摘要会基于新的变更内容重新生成或增量更新，帮助维护者快速理解本次变更范围。

适合场景：

- PR 描述较短或缺少背景说明。
- 维护者希望在进入代码细节前先了解变更重点。
- 大型 PR 需要快速概览改动模块和影响面。

### PR 依赖图

启用 `enable_pr_dependency_graph` 后，系统会分析变更文件之间的 import / 模块依赖关系，并生成 Mermaid 图写入 PR 描述。依赖图内容由固定 marker 管理，重复运行时会替换旧图，而不是不断追加重复内容。

依赖图支持两种模式：

| 模式 | 配置值 | 说明 | 适合场景 |
| --- | --- | --- | --- |
| AI 模式 | `ai` | 调用模型分析变更文件与依赖关系，生成 Mermaid 图 | 需要语义解释、依赖关系较复杂或静态 import 不足以表达影响面的 PR |
| 静态模式 | `static` | 使用静态 import / module 解析生成 Mermaid 图，不额外调用模型 | 希望降低 AI 成本、提高输出稳定性，或以代码级 import 关系为主的 PR |

## 配置项

在 WebUI 配置管理中可配置以下选项：

| 配置项 | 默认/示例 | 说明 |
| --- | --- | --- |
| `enable_pr_summary` | `false` | 是否启用 PR 变更总结 |
| `enable_pr_dependency_graph` | `false` | 是否启用 PR 依赖图 |
| `pr_dependency_graph_mode` | `ai` | 依赖图生成模式，可选 `ai` 或 `static` |
| `pr_dependency_graph_max_nodes` | `25` | 依赖图最大节点数，避免图过大影响阅读 |
| `pr_dependency_graph_max_files` | `50` | 参与依赖分析的最大文件数 |

## 使用建议

- 如果主要目标是低成本和稳定输出，优先使用 `static` 模式。
- 如果 PR 涉及动态导入、框架约定、跨语言关系或需要更高层语义理解，可使用 `ai` 模式。
- 对大型 PR，适当降低 `pr_dependency_graph_max_nodes` 和 `pr_dependency_graph_max_files`，避免 PR 描述过长。
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

### 依赖图成本如何控制

- `static` 模式不额外调用模型，通常成本最低。
- `ai` 模式会产生模型调用成本，适合需要语义补充的 PR。
- 使用辅助模型配置可以降低摘要、依赖图等轻量任务成本。

## 实现参考

- `backend/services/ai_reviewer/pr_dependency_graph.py`：`PRDependencyGraphService` 负责依赖图生成与 PR body 更新。
- `backend/services/ai_reviewer/pr_summary.py`：`PRSummaryService` 负责 PR 变更总结相关逻辑。
- `backend/core/config.py`：定义 `pr_dependency_graph_mode` 等动态配置项。
