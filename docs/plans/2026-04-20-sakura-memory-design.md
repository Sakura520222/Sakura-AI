# Sakura AI Reviewer — .sakura/ 记忆系统架构设计

> 日期: 2026-04-20
> 状态: 已批准
> 关联 Issue: #102（增强 .sakura/ 目录功能）

---

## 1. 概述

将 Sakura AI Reviewer 从无状态审查系统升级为**具备自我反思和项目记忆能力**的智能审查系统。核心思路：每次 PR 审查后，AI 进行深度反思并将结果写回仓库的 `.sakura/` 目录；经过一定轮数的反思后，触发知识合并更新项目概述和记忆文件。这些文件在后续审查时被注入 prompt，形成"审查 → 反思 → 积累 → 改进"的闭环。

### 设计决策记录

| 决策项 | 选择 | 理由 |
|--------|------|------|
| 写回分支策略 | 直接提交到默认分支 | 避免 PR 爆炸，文件立即可用 |
| 反思触发时机 | 每次审查后 | 保持记忆及时性 |
| SAKURA.md 更新频率 | 每 5 次反思 | 平衡及时性和成本 |
| 反思深度 | 深度反思 | 更有价值的知识积累 |
| 仓库初始化 | 自动创建 | 降低使用门槛 |
| 写入模式 | 异步（不阻塞审查） | 审查速度不受影响 |
| 架构方案 | 独立服务 + 异步任务 | 职责分离，可独立重试 |
| RAG 索引范围 | 仅 rules/docs/plans/ | SAKURA.md 和 memory.md 直接注入 |

---

## 2. .sakura/ 目录结构

```
.sakura/
  SAKURA.md                          # 项目概述 + 知识积累
  memory.md                          # 当前记忆快照（精炼版）
  memory/                            # 反思记录（每次审查后追加）
    2026-04-20_PR187_cd02e2e.md      # 命名：日期_PR号_commit短sha
    2026-04-21_PR188_a1b2c3d.md
  rules/                             # 审查规则（RAG 索引）
  docs/                              # 架构文档（RAG 索引）
  plans/                             # 开发计划（RAG 索引）
```

### 三个核心文件的角色

| 文件 | 更新时机 | 注入时机 | 内容性质 |
|------|----------|----------|----------|
| `SAKURA.md` | 每 5 次反思 | 每次审查 | 项目概述、技术栈、架构决策、团队规范 |
| `memory.md` | 每 5 次反思 | 每次审查 | 精炼记忆：近期审查模式、常见问题、经验教训 |
| `memory/*.md` | 每次审查后 | 不注入 | 单次审查的深度反思（历史记录） |

### RAG 索引分离

- `rules/`、`docs/`、`plans/` 下的文件由现有 RAG 管道索引，支持 `search_project_docs` 工具按需检索
- `SAKURA.md` 和 `memory.md` 不走 RAG，直接注入 prompt
- `memory/` 目录下的反思文件既不注入也不索引
- `DocumentService.scan_sakura_directory()` 需更新，跳过 `SAKURA.md`、`memory.md` 和 `memory/` 目录

---

## 3. 增量审查处理

同一 PR 的增量提交（新 commit push 触发）：

- 每个 commit 生成独立的反思文件，用 commit SHA 区分
- 增量审查的反思携带历史上下文（该 PR 之前的反思摘要）
- 每次反思（包括增量）都计 1 次，累积 5 次触发合并
- 计数器按仓库维度维护

---

## 4. 数据层

### 新增模型：SakuraMemoryState

```python
class SakuraMemoryState(Base):
    __tablename__ = "sakura_memory_states"

    id: int                          # 主键
    repo_full_name: str              # 仓库名（唯一）

    # 状态跟踪
    reflection_count: int            # 累积反思次数
    last_consolidation_at: datetime  # 上次合并更新时间
    is_initialized: bool             # 是否已初始化 .sakura/ 目录

    # 最后写入的文件 SHA（用于增量更新）
    last_sakura_md_sha: str          # SAKURA.md 最新 commit SHA
    last_memory_md_sha: str          # memory.md 最新 commit SHA

    # 配置（可覆盖全局配置）
    consolidation_interval: int      # 触发合并的反思轮数（默认 5）

    created_at: datetime
    updated_at: datetime
```

---

## 5. 核心服务架构

### 5.1 组件职责

| 组件 | 职责 | 文件 |
|------|------|------|
| `SakuraMemoryService` | 反思生成、合并逻辑、计数管理、初始化 | `backend/services/sakura_memory_service.py`（新建） |
| `GitHubWriteService` | Git 提交、文件创建/更新 | `backend/services/github_write_service.py`（新建） |

### 5.2 GitHubWriteService 提交流程

使用 PyGithub Git Data API：

1. `repo.get_git_ref(f"heads/{default_branch}")` 获取最新 commit SHA
2. `repo.get_git_commit(latest_sha)` 获取 base tree
3. 构建 `InputGitTreeElement` 列表（新增/修改的文件）
4. `repo.create_git_tree(elements, base_tree)` 创建新 tree
5. `repo.create_git_commit(message, new_tree, [parent])` 创建 commit
6. `ref.edit(new_commit_sha)` 更新分支引用

### 5.3 服务交互流程

```
ReviewWorker（审查完成后）
  │
  ├── asyncio.create_task(reflect())
  │
  ▼
SakuraMemoryService.reflect(repo, pr, review_result)
  │
  ├── 1. 检查 is_initialized → 如否，先 initialize()
  ├── 2. 构建 Prompt → 调用 LLM → 生成深度反思
  ├── 3. GitHubWriteService.commit_files() → 写入 memory/*.md
  ├── 4. reflection_count += 1
  └── 5. if count % interval == 0 → consolidate()

SakuraMemoryService.consolidate(repo, state)
  │
  ├── 1. 读取最近 N 篇 memory/*.md
  ├── 2. 读取当前 SAKURA.md 和 memory.md
  ├── 3. 构建 Prompt → 调用 LLM → 生成新的 SAKURA.md + memory.md
  ├── 4. GitHubWriteService.commit_files() → 更新两个文件
  └── 5. 更新 SakuraMemoryState

SakuraMemoryService.initialize(repo)
  │
  ├── 1. 收集仓库信息（README、语言统计、目录结构）
  ├── 2. 调用 LLM 生成初始 SAKURA.md
  ├── 3. GitHubWriteService.commit_files() → 创建 .sakura/ 结构
  └── 4. 创建 SakuraMemoryState 记录
```

---

## 6. Prompt 设计

### 6.1 反思 Prompt（每次审查后）

**输入**：
- 审查结果摘要（PR 变更、AI 评论、评分、决策）
- 当前 `memory.md` 内容
- 仓库基本信息

**输出格式**：
```markdown
# 反思：PR#{number} @ {commit_sha}

## 审查质量评估
- 覆盖度、准确度、完整性分析

## 发现的模式
- 代码模式、架构观察

## 规范完善建议
- 建议新增或修改的审查规则

## 经验教训
- 值得记住的审查经验
```

### 6.2 合并 Prompt（每 5 次反思触发）

**输入**：
- 最近 5 篇反思文件全文
- 当前 SAKURA.md 和 memory.md
- 仓库基本信息

**SAKURA.md 更新策略**：
- 保留已有概述，更新架构决策和已知问题
- 新增反思中发现的重要模式
- 删除过时信息，最大 5000 字

**memory.md 更新策略**：
- 从反思中提取精炼记忆点
- 按类别组织：常见问题、审查模式、规范建议
- 最大 2000 字

---

## 7. 审查注入流程（每次审查开始时）

```
ReviewWorker.process_review_task()
  │
  ├── 1. 检查 .sakura/ 是否存在 → 无则触发 initialize()
  ├── 2. 通过 GitHub API 读取 SAKURA.md 和 memory.md
  ├── 3. 注入到 context["sakura_docs_context"]
  ├── 4. prompt_builder 渲染为：
  │     "## 项目知识（来自 .sakura/）
  │      ### 项目概述
  │      {SAKURA.md 内容}
  │      ### 项目记忆
  │      {memory.md 内容}"
  └── 5. 正常执行审查（流程不变）
```

---

## 8. 配置

### strategies.yaml 新增

```yaml
context_enhancement:
  sakura_memory:
    enabled: true
    reflection:
      enabled: true
      model: null                    # null = 使用审查相同模型
      prompt_template: null
    consolidation:
      interval: 5
      model: null
      max_memory_chars: 2000
      max_sakura_chars: 5000
      cleanup_old_reflections: false
    initialization:
      auto_init: true
      init_commit_message: "chore: initialize .sakura/ directory for Sakura AI Reviewer"
```

### GitHub App 权限升级

- `contents`: `read-only` → `write`
- 提交信息固定前缀 `chore(sakura):` 便于识别

### WebUI 配置（超级管理员）

所有新增配置项通过 `DYNAMIC_CONFIG_GROUPS` 注册到 WebUI，仅超级管理员可修改：

| 配置键 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `sakura_memory_enabled` | bool | true | 启用记忆系统 |
| `sakura_reflection_enabled` | bool | true | 启用审查后反思 |
| `sakura_reflection_model` | str | null | 反思模型（null=同审查模型） |
| `sakura_consolidation_interval` | int | 5 | 合并触发的反思轮数 |
| `sakura_consolidation_model` | str | null | 合并模型 |
| `sakura_max_memory_chars` | int | 2000 | memory.md 最大字符数 |
| `sakura_max_sakura_chars` | int | 5000 | SAKURA.md 最大字符数 |
| `sakura_auto_init` | bool | true | 自动初始化 .sakura/ |

在 `backend/core/config.py` 的 `Settings` 类新增对应字段，在 `DYNAMIC_CONFIG_GROUPS` 中注册 `sakura_memory` 组。

---

## 9. 安全与可靠性

- 写入操作集中在 `GitHubWriteService` 中管理
- 所有写入有详细日志
- 写入失败不阻塞审查（异步执行，异常隔离）
- 支持通过配置 `enabled: false` 完全关闭
- 提交使用 GitHub App 身份，作者标记为 bot

---

## 10. 关键文件清单

| 操作 | 文件 |
|------|------|
| 新建 | `backend/services/sakura_memory_service.py` |
| 新建 | `backend/services/github_write_service.py` |
| 修改 | `backend/models/database.py` — 新增 SakuraMemoryState |
| 修改 | `backend/workers/review_worker.py` — 注入 SAKURA.md/memory.md + 异步触发反思 |
| 修改 | `backend/services/document_service.py` — 扫描时跳过 memory/ 和 SAKURA.md/memory.md |
| 修改 | `backend/services/ai_reviewer/prompt_builder.py` — 渲染 sakura_docs_context |
| 修改 | `config/strategies.yaml` — 新增 sakura_memory 配置 |
| 修改 | `docker/mysql-init/init.sql` — 新增表 |
| 修改 | `README.md` / `README_EN.md` — 更新权限说明 |
