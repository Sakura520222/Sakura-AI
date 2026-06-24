# GitHub Check Runs 集成设计：PR 审查进度可视化

- 日期: 2026-06-24
- 状态: Draft（待实现）
- 类型: feature
- 关联 Issue: Check Runs 集成（可能与 #85 相关，实现前需核实）

## 1. 背景与目标

### 1.1 现状

Sakura AI Reviewer 通过 webhook（`backend/api/webhook.py`）触发 PR 审查，由
`ReviewWorker.process_review_task` 异步处理。审查状态记录在数据库 `PRReview` 表的
`PRStatus` 枚举中（pending/reviewing/completed/failed/cancelled），审查进度通过
`_log_activity` 机制持久化并经 SSE 推送到 WebUI。

但审查状态完全没有映射到 GitHub 的 Checks 面板，用户只能通过 PR 评论/Telegram 通知
了解进度，无法在 PR 的 Checks 面板看到审查生命周期。

### 1.2 目标

将 PR 审查生命周期集成到 GitHub Check Runs，使审查状态（排队中/审查中/各阶段/完成/
失败/取消/跳过）在 PR 的 Checks 面板可视化展示。

### 1.3 非目标（本次不做）

- Check Run annotations（行级标注）
- WebUI 配置入口（后续 WebUI 迭代再加，本次仅支持 yaml/settings 配置）
- required check 门禁模式（conclusion 固定为纯展示语义）
- metrics/统计仪表盘

## 2. 关键发现（现有架构）

- `GitHubAppClient`（`backend/core/github_app.py`）：单例，`get_repo_client(owner, name)`
  返回 PyGithub `Github` 客户端（带重试）。目前无任何 Check Runs 方法。PyGithub 原生
  支持 `repo.create_check_run()` 与 `CheckRun.edit()`。
- `PRReview` 模型（`backend/models/database.py:129`）：已有 `head_sha`（nullable,
  indexed）、`overall_score`、`decision`、`review_summary`、`repo_owner`、`repo_name`、
  `pr_id` 字段。Check Run 所需的绑定与摘要信息均已具备。
- `PRStatus`（`backend/models/database.py:33`）：`pending / reviewing / completed /
  failed / cancelled`。
- `ReviewDecision`（`backend/models/database.py:43`）：`approve / request_changes /
  comment`。
- `_log_activity`（`backend/workers/review_worker.py:167`）：已有的审查活动事件机制
  （持久化 + SSE），event_type 含 thinking/status/tool_call/tool_result，提供宏观阶段
  语义可被 Check Run 进度复用。
- 输出语言机制：`output_language == "en"` 为英文，其余按中文。`CommentService` 用
  `_is_english(output_language)` 做内联中英双语分支（项目无统一 i18n 框架）。
  `get_user_dynamic_config("output_language", user_id)` 解析链为 UserConfig →
  AppConfig → Settings 默认值，带缓存。

## 3. 设计决策

### 3.1 conclusion 语义：纯展示模式

| PR 终态 | Check Run conclusion | 说明 |
|---|---|---|
| completed + approve | success | 审查通过 |
| completed + comment | neutral | 仅评论，不阻止合并 |
| completed + request_changes | neutral | 建议修改，但不阻止合并（AI 审查为建议性质，非 CI 门禁） |
| failed | failure | 审查自身出错（唯一红叉场景） |
| cancelled | cancelled | PR 关闭/合并导致中止 |

理由：AI 审查本质是建议而非强制门禁，与 CI 语义不同；避免误报卡住 PR；与现有
「评论 + Review」机制职责分离。

### 3.2 进度粒度：中粒度

在 queued、关键阶段切换、completed 三个层面更新 Check Run output，复用
`_log_activity` 已有的阶段语义。必经阶段：queued → reviewing → 终态。条件阶段：
indexing / summary / reporting（按对应功能开关启用才更新）。

### 3.3 跳过场景：仅为 Worker 内 should_skip 创建

Webhook 前置过滤的跳过（draft / bot 自身 / sakura-memory 分支 / 已合并 / 未注册 /
配额不足）不创建 Check Run。Worker 内 `analysis.should_skip` 创建 neutral Check Run
并写明原因。

### 3.4 架构：CheckRunService 封装

新建 `backend/services/check_run_service.py`，提供语义化方法，内部处理状态映射、
中英 output 格式化、find_or_create 定位、异常吞掉、配置开关判断。`GitHubAppClient`
新增底层薄封装方法。`ReviewWorker` 在生命周期节点调用。

### 3.5 Check Run 定位：head_sha + name 查询（无 DB 迁移）

Check Run `update` 需已知 id。策略为每次 update 前 `find_check_run_for_sha(head_sha,
name)` 查询找回。无需数据库迁移，对所有场景通用（含无 review_id 的 should_skip /
cancelled 早期场景），`CheckRunService` 保持无状态。

### 3.6 配置开关

`Settings` 新增 `enable_check_runs: bool = True`，支持 config yaml 覆盖（符合项目
「禁止硬编码、走 config」约定）。WebUI 入口本次不做。

### 3.7 输出语言与无 emoji

- Check Run 的 output 文本（title/summary/text）跟随用户 `output_language`，
  `CheckRunService` 复用 `_is_english()` 双语分支模式。
- 解决时序：`output_language = await get_user_dynamic_config("output_language",
  user_id)` 从 `review_worker.py:493` 提前到 `_create_review_record`（L304）之后立即
  获取，函数带缓存，提前调用零成本。
- output 文本一律纯文本，不使用 emoji。

## 4. 状态映射契约

| 触发时机 | PRStatus / 阶段 | Check Run status | conclusion | output 内容（跟随用户语言） |
|---|---|---|---|---|
| 任务提交 / 审查记录创建 | pending | queued | — | "审查已排队" |
| 进入 worker 处理 | reviewing 起始 | in_progress | — | 当前阶段 |
| 代码索引 | reviewing 进行中 | in_progress（更新 output） | — | "代码索引中"（仅启用时） |
| PR 总结 | reviewing 进行中 | in_progress（更新 output） | — | "PR 总结中"（仅启用时） |
| AI 审查 | reviewing 进行中 | in_progress（更新 output） | — | "AI 审查中" |
| 生成报告 | reviewing 进行中 | in_progress（更新 output） | — | "生成报告中" |
| 审查完成 + approve | completed | completed | success | 决策 + 评分 + 评论数 |
| 审查完成 + comment | completed | completed | neutral | 决策 + 评分 + 评论数 |
| 审查完成 + request_changes | completed | completed | neutral | 决策 + 评分 + 评论数 |
| 审查自身出错 | failed | completed | failure | 脱敏错误信息 |
| PR 关闭/合并取消 | cancelled | completed | cancelled | "PR 已关闭，审查取消" |
| Worker 内 should_skip | （无 PRReview） | completed | neutral | 跳过原因 |

Check Run 标识：`name = "Sakura AI Review"`（固定名），绑定 `head_sha`（每个 commit
独立一条 check，符合 GitHub 约定）。conclusion 一旦设置不可更改，映射须一次到位。

## 5. 组件设计

### 5.1 GitHubAppClient 新增底层方法

`backend/core/github_app.py` 新增三个薄封装方法（内部复用 `get_repo_client`）：

```python
def create_check_run(
    self, repo_owner: str, repo_name: str, name: str, head_sha: str,
    status: str = "queued", conclusion: str | None = None,
    output_title: str | None = None, output_summary: str | None = None,
    output_text: str | None = None,
) -> dict | None:
    """创建 Check Run，返回 {"id": int, ...} 或 None。失败返回 None 并记日志。"""

def update_check_run(
    self, repo_owner: str, repo_name: str, check_run_id: int,
    status: str | None = None, conclusion: str | None = None,
    output_title: str | None = None, output_summary: str | None = None,
    output_text: str | None = None,
) -> bool:
    """更新指定 Check Run。成功返回 True。"""

def find_check_run_for_sha(
    self, repo_owner: str, repo_name: str, head_sha: str, name: str,
) -> int | None:
    """按 head_sha + name 查找已存在的 Check Run ID，未找到返回 None。"""
```

PyGithub 调用要点：
- create：`repo.create_check_run(name=..., head_sha=..., status=..., conclusion=...,
  output={"title":..., "summary":..., "text":...})`，返回对象的 `id` 属性。
- update：`repo.get_check_run(check_run_id).edit(status=..., conclusion=..., output=...)`。
- find：`repo.get_check_runs_for_ref(ref=head_sha)` 过滤 `cr.name == name` 且
  `cr.app.id == 本 App id`，按 `started_at` 降序取首个 `id`。

### 5.2 CheckRunService

新建 `backend/services/check_run_service.py`，无状态，所有方法签名包含
`(repo_owner, repo_name, head_sha, *, ..., output_language=None)`：

```python
class CheckRunService:
    CHECK_RUN_NAME = "Sakura AI Review"

    async def report_queued(self, repo_owner, repo_name, head_sha, *,
                            pr_number, output_language=None) -> None
    async def report_progress(self, repo_owner, repo_name, head_sha, *,
                              stage, output_language=None) -> None
    async def report_completed(self, repo_owner, repo_name, head_sha, *,
                               decision, overall_score, comment_count,
                               summary_excerpt, output_language=None) -> None
    async def report_failed(self, repo_owner, repo_name, head_sha, *,
                            error_message, output_language=None) -> None
    async def report_cancelled(self, repo_owner, repo_name, head_sha, *,
                               output_language=None) -> None
    async def report_skipped(self, repo_owner, repo_name, head_sha, *,
                             reason, output_language=None) -> None
```

每个方法内部流程：
1. `if not get_settings().enable_check_runs: return`
2. 解析中英文本（`_is_english(output_language)`）
3. `find_or_create`：`report_queued` find→未命中则 create，命中则 update 为 queued
   （幂等，覆盖 worker 重试场景）；其余方法 find→命中则 update，未命中则记 debug
   跳过（`report_queued` 总是首个调用，后续方法调用时 check run 必然已存在）
4. 调 `GitHubAppClient` 对应方法，全部 `asyncio.to_thread` 包裹
5. 整体 `try/except Exception`，异常只 `logger.debug`，绝不向上抛出

`stage` 取值与中英映射：`indexing / summary / reviewing / reporting`（见第 6 节）。
`report_completed` 内部据 `decision` 映射 conclusion：`approve → success`，
`comment / request_changes → neutral`。

`report_completed` 参数来源：`decision` 取自 `PRReview.decision`，`overall_score`
取自 `PRReview.overall_score`，`comment_count` 取自
`review_result["inline_comments"]` 的长度，`summary_excerpt` 取自
`review_result["summary"]` 的截取片段。该调用须在 PRReview 写入 decision/score
**之后**。

### 5.3 配置项

`Settings` 类（`backend/core/config.py`）新增 `enable_check_runs: bool = True` 字段，
纳入动态配置解析链（与现有动态配置项一致），支持 config yaml 覆盖。

## 6. output 格式化

`CheckRunService` 内置中英双语常量表，纯文本，无 emoji。每个 `report_*` 产出
`(title, summary, text)`：

| 调用 | title（中 / 英） | summary（中 / 英） |
|---|---|---|
| report_queued | Sakura AI 审查已排队 / Review Queued | PR #N 已排队，等待处理 / PR #N queued |
| report_progress("indexing") | Sakura AI 正在审查 / Reviewing | 正在索引代码变更 / Indexing code changes |
| report_progress("summary") | 同上 | 正在生成 PR 总结 / Generating PR summary |
| report_progress("reviewing") | 同上 | AI 审查进行中 / AI review in progress |
| report_progress("reporting") | 同上 | 正在生成报告 / Generating report |
| report_completed (approve) | Sakura AI 审查完成 / Review Completed | 决策: 通过, 评分: N/10, 评论: M 条 / Decision: Approve, Score: N/10, Comments: M |
| report_completed (comment) | 同上 | 决策: 仅评论, 评分: N/10, 评论: M 条 / Decision: Comment, Score: N/10, Comments: M |
| report_completed (request_changes) | 同上 | 决策: 建议修改, 评分: N/10, 评论: M 条 / Decision: Request changes, Score: N/10, Comments: M |
| report_failed | Sakura AI 审查失败 / Review Failed | 审查过程出错（脱敏）/ Review errored (sanitized) |
| report_cancelled | Sakura AI 审查已取消 / Review Cancelled | PR 已关闭或合并 / PR closed or merged |
| report_skipped | Sakura AI 审查已跳过 / Review Skipped | {reason 中英映射} |

`text` 字段（最多 64KB markdown）：
- `report_completed`：写入 `summary_excerpt`（`review_summary` 的一段摘录）+ 指向 PR
  审查评论的指引。
- `report_progress`：当前阶段 + 已完成阶段清单，例如 reviewing 阶段：
  ```
  当前阶段: AI 审查进行中
  已完成: 代码索引, PR 总结
  ```
- 其余状态：一两句说明。

`report_skipped` 的 reason 中英映射表覆盖已知跳过原因（无代码变更 / PR 过大被策略
过滤等），未知 reason 回退到原始字符串。

## 7. 集成点与数据流

### 7.1 组件持有

`ReviewWorker.__init__` 新增 `self.check_run_service = CheckRunService()`。

### 7.2 生命周期集成点

| 语义位置 | 现有代码锚点 | CheckRunService 调用 |
|---|---|---|
| 审查记录创建后 | `_create_review_record` 之后、提前获取的 output_language 之后 | report_queued(pr_number) |
| 代码索引开始 | `settings.auto_index_pr_changes` 分支内 | report_progress("indexing")（仅启用时） |
| PR 总结/依赖图阶段 | `enable_pr_summary` 分支附近 | report_progress("summary")（仅启用时） |
| AI 审查开始 | `create_placeholder_comment` 之后、`review_pr_with_tools` 之前 | report_progress("reviewing") |
| 生成报告阶段 | AI 审查返回后、提交决策前 | report_progress("reporting") |
| 审查完成 | 设置 `PRStatus.COMPLETED` 处 | report_completed(decision, score, comment_count, summary_excerpt) |
| 审查出错 | 设置 `PRStatus.FAILED` 的 except 路径 | report_failed(error_message) |
| 取消（各 checkpoint） | `_cancel_and_cleanup` | report_cancelled |
| Worker 内跳过 | `_save_skip_record`（should_skip） | report_skipped(reason) |

实现第一步用 codegraph 精确定位锚点：`_update_review_status(…, PRStatus.COMPLETED)`、
`_update_review_status(…, PRStatus.FAILED)`、`review_pr_with_tools` 调用点、
`_save_skip_record`、`_make_and_submit_decision`。

### 7.3 webhook 层不直接调 CheckRunService

`closed` 取消复用现有取消架构：`closed → cancel_task()` 只设取消信号（不变）；worker
在下一个 `_check_cancelled` checkpoint 捕获 → `_cancel_and_cleanup` → 统一调
`report_cancelled`。webhook 层零改动。

边界：若 PR 在 queued 后、worker 进入 in_progress 前 closed，第一个 checkpoint
（`_check_cancelled` 在分析前）捕获 → `_cancel_and_cleanup`（此时已有 head_sha，
check run 已在 report_queued 创建）→ 正常 update 成 cancelled。

### 7.4 head_sha 来源

所有 worker 内调用点都在 `process_review_task` 作用域内，`pr_info["head_sha"]` 由
`extract_pr_info_from_webhook` 从 payload 的 `pull_request.head.sha` 提取，
opened/synchronize/reopened 均可靠提供。should_skip/cancelled 路径同在该作用域。

### 7.5 数据流（单次审查）

```
webhook(opened/sync) → submit_review_task → process_review_task
  output_language = get_user_dynamic_config(...)        ← 提前获取
  _create_review_record ──► [DB: PRReview PENDING]
  report_queued(owner, repo, head_sha, pr_number, lang)
      └─ find_or_create → GitHubAppClient.create_check_run(status=queued)
  (条件) report_progress("indexing" / "summary")
  report_progress("reviewing")
  ... AI 审查 ...
  report_progress("reporting")
  终态:
    COMPLETED  → report_completed(decision,...) → conclusion success/neutral
    FAILED     → report_failed(err)             → conclusion failure
    CANCELLED  → report_cancelled               → conclusion cancelled
    should_skip → report_skipped(reason)        → conclusion neutral
```

## 8. 错误处理

- 每个 `report_*` 方法整体 `try/except Exception`，异常只 `logger.debug`（Check Run
  是辅助，不算系统错误），绝不向上抛出。
- 所有 PyGithub 同步调用走 `asyncio.to_thread`，不阻塞事件循环。
- 方法入口首行判断 `enable_check_runs`，关闭时直接 return。
- `head_sha` 缺失或 `GitHubAppClient` 返回 None：静默跳过 + debug 日志。
- GitHub 限流：单次审查约 5-8 次 update，远低于限额；偶发限流直接吞掉。

## 9. 测试策略

| 层级 | 测试内容 | 方式 |
|---|---|---|
| CheckRunService 单测 | decision→conclusion 映射；find_or_create 幂等；中英 output 文本；异常被吞（mock raise 验证不抛）；enable_check_runs=False 跳过；output 无 emoji 断言 | mock GitHubAppClient |
| GitHubAppClient 单测 | create/update/find_check_run 正确调用 PyGithub；返回 None 容错 | mock PyGithub |
| ReviewWorker 集成 | 生命周期各节点按序调用对应 report_*；completed/failed/cancelled/skipped 分支正确 | mock CheckRunService，参照 tests/test_review_worker_timeout.py |
| 真实 API | 不做（无测试仓库） | — |

## 10. 实现顺序建议

1. `Settings.enable_check_runs` 配置项。
2. `GitHubAppClient` 三个底层方法 + 单测。
3. `CheckRunService` + 单测（含中英、映射、异常吞掉、无 emoji）。
4. `ReviewWorker` 集成（提前 output_language + 9 个调用点）+ 集成测试。
5. Ruff 检查 + 更新两个 README。

## 11. Gitflow 与提交

- 从 `develop` 创建 `feature/check-runs-integration` 分支，PR 目标为 `develop`。
- 不允许自主提交变更；spec 与实现均由用户提交。

## 12. 风险与备注

- **#85 重复核实**：实现前需核实 #85 是否已覆盖部分需求，避免重复工作。
- **权限前置**：用户已确认 GitHub App 已授予 `checks:write` 权限。
- **conclusion 不可逆**：映射须一次到位（已在状态契约表中明确）。
- **synchronize 增量审查**：每次 push 产生新 head_sha，会创建新 Check Run（GitHub
  标准约定）；旧的 head_sha 上的 check run 保持其最终状态，无需额外处理。
