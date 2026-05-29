# 🧠 Sakura 项目记忆系统使用指南

## 概述

Sakura AI Reviewer 内置项目记忆系统，通过 `.sakura/` 目录实现自我反思和知识积累。系统会在每次 PR 审查和 Issue 分析后自动记录经验，随审查次数增加，AI 对你的项目理解越来越深，审查质量持续提升。

**核心能力：**
- 🔄 **自动反思**：每次审查后 AI 自主总结经验教训
- 📝 **知识合并**：定期将反思精炼为结构化的项目知识
- 🗂️ **周期性知识提取**：每累计指定数量的反思后，将可复用规则、架构知识和经验计划沉淀到分类文档
- 🧩 **上下文注入**：下次审查时自动加载项目知识，无需手动配置
- 📂 **自定义文档**：用户可在 `rules/`、`docs/`、`plans/` 目录放置项目文档，通过 RAG 按需检索
- 🖥️ **WebUI 管理**：超级管理员可查看、编辑、删除文件，并手动触发合并或知识提取

---

## 目录结构

`.sakura/` 目录位于仓库根目录下，结构如下：

```
.sakura/
├── SAKURA.md              # 项目概述（Sakura 自动维护）
├── memory.md              # 精炼记忆（Sakura 自动维护）
├── memory/                # 反思历史（Sakura 自动维护）
│   ├── 2026-04-20_PR187_cd02e2e.md
│   ├── 2026-04-21_PR188_a1b2c3d.md
│   └── ...
├── rules/                 # 👤/🤖 审查规则、编码规范
│   └── review-rules.md
├── docs/                  # 👤/🤖 架构文档、设计决策
│   ├── architecture.md
│   └── api-design.md
└── plans/                 # 👤/🤖 开发计划、路线图、经验沉淀
    └── roadmap.md
```

### 各文件/目录职责

| 路径 | 维护者 | 用途 | 注入方式 |
|------|--------|------|----------|
| `SAKURA.md` | 🤖 Sakura | 项目概述：技术栈、架构决策、已知问题、审查模式 | **直接注入**每次审查的 Prompt |
| `memory.md` | 🤖 Sakura | 精炼记忆：近期审查模式、常见问题、经验教训 | **直接注入**每次审查的 Prompt |
| `memory/` | 🤖 Sakura | 反思历史：每次审查后的深度反思记录 | 仅用于合并，不直接注入 |
| `rules/` | 👤 用户 / 🤖 Sakura | 审查规则、编码规范、团队约定 | **RAG 索引**，AI 按需检索 |
| `docs/` | 👤 用户 / 🤖 Sakura | 架构文档、设计决策、领域知识 | **RAG 索引**，AI 按需检索 |
| `plans/` | 👤 用户 / 🤖 Sakura | 路线图、功能计划、经验沉淀 | **RAG 索引**，AI 按需检索 |

> 🤖 = Sakura 自动生成和维护，你通常不需要手动编辑
> 👤 = 用户自定义内容，你可以自由添加和管理
>
> 初始化时默认会创建 `rules/`、`docs/`、`plans/` 的占位文档，可通过 `sakura_auto_create_subdirs` 关闭。知识提取 Agent 可能更新这些分类文档，但不会修改 `memory/` 目录下的反思原始记录。

---

## 三层知识积累机制

Sakura 记忆系统采用三层知识架构，从宏观到微观逐层细化：

### 第一层：项目概述（SAKURA.md）

- **定位**：宏观、稳定的项目知识
- **内容**：项目简介、技术栈、架构决策、已知问题列表、常见审查模式
- **字数限制**：默认 ≤ 5000 字符（可配置 `sakura_max_sakura_chars`）
- **更新频率**：每 N 次反思合并更新一次（默认 N=5）

### 第二层：精炼记忆（memory.md）

- **定位**：中观、精选的近期经验
- **内容**：近期审查中发现的高频模式、值得关注的规范建议、关键经验教训
- **字数限制**：默认 ≤ 2000 字符（可配置 `sakura_max_memory_chars`）
- **更新频率**：与 SAKURA.md 同步更新

### 第三层：反思历史（memory/*.md）

- **定位**：微观、详细的单次审查反思
- **内容**：覆盖度评估、代码模式发现、规范完善建议、具体经验教训
- **文件命名**：`{日期}_{类型}{编号}_{sha}.md`
  - PR 审查：`2026-04-20_PR187_cd02e2e.md`
  - 增量审查：`2026-04-20_PR187_incr2_cd02e2e.md`
  - Issue 分析：`2026-04-20_ISSUE42_a1b2c3d.md`
- **用途**：作为合并的输入源，不直接注入审查 Prompt

---

## 完整生命周期

### 1. 初始化（首次审查时自动触发）

当 Sakura 第一次审查你的仓库时（且 `sakura_auto_init` 开启），会自动初始化 `.sakura/` 目录：

1. 收集仓库信息：语言统计、README 内容（最多 3000 字）、目录结构
2. 调用 LLM 生成初始 `SAKURA.md`
3. 创建 `memory.md` 占位文件
4. 默认创建 `rules/`、`docs/`、`plans/` 子目录占位文档（由 `sakura_auto_create_subdirs` 控制）
5. 通过一次 Git 提交写入仓库（提交到默认分支）

提交信息示例：
```
chore: initialize .sakura/ directory for Sakura AI Reviewer
```

> 💡 如果自动初始化未触发（如 `sakura_auto_init=false`），系统会在首次反思时自动补全初始化。

### 2. 反思（每次审查后）

每次 PR 审查或 Issue 分析完成后，Sakura 会异步生成一篇反思：

1. 从审查评论中提取关键信息
2. 调用 LLM 对以下维度进行反思：
   - 审查质量评估（覆盖度、准确度、完整性）
   - 发现的代码模式和架构观察
   - 规范完善建议
   - 值得关注的经验教训
3. 将反思写入 `.sakura/memory/{日期}_{类型}{编号}_{sha}.md`
4. 累加反思计数

### 3. 合并（每 N 次反思触发）

当累计反思次数达到 `sakura_consolidation_interval`（默认 5）时：

1. 读取最近 N 篇反思文件
2. 启动工具调用驱动的 `SakuraConsolidationAgent`
3. 串行更新两个目标文件：
   - 📄 更新 `SAKURA.md`（基于反思 + 当前内容 + 仓库信息）
   - 📝 更新 `memory.md`（基于反思 + 当前内容）
4. 通过一次 Git 提交更新变更文件
5. 若满足知识提取条件，继续触发结构化知识提取

提交信息示例：
```
chore(sakura): consolidate memory (reflection #5)
```

合并 Agent 每个文件的最大工具调用轮数由 `sakura_consolidation_max_iterations` 控制。若开启 `sakura_consolidation_partial_commit`，单个目标文件生成失败时仍可提交其他成功生成的文件。

### 4. 知识提取（周期性触发）

当 `sakura_knowledge_extraction_enabled=true` 时，Sakura 会按照 `sakura_extraction_min_reflections`（默认 10）设定的间隔，周期性运行知识提取 Agent。2.12.0 起，知识提取不再是一次性开关，而是每当反思数量距上次提取达到间隔值时自动触发：

1. 浏览 `.sakura/` 目录结构
2. 读取 `memory/` 下的反思文件，提取可复用知识
3. 查看现有 `rules/`、`docs/`、`plans/` 分类文档
4. 创建新文件或精确更新已有文件
5. 将提取结果通过一次 Git 提交保存

知识分类约定：

| 目录 | 提取内容 |
|------|----------|
| `rules/` | 审查规则、编码规范、项目约定 |
| `docs/` | 架构文档、设计决策、技术栈信息 |
| `plans/` | 经验教训、常见问题模式、开发计划 |

提取 Agent 可以读取反思历史，但不允许写入 `memory/` 目录，避免覆盖原始审查记录。知识提取会按照间隔周期性自动执行；超级管理员也可以在 WebUI 中手动触发。

知识提取模型可通过 `sakura_extraction_provider` 选择凭据来源：

- `main`：使用主 AI 配置
- `summary`：使用辅助模型配置
- `custom`：使用 `sakura_extraction_api_base` 与 `sakura_extraction_api_key`

### 5. 注入（下次审查前）

每次 PR 审查开始时，Sakura 自动将项目知识注入审查 Prompt：

```
## 项目知识（来自 .sakura/ 目录）

### 项目概述
{SAKURA.md 的完整内容}

### 项目记忆
{memory.md 的完整内容}
```

AI 在完整的上下文中进行审查，从而提供更精准、更贴合项目特点的审查意见。

---

## 自定义文档目录

`.sakura/` 下的 `rules/`、`docs/`、`plans/` 三个目录用于放置分类知识文档。用户可以手动维护这些文档，Sakura 也可以通过知识提取 Agent 将反思中的稳定经验沉淀到这些目录。系统会自动扫描这些目录下的 Markdown 文件，建立 RAG 向量索引。

### 推荐目录用途

| 目录 | 推荐内容 | 示例文件 |
|------|----------|----------|
| `rules/` | 审查规则、编码规范、团队约定 | `review-rules.md`、`coding-standards.md`、`naming-conventions.md` |
| `docs/` | 架构设计、技术决策、API 规范 | `architecture.md`、`api-design.md`、`database-schema.md` |
| `plans/` | 开发计划、功能路线图、迁移方案 | `roadmap.md`、`migration-plan.md`、`feature-xyz-design.md` |

### RAG 索引行为

| 路径 | 是否被 RAG 索引 | 说明 |
|------|-----------------|------|
| `.sakura/rules/*.md` | ✅ 是 | AI 审查时可通过 `search_project_docs` 按需检索 |
| `.sakura/docs/*.md` | ✅ 是 | 同上 |
| `.sakura/plans/*.md` | ✅ 是 | 同上 |
| `.sakura/SAKURA.md` | ❌ 否（直接注入） | 每次审查自动加载，无需检索 |
| `.sakura/memory.md` | ❌ 否（直接注入） | 每次审查自动加载，无需检索 |
| `.sakura/memory/*.md` | ❌ 否 | 仅用于合并，不参与检索或注入 |

### 使用建议

1. **放置对 AI 审查有帮助的文档**：编码规范、架构设计、API 约定等
2. **文件使用 Markdown 格式**：`.md` 后缀才会被扫描
3. **文件内容精炼**：每份文档聚焦一个主题，避免过于冗长
4. **使用清晰的标题层级**：便于 RAG 分块和检索
5. **保持更新**：项目演进时及时更新文档内容

### WebUI 管理

超级管理员可在 **WebUI → Sakura 记忆管理** 中管理仓库记忆：

- 查看已初始化仓库的 `.sakura/` 文件列表
- 读取 `SAKURA.md`、`memory.md`、`memory/` 反思和分类文档内容
- 在线编辑并保存 `.sakura/` 下的文件
- 删除不再需要的文件
- 手动触发记忆合并
- 手动触发结构化知识提取

WebUI 保存文件会通过 GitHub 写入仓库，并记录管理员操作日志。

### 示例：审查规则文件

```markdown
# 审查规则

## 错误处理
- 所有对外部服务的调用必须包含错误处理和重试机制
- API 返回值必须检查错误状态

## 命名规范
- 变量使用 snake_case
- 类名使用 PascalCase
- 常量使用 UPPER_SNAKE_CASE

## 测试要求
- 新增功能必须包含单元测试
- Bug 修复必须包含回归测试
```

---

## AI 工具

Sakura 为 AI 提供了三个专用工具来访问 `.sakura/` 下的文档和反思历史：

### read_sakura_docs — 读取文档

- **功能**：读取 `.sakura/` 目录中的指导文档
- **参数**：
  - `doc_path`（可选）：文档路径，如 `rules/review-rules.md`
  - 留空则返回所有文档的概览（文件名、大小、内容预览）
- **用途**：AI 审查时可按需查询项目的编码规范、架构设计等

### list_sakura_directory — 浏览目录

- **功能**：列出 `.sakura/` 的目录结构和文档列表
- **参数**：
  - `subdirectory`（可选）：子目录路径，如 `rules`
  - 留空则列出根目录
- **用途**：AI 快速了解项目文档的组织方式

### read_sakura_memory — 读取反思历史

- **功能**：读取 `.sakura/memory/` 下的审查反思文件
- **参数**：
  - `file_name`（可选）：反思文件名，如 `2026-04-20_PR187_cd02e2e.md`
  - `count`（可选）：未指定文件名时列出最近 N 个反思文件，默认 5
- **用途**：AI 可参考历史审查经验，避免重复建议，并识别项目中的长期问题模式

---

## 配置参考

所有配置项均可在 **WebUI → 配置管理** 中调整（需要超级管理员权限）。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `sakura_memory_enabled` | bool | `true` | 启用/禁用整个记忆系统 |
| `sakura_reflection_enabled` | bool | `true` | 启用/禁用审查后反思 |
| `sakura_reflection_model` | string | `""` | 反思使用的模型（空值 = 使用审查模型） |
| `sakura_consolidation_interval` | int | `5` | 累计多少次反思后触发合并 |
| `sakura_consolidation_model` | string | `""` | 合并使用的模型（空值 = 使用审查模型） |
| `sakura_max_sakura_chars` | int | `5000` | SAKURA.md 最大字符数 |
| `sakura_max_memory_chars` | int | `2000` | memory.md 最大字符数 |
| `sakura_auto_init` | bool | `true` | 新仓库首次审查时自动初始化 `.sakura/` |
| `sakura_auto_create_subdirs` | bool | `true` | 初始化时自动创建 `rules/`、`docs/`、`plans/` 子目录占位文档 |
| `sakura_consolidation_partial_commit` | bool | `false` | 合并时单个文件失败后是否仍提交其他成功生成的文件 |
| `sakura_issue_reflection_enabled` | bool | `true` | 启用 Issue 分析后的反思 |
| `sakura_issue_reflection_model` | string | `""` | Issue 反思使用的模型 |
| `sakura_use_summary_model` | bool | `false` | 使用辅助（低成本）模型执行反思/合并任务 |
| `sakura_knowledge_extraction_enabled` | bool | `true` | 启用自动结构化知识提取 |
| `sakura_extraction_min_reflections` | int | `10` | 知识提取间隔，每积累指定轮数反思后自动触发一次提取 |
| `sakura_extraction_provider` | string | `"main"` | 知识提取 AI 凭据来源：`main` / `summary` / `custom` |
| `sakura_extraction_api_base` | string | `""` | `custom` 模式下的知识提取 API Base |
| `sakura_extraction_api_key` | string | `""` | `custom` 模式下的知识提取 API Key |
| `sakura_extraction_model` | string | `""` | 知识提取模型名称（空值 = 按 provider 推导） |
| `sakura_extraction_max_iterations` | int | `15` | 知识提取 Agent 最大工具调用轮数 |
| `sakura_consolidation_max_iterations` | int | `20` | 合并 Agent 每个目标文件的最大工具调用轮数 |

> 💡 **成本优化提示**：开启 `sakura_use_summary_model` 可使用更便宜的模型执行反思和合并任务，适合审查量大的场景。你也可以通过 `sakura_reflection_model` 和 `sakura_consolidation_model` 分别指定不同的模型。

---

## 常见问题

### Q：我可以手动编辑 SAKURA.md 或 memory.md 吗？

可以，但需要注意：Sakura 会在下次合并时更新这两个文件。如果你有想让 AI 始终记住的项目知识，建议放在 `rules/` 或 `docs/` 目录下；若启用了知识提取，Sakura 也可能在这些分类目录中创建或更新结构化知识文件。

### Q：反思文件会无限增长吗？

不会。`memory/` 目录下的反思文件会持续累积，但合并时只读取最近 N 篇（N = `sakura_consolidation_interval`）。你可以手动清理旧的反思文件，不影响系统运行。

### Q：初始化后仓库里没有 rules/ 等目录？

默认情况下，初始化会创建 `rules/`、`docs/`、`plans/` 子目录占位文档。如果关闭了 `sakura_auto_create_subdirs`，或仓库是在旧版本中初始化的，你可以直接在仓库中创建这些目录并放入 Markdown 文件，Sakura 会自动发现并索引。

### Q：Sakura 会自动修改 rules/docs/plans 里的用户文档吗？

知识提取 Agent 可能会创建或更新 `rules/`、`docs/`、`plans/` 下的 Markdown 文件，用于沉淀反思中的稳定知识。它不会写入 `memory/` 目录。如果你希望完全手动维护分类文档，可以关闭 `sakura_knowledge_extraction_enabled`，或在 WebUI 中审查提取结果后再调整。

### Q：如何禁用记忆系统？

在 WebUI 配置管理中关闭 `sakura_memory_enabled`，或设置 `sakura_reflection_enabled=false` 仅禁用反思。已存在的 `.sakura/` 目录不会被删除。

### Q：.sakura/ 目录占用了多少 Token？

- `SAKURA.md`（最多 5000 字符）和 `memory.md`（最多 2000 字符）会直接注入 Prompt，共占用最多约 7000 字符的上下文空间
- `rules/`、`docs/`、`plans/` 下的文档通过 RAG 按需检索，不会每次都占用 Prompt 空间
- 你可以通过 `sakura_max_sakura_chars` 和 `sakura_max_memory_chars` 调整字数上限

### Q：我可以使用其他目录名吗？

可以。`.sakura/` 下除 `SAKURA.md`、`memory.md`、`memory/` 之外的任何 `.md` 文件都会被 RAG 索引。但推荐使用 `rules/`、`docs/`、`plans/` 以获得 AI 工具的目录分类说明。

---

## 相关文档

- [项目记忆系统设计](plans/2026-04-20-sakura-memory-design.md) — 完整的架构设计文档
- [PR 功能指南](PR_FEATURES_GUIDE.md) — PR 审查相关配置
- [模型上下文管理](MODEL_CONTEXT_FEATURE.md) — AI 上下文窗口和压缩功能
