# Check 状态细化呈现设计

- 日期：2026-07-08
- 分支目标：`feature/check-status-enhancement`（从 `develop` 切出）
- 涉及模块：`backend/services/check_run_service.py`、`backend/workers/review_worker.py`、`backend/services/ai_reviewer/reviewer.py`、`backend/models/database.py`、`config/strategies.yaml`

## 1. 概述

### 1.1 目标

将 PR 审查的 GitHub Check Run 由当前的单一粗粒度 Check 升级为「主从式三 Check」，并在不污染主审查流程、不烧 API 配额的前提下，向 Checks 面板输出更细化的流程与运行时信息。

- 把现有 4 阶段（indexing / summary / reviewing / reporting）拆为 5 中间阶段，并以步骤清单形式呈现。
- 新增两个「按需出现」的副 Check：`Sakura AI - Analysis`（AI 运行时指标）、`Sakura AI - Findings`（发现统计）。
- 失败/取消时副 Check 不被追溯改写；findings 数与主 Review 同源。

### 1.2 非目标

- 不改造 `scan_worker`、`agent_team_worker` 的 Check 行为（本期只覆盖 `review_worker` + `webhook` 直接触发路径）。
- 不给标准模式 `review_pr` 增加 Analysis Check（标准模式无中间态可观察）。
- 不在 Check 中展示文件级 findings 明细（文件定位是 inline comment 的职责）。
- 不在本期扩展 WebUI 配置页（配置先落 `strategies.yaml` + `app_config`）。

## 2. 现状

`CheckRunService`（`backend/services/check_run_service.py`）定义 4 个粗阶段，由 `review_worker` 在审查主流程插桩：

| 阶段 key | output.title | output.summary |
| --- | --- | --- |
| indexing | Sakura AI 正在索引代码 | 正在索引代码变更 |
| summary | Sakura AI 正在生成总结 | 正在生成 PR 总结 |
| reviewing | Sakura AI 正在审查 | AI 审查进行中 |
| reporting | Sakura AI 正在生成报告 | 正在生成报告 |

终态：`completed / failed / cancelled / skipped`。output 文案为代码内中英双语常量，`output.text` 仅两行（当前阶段 + 已完成清单）。缓存按 `head_sha` 单维，无法区分同 SHA 的多次执行。

`review_worker` 通过 `self.check_run_service = CheckRunService()`（[review_worker.py:124](../../../backend/workers/review_worker.py#L124)）持有实例，在 7 处调用 `report_*`。

## 3. 架构总览

### 3.1 主从式三 Check

```
阶段: queued → fetching → indexing → summary → reviewing → reporting → completed
       ──────────────────────────────────────────────────────────────────────────
主 Review   |<—————————————— 全程，承载最终 conclusion ——————————————————————>|
Analysis    |                                          |<── 仅工具模式 ──>|
Findings    |                                                            |<── 有 findings ──>|
```

| Check 名（`name`） | 职责 | 生命周期 | 可否 required |
| --- | --- | --- | --- |
| `Sakura AI Review` | 流程步骤清单 + 最终决策 conclusion | 全生命周期 | 是（唯一建议） |
| `Sakura AI - Analysis` | AI 运行时指标（轮次/工具/Token/上下文/模型/耗时） | 进入 reviewing（仅工具模式）→ reporting 入口定格 | 否（不保证每次出现） |
| `Sakura AI - Findings` | 发现分级统计 + 发布状态 | reporting 阶段有 `publishable_findings` 时创建 → 评论发布后定格 | 否 |

> `name` 是 Checks 列表固定检查项名称（required status check 按它识别）；`output.title/summary/text` 是展开后的输出区域，随状态动态变化。副 Check 名带 ` - Analysis` / ` - Findings` 后缀，便于管理员识别其不应纳入 required。

### 3.2 实现路径：扩展现有 `CheckRunService`

三个 Check 生命周期强耦合（同 `ReviewRunKey`、同 `output_language`、同收敛时机），单类内可原子化收敛。`CheckRunService` 扩展为多 Check：

- `CHECK_RUN_NAME` 单常量 → 三个常量（`CHECK_RUN_NAME_REVIEW` / `_ANALYSIS` / `_FINDINGS`）。
- `_check_run_ids: dict[str, int]` → `dict[ReviewRunKey, dict[str, int]]`（键为执行上下文 + check_name）。
- `_find_or_create(...)` 增加 `check_name` 参数。
- 新增语义方法与批量收敛方法（见 §7）。

## 4. 数据模型

### 4.1 ReviewRunKey

```python
@dataclass(frozen=True)
class ReviewRunKey:
    repo_full_name: str
    pr_number: int
    head_sha: str
    review_job_id: str   # = PRReview.id（review_worker review_id）；标识本次审查执行
```

> 仅 `(head_sha, check_name)` 不足以区分同 SHA 多 PR、webhook 重投、手动重跑与任务重试，故引入执行上下文键。
>
> `review_job_id` 标识「本次审查执行」。跨执行的 Check Run 幂等（重投、重试时复用而非新建）由 §4.3 的 `head_sha + name + external_id` 恢复机制保证，**不依赖 `review_job_id` 在重投时是否变化**——即使重投新建了 `PRReview` 记录，仍能按 `head_sha + name` 列举到既有 active Check Run 并恢复。
>
> webhook 路径（如 PR 关闭取消审查）若无 `review_job_id`，不构造完整 `ReviewRunKey`，走 §7.2 `cancel_active_runs_by_sha` 兜底。

### 4.2 ReviewProgressSnapshot

```python
@dataclass(frozen=True)
class ReviewProgressSnapshot:
    current_round: int
    max_rounds: int              # = max_iterations（配置项），非实际轮次
    tool_call_count: int         # AI 调用工具次数累计，非 tracker.api_call_count
    total_input_tokens: int | None
    total_output_tokens: int | None
    current_context_tokens: int | None
    context_limit: int | None
    model_name: str | None
    elapsed_seconds: float | None   # finalize 时填；运行中由 CheckRunService 渲染当下耗时但不因此触发写入
```

### 4.3 持久化与恢复

扩展 `PRReview` 表（`backend/models/database.py:146`），新增字段：

| 字段 | 类型 | 用途 |
| --- | --- | --- |
| `review_check_run_id` | BigInteger, nullable | 主 Review Check Run id |
| `analysis_check_run_id` | BigInteger, nullable | Analysis Check Run id |
| `findings_check_run_id` | BigInteger, nullable | Findings Check Run id |
| `error_reference` | String(16), nullable | 脱敏故障编号（短 id） |
| `error_summary` | String(255), nullable | 脱敏错误摘要 |

恢复优先级：

1. **DB 优先**：从 `PRReview` 读取对应 `*_check_run_id`。
2. **external_id 兜底**：DB id 缺失时，`commit.get_check_runs()` 列举候选 → 客户端匹配 `external_id`（复用 `cleanup_stale_check_runs` 的列举模式，[github_app.py:1219](../../../backend/core/github_app.py#L1219)）。

`external_id` 编码（版本化、紧凑、可解析）：

```
sakura-ai:v1:{review_job_id}:{check_kind}
```

`check_kind` ∈ {`review`, `analysis`, `findings`}。三 Check 的 `external_id` 各不相同，避免客户端匹配出多个。GitHub API 不支持 `external_id` 服务端反查，远端扫描不作唯一事实来源（存在分页与历史清理风险）。

## 5. 状态机与收敛规则

### 5.1 三 Check 生命周期

- **主 Review**：全生命周期，承载最终 conclusion。
- **Analysis**：仅工具模式下，reviewing 入口创建并 `in_progress` → reporting 入口 `finalize_analysis(success)`。
- **Findings**：reporting 阶段且 `publishable_findings` 非空时创建并 `in_progress` → 评论发布成功后 `finalize_findings(neutral)`；发布失败 `finalize_findings(failure)`。

不提前创建副 Check，不显示 queued/等待状态。简单跳过、无 findings 的 PR 不出现副 Check。

### 5.2 收敛规则（核心：不追溯改写已 completed 的副 Check）

`finalize_review_run(review_run_key, conclusion, ...)` 只处理**本次执行已登记且仍处于 queued/in_progress** 的 Check；已经 completed 的副 Check 保持原终态。

| 失败/取消时机 | Analysis | Findings | 主 Review |
| --- | --- | --- | --- |
| reviewing 中失败/取消 | failure / cancelled | 不创建 | failure / cancelled |
| 进入 reporting 前 | 强制刷新最终快照 → success | — | — |
| reporting 中失败/取消 | 保持 completed | 若已创建：发布失败→failure；未发布→cancelled | failure / cancelled |
| reporting 后失败 | 保持 completed | 保持 completed（已发布）/ cancelled（取消） | failure / cancelled |

### 5.3 conclusion 语义

| Check | 正常 | 自身技术错误 | 取消 |
| --- | --- | --- | --- |
| 主 Review | approve→success；comment/request_changes→neutral | failure | cancelled |
| Analysis | success | failure | cancelled（保留最后快照） |
| Findings | neutral（无论 severity；不因 high severity 标 failure，避免双重失败源） | failure | cancelled |

发布失败 ≠ 审查失败：Findings 发布失败标 `failure`（output 注明「详见主 Review」），与主 Review 的决策 failure 语义不同源，不算双重失败源。双重失败源特指「Findings 因发现 high severity 而标 failure」，此情形禁止。

### 5.4 同源约束

主 Review 的「发现 N 条」= Findings 的「共 N 条」= 同一份 `publishable_findings`（校验/去重/过滤/分级后的可发布结果）。模型原始 findings 数不出现在任何用户可见 output。

## 6. output 模板

### 6.1 渲染规则

- 符号集：`✓` 已完成 · `►`（U+25BA，无 emoji 变体）进行中 · `○` 未执行 · `✗` 失败。
- 主 Review **始终渲染完整 5 步**；失败步显 `✗`，后续未执行步仍显 `○`。
- 运行时按 `output_language` **单一语言**渲染，不混合；本节中英双栏仅用于模板评审。
- severity（critical/major/minor/suggestion）是枚举值，**不进翻译表**，中英 output 都用英文标签。
- 主 Review text 分级行只列非零级；Findings text 列全四级含 0。
- 放弃等宽列对齐，统一「标签: 值」单空格分隔。
- 语言缺失回退英文。
- `cancel_reason` 为结构化枚举（`user_cancelled` / `superseded` / `pr_closed_merged` / `worker_cancelled` / `system_shutdown` / `unknown`），三 Check 复用同一规范化原因。
- 不可用指标字段直接省略该行，不显示 `0/0` 或 `unknown`。
- `error_reference` 同一次故障在主/副 Check 共享同一编号。

### 6.2 主 Check `Sakura AI Review`

**reviewing 中**

```
ZH                                          EN
title:   Sakura AI 正在审查                  title:   Sakura AI Reviewing
summary: 阶段 4/5 · AI 审查进行中             summary: Stage 4/5 · AI review in progress
text:                                       text:
✓ 1. 获取变更                               ✓ 1. Fetch changes
✓ 2. 索引代码                               ✓ 2. Index code
✓ 3. 生成总结                               ✓ 3. Generate summary
► 4. AI 审查                                ► 4. AI review
○ 5. 生成报告                               ○ 5. Generate report
```

**completed · 通过**

```
title:   Sakura AI 审查完成 · 通过  /  Sakura AI Review Completed · Approved
summary: 决策: 通过 · 评分: 9/10 · 发现: 5 条  /  Decision: Approve · Score: 9/10 · Findings: 5
text:
✓ 1..5 全部完成
决策: 通过
评分: 9/10
发现: 5 条（minor 3 · suggestion 2）
```

**completed · 请求修改**

```
title:   Sakura AI 审查完成 · 请求修改  /  ... · Changes Requested
summary: 决策: 请求修改 · 评分: 4/10 · 发现: 12 条
text: ✓ 1..5 全部完成
      决策: 请求修改
      评分: 4/10
      发现: 12 条（critical 1 · major 3 · minor 8）
```

**failed**（完整 5 步 + 故障编号）

```
title:   Sakura AI 审查失败  /  Sakura AI Review Failed
summary: 失败阶段: AI 审查 · 故障编号 8f3c2a17  /  Failed at: AI review · Ref 8f3c2a17
text:
✓ 1. 获取变更
✓ 2. 索引代码
✓ 3. 生成总结
✗ 4. AI 审查
○ 5. 生成报告
失败阶段: AI 审查
故障编号: 8f3c2a17
```

**cancelled**

```
title:   Sakura AI 审查已取消  /  Sakura AI Review Cancelled
summary: 审查任务已取消 · PR 已关闭或合并  /  Review cancelled · PR closed or merged
text: 步骤清单渲染至取消时进度（取消步显 ○，后续步显 ○）
```

### 6.3 副 Check `Sakura AI - Analysis`（仅工具模式）

**reviewing 中**

```
ZH                                          EN
title:   Sakura AI 工具分析中                title:   Sakura AI Tool Analysis In Progress
summary: 第 3/20 轮 · 工具调用 7 次 · 上下文 45%   summary: Round 3/20 · 7 tool calls · 45% context
text:                                       text:
当前轮次: 3（上限 20）                       Current round: 3 (max 20)
工具调用: 7 次                               Tool calls: 7
模型: gpt-4o                                Model: gpt-4o
累计 Token: 输入 12,345 / 输出 678           Total tokens: 12,345 input / 678 output
当前上下文: 45,234 / 100,000（45%）          Current context: 45,234 / 100,000 (45%)
```

**completed**

```
title:   Sakura AI 工具分析完成  /  Sakura AI Tool Analysis Completed
summary: 5 轮 · 工具调用 18 次  /  5 rounds · 18 tool calls
text:
实际轮次: 5（上限 20）  /  Rounds: 5 (max 20)
工具调用: 18 次         /  Tool calls: 18
模型: gpt-4o           /  Model: gpt-4o
累计 Token: 输入 34,567 / 输出 1,234   /  Total tokens: 34,567 input / 1,234 output
上下文峰值: 78,000 / 100,000（78%）   /  Peak context: 78,000 / 100,000 (78%)
分析耗时: 2 分 18 秒   /  Duration: 2m 18s
```

**failed**（保留 Token + 上下文，去自指）

```
title:   Sakura AI 工具分析失败  /  Sakura AI Tool Analysis Failed
summary: 第 3 轮出错 · 故障编号 8f3c2a17  /  Failed at round 3 · Ref 8f3c2a17
text:
当前轮次: 3（上限 20）
工具调用: 7 次
模型: gpt-4o
累计 Token: 输入 12,345 / 输出 678
当前上下文: 92,000 / 100,000（92%）
详见主 Review 获取错误详情  /  See main Review for error details
```

**cancelled**（保留最后快照，无自指「取消阶段」）

```
title:   Sakura AI 工具分析已取消  /  Sakura AI Tool Analysis Cancelled
summary: 已执行 3 轮 · 工具调用 7 次  /  3 rounds done · 7 tool calls
text:
实际轮次: 3（上限 20）
工具调用: 7 次
模型: gpt-4o
累计 Token: 输入 12,345 / 输出 678
取消时进度: 第 3 轮  /  Cancelled at round 3
```

### 6.4 副 Check `Sakura AI - Findings`（仅有 publishable findings 时）

**正常发布（neutral）**

```
title:   Sakura AI 发现统计  /  Sakura AI Findings Summary
summary: 9 条发现 · critical 1 · major 3 · minor 5  /  9 findings · 1 critical · 3 major · 5 minor
text:
共 9 条发现，涉及 4 个文件  /  9 findings across 4 files

critical: 1
major: 3
minor: 5
suggestion: 0
```

**部分发布失败（failure）**

```
title:   Sakura AI 发现发布失败  /  Sakura AI Findings Failed to Publish
summary: 9 条中已发布 6 条 · 3 条失败 · 详见主 Review
        /  6 of 9 published · 3 failed · See main Review
text:
发布状态:                /  Publishing status:
- 总计: 9                /  - Total: 9
- 已发布: 6              /  - Published: 6
- 失败: 3                /  - Failed: 3

全部发现分级:            /  Severity totals:
- critical: 1
- major: 3
- minor: 5
- suggestion: 0

详见主 Review 获取错误详情  /  See main Review for error details
```

**全部发布失败（failure）**

```
title:   Sakura AI 发现发布失败  /  Sakura AI Findings Failed to Publish
summary: 9 条发现均未能发布 · 详见主 Review  /  All 9 findings failed to publish · See main Review
text: 同部分失败结构，已发布=0
```

**cancelled**

```
title:   Sakura AI 发现已取消  /  Sakura AI Findings Cancelled
summary: 发布已取消，N 条待发布  /  Publish cancelled, N pending
```

## 7. CheckRunService 接口

### 7.1 改造现有方法

下列方法签名中的 `head_sha` 入参统一改为 `ReviewRunKey`（兼容包装：保留旧签名作 thin wrapper，内部构造 `ReviewRunKey` 调新实现，便于平滑迁移；迁移完成后移除 wrapper）：

- `report_queued` / `report_stage_progress`（原 `report_progress` 重命名）/ `report_completed`（title 带结论）/ `report_failed` / `report_cancelled`（接受 `cancel_reason`）/ `report_skipped`。
- 全部经 `_find_or_create(..., check_name=CHECK_RUN_NAME_REVIEW)` 定位主 Check。

### 7.2 新增方法

```python
async def report_analysis_snapshot(
    self,
    run_key: ReviewRunKey,
    snapshot: ReviewProgressSnapshot,
    *,
    output_language: str | None = None,
) -> None:
    """更新 Analysis Check（带节流：analysis_min_interval_sec）。
    离开 reviewing 前 worker 调 finalize_analysis(force_flush=True) 强制刷新一次。"""

async def report_findings_snapshot(
    self,
    run_key: ReviewRunKey,
    *,
    severity_counts: dict[str, int],   # {critical, major, minor, suggestion}
    files_count: int,
    total_count: int,
    published_count: int,
    failed_count: int,
    output_language: str | None = None,
) -> None:
    """更新 Findings Check。"""

async def finalize_analysis(
    self,
    run_key: ReviewRunKey,
    conclusion: str,                    # success / failure / cancelled
    *,
    snapshot: ReviewProgressSnapshot | None = None,
    output_language: str | None = None,
) -> None:
    """定格 Analysis；跳过已 completed 的。"""

async def finalize_findings(
    self,
    run_key: ReviewRunKey,
    conclusion: str,                    # neutral / failure / cancelled
    *,
    output_language: str | None = None,
) -> None:
    """定格 Findings；跳过已 completed 的。"""

async def finalize_review_run(
    self,
    run_key: ReviewRunKey,
    conclusion: str,
    *,
    failed_stage: str | None = None,
    cancel_reason: str | None = None,
    error_reference: str | None = None,
    completed_steps: list[str] | None = None,
    output_language: str | None = None,
) -> None:
    """主 Review 终态 + 同步收敛本次已登记且仍非 completed 的副 Check。
    error_reference 由本次失败统一传入，主/副 Check 共用同一编号。"""

async def cancel_active_runs_by_sha(
    self,
    repo_owner: str,
    repo_name: str,
    head_sha: str,
    *,
    cancel_reason: str = "unknown",
    output_language: str | None = None,
) -> None:
    """兜底：按 head_sha 列举本 App 所有 active（非 completed）Check Run，标 cancelled。
    用于 webhook PR-closed 等无 ReviewRunKey 的场景；不影响已 completed 的 Check。"""
```

### 7.3 节流实现

`report_analysis_snapshot` 内部按 `(run_key, ANALYSIS)` 维护 `last_update_ts`；`now - last < analysis_min_interval_sec` 且非强制时跳过远端写入（仅更新内存快照）。强制刷新（`finalize_analysis`、离开 reviewing 前）不受节流。

## 8. 数据来源（Analysis 快照）

核实结论：现有 `event_callback` 签名为 `async (event_type: str, data: dict) -> None`，调用点只传 `("message", msg_dict)` / `("tool_running", tool_call.id)`，**不含 tracker/token/context**（[reviewer.py:391-572](../../../backend/services/ai_reviewer/reviewer.py#L391-L572)）。因此采用方案 B：在 `_run_tool_loop` 现有 `tracker.log_context_usage(...)`（[reviewer.py:572](../../../backend/services/ai_reviewer/reviewer.py#L572)）之后追加一次 `"progress"` 事件。

`reviewer.py` 新增（纯增量，不改 `event_callback` 签名与现有调用点）：

```python
# 在 tracker.log_context_usage(current_tokens, safe_context, iteration) 之后
if event_callback:
    try:
        await event_callback("progress", {
            "iteration": iteration,
            "max_iterations": max_iterations,
            "token_usage": tracker.to_dict(),
            "current_tokens": current_tokens,
            "safe_context": safe_context,
            "model": settings.openai_model,
        })
    except Exception as exc:
        logger.warning("event_callback progress failed: {}", exc)
```

worker 的 `_review_event_callback`（[review_worker.py:939](../../../backend/workers/review_worker.py#L939)）增加 `elif event_type == "progress":` 分支：

- 累计 `tool_call_count`（来自本次与历史 `"message"` assistant+tool_calls 计数）。
- 构造 `ReviewProgressSnapshot`，调 `check_run_service.report_analysis_snapshot`。
- 异常仅记日志，不中断核心审查。

| 快照字段 | 来源 |
| --- | --- |
| current_round / max_rounds / token / context / model | `"progress"` 事件 data |
| tool_call_count | worker 在 `event_callback` 内累计 |
| elapsed_seconds | worker 记录 reviewing 进入时间戳，finalize 时算 |

## 9. error_reference 方案

- **生成**：worker 异常收敛路径，`uuid4().hex[:8]`，per-failure 一个；**不复用 task_id**（task_id 是任务标识，一次任务可多次失败/重试）。
- **共享**：传入 `finalize_review_run`，主 Review 与已登记副 Check 共用同一编号，避免用户误判为两个故障。
- **存储**：DB 存 `error_reference` + `error_summary`（脱敏摘要，便于检索）；loguru 日志带 `error_reference` tag 存完整堆栈（便于排查）。
- **展示**：Check output 仅显短编号与脱敏摘要，不暴露堆栈与敏感内容。

## 10. 配置项

与现有 `enable_check_runs` 同层——走 `Settings` + DB `app_config` 动态配置（**不放 `strategies.yaml`**，该文件只持工具策略如 `max_tool_iterations`，开关类配置须与 `enable_check_runs` 保持一致；对齐「禁止硬编码限制」记忆）：

- `enable_analysis_check`（bool，默认 true）—— 副 Analysis 总开关
- `enable_findings_check`（bool，默认 true）—— 副 Findings 总开关
- `analysis_min_interval_sec`（int，默认 3）—— Analysis 快照写入最小间隔

`max_rounds` 沿用现有 `max_tool_iterations`（`review_pr_with_tools`）/ `agent_team_reviewer_max_tool_rounds`，不新增。

## 11. i18n 策略

- 两套完整模板键（`checks.review.* / checks.analysis.* / checks.findings.*`），按 `output_language` 单语渲染，不混合，不逐句翻译。
- severity（critical/major/minor/suggestion）是枚举值，不进翻译表。
- 主 Review text 分级行只列非零级；Findings text 列全四级含 0。
- 语言缺失统一回退英文。
- cancelled 接受结构化 `cancel_reason`，三 Check 复用同一规范化原因。

## 12. worker 接触面

- **主 Check**：现有 4 插桩点保留并改传 `ReviewRunKey`；**新增 `fetching` 插桩点**（PR 元信息/diff 拉取后）；`check_run_stages` 列表相应调整为 5 阶段。
- **Analysis**：reviewing 入口（仅 `enable_tools=True`）建 Analysis；`_review_event_callback` 识别 `"progress"` 事件 → `report_analysis_snapshot`；reporting 入口 `finalize_analysis(success)`；失败/取消按 §5.2 分阶段收敛。
- **Findings**：reporting 阶段从 `publishable_findings` 计算 `severity_counts` + `files_count`，非空时创建；评论发布后据 `published_count` / `failed_count` 调 `finalize_findings(neutral|failure)`。
- **`AIReviewer`**：`_run_tool_loop` 加 `"progress"` 事件（§8）。
- **`professional_reviewer`**：不动（不在 Check Run 流程）。
- **标准模式 `review_pr`**：不建 Analysis。
- **`webhook`**：PR 关闭/合并等无 `review_job_id` 的取消场景，改调 `cancel_active_runs_by_sha` 兜底收敛（不再依赖 `ReviewRunKey`）；具体适配点在编码阶段按 webhook 现有调用逐一改造。
- **`CIFailureService`**：自身 Check 过滤由精确名 `Sakura AI Review` 扩展为全部三个 Sakura check name（或按 App slug/前缀），避免 `Sakura AI - Findings`（failure）被误记为外部 CI 失败（[ci_failure_service.py:71](../../../backend/services/ci_failure_service.py#L71) 现状只过滤精确名）。
- **`_make_and_submit_decision`**：返回结构化发布结果（`total` / `published` / `failed` / `fallback_mode`），作为 Findings Check 发布状态的数据来源；当前只返回 `(decision, reason)` 且吞掉 GitHub 发布失败（[review_worker.py:1647](../../../backend/workers/review_worker.py#L1647)）。
- **re-run**：本期**不支持** `check_run.rerequested`（[webhook.py:192](../../../backend/api/webhook.py#L192) 现状忽略非 completed action）；README 声明不支持，并加测试覆盖「忽略」行为。

## 13. 幂等与乱序

- `finalize_*` 幂等：重复调用跳过已 `completed` 的 Check。
- `"progress"` 快照按 `current_round` 单调递增；旧/重复快照丢弃，不回退指标。
- DB 写 `*_check_run_id`：仅在创建成功且字段为空时写（不覆盖非空）。
- 主/副 findings N 值同源 `publishable_findings`。

## 14. 测试策略

- `tests/test_check_run_service.py` 扩展：
  - 节流逻辑（最小间隔、强制刷新）。
  - `external_id` 编解码（三 check_kind 互异）。
  - 多 Check 收敛 + 不追溯改写已 completed。
  - `cancel_reason` 渲染、`error_reference` 共享。
- `tests/test_review_worker_check_run.py` 扩展：
  - `"progress"` 桥接 → Analysis 快照。
  - Analysis 生命周期（建/定格/失败收敛）。
  - Findings 部分成功 / 全失败 / cancelled。
- `tests/test_ai_reviewer_incremental_callback.py` 扩展：`"progress"` 事件断言。
- 现有 `test_github_app_check_run.py`：`external_id` 写入与列举恢复。

## 15. 范围边界

- **覆盖**：`review_worker` + `webhook` 直接触发路径。
- **不覆盖**：`scan_worker` / `agent_team_worker`。
- **README 增补**（项目规范「最后更新两个 README」）：
  - required check 约定：只将 `Sakura AI Review` 纳入 required；副 Check 名带后缀，不应 required。
  - 三 Check 命名与生命周期说明。

## 16. 接触面汇总

| 文件 | 改动 |
| --- | --- |
| `backend/services/check_run_service.py` | 常量 1→3；缓存键改 `(ReviewRunKey, check_name)`；`_find_or_create` 加 `check_name`；现有 `report_*` 改 `ReviewRunKey`；新增 `report_analysis_snapshot` / `report_findings_snapshot` / `finalize_analysis` / `finalize_findings` / `finalize_review_run`；节流；`external_id` 编码；DB 读写恢复 |
| `backend/workers/review_worker.py` | `ReviewRunKey` 构造；`fetching` 插桩点；Analysis 建/桥接/定格；Findings 建/定格；失败收敛传 `error_reference` + `cancel_reason`；5 阶段 `check_run_stages` |
| `backend/services/ai_reviewer/reviewer.py` | `_run_tool_loop` 加 `"progress"` 事件（约 10 行，纯增量） |
| `backend/models/database.py` | `PRReview` 扩展 5 字段 + 迁移 |
| `backend/core/config.py` | `Settings` 新增 3 字段 + DB 动态配置加载（与 `enable_check_runs` 同层） |
| `backend/core/github_app.py` | `create_check_run` / `update_check_run` 增加 `external_id` 参数；`_build_check_run_output` 保证 title+summary 同时存在（拒绝不完整 output） |
| `backend/services/ci_failure_service.py` | 自身 Check 过滤扩展为全部 Sakura check name，避免副 Check failure 被误记为外部 CI 失败 |
| `tests/test_check_run_service.py` / `test_review_worker_check_run.py` / `test_ai_reviewer_incremental_callback.py` | 扩展 |
| `backend/api/webhook.py` | 取消场景改调 `cancel_active_runs_by_sha` 兜底 |
| `README.md` / `README_EN.md`（或对应文档） | required check 约定 + 三 Check 说明 |

`professional_reviewer`、`review_pr`（标准模式）、`scan_worker`、`agent_team_worker` 零改动。

## 17. 实现要点（编码时落实）

- **external_id 读写**：`create_check_run` / `update_check_run` 当前未传 `external_id`（[github_app.py:1083](../../../backend/core/github_app.py#L1083) / `:1140`），需补参数；恢复走「列举候选 + 客户端匹配 external_id」，不当作唯一真值。
- **output 完整性**：`_build_check_run_output`（[github_app.py:1063](../../../backend/core/github_app.py#L1063)）允许部分 output，但 GitHub API 要求 title+summary 同时存在；wrapper 层保证完整性，并修复允许部分 output 的既有测试。
- **finalize 状态守卫**：`update_check_run`（[github_app.py:1140](../../../backend/core/github_app.py#L1140)）当前无条件 edit；finalize 前读 Check Run 状态或查 DB 终态标记，已 `completed` 则跳过远端写入（落实 §5.2 不追溯改写）。
- **cleanup conclusion**：`cleanup_stale_check_runs`（[github_app.py:1219](../../../backend/core/github_app.py#L1219)）当前把旧 active run 标 `neutral`；多 Check 下被取代的 run 应标 `cancelled` + `cancel_reason=superseded`，且不动已 `completed` 的子 Check。
- **PR 关闭直接取消**：closed-PR 路径直接调 `cancel_active_runs_by_sha`（不依赖 worker 存活），覆盖 worker 未启动/已退出/未到检查点的场景。
- **rate-limit 恢复**：create/update wrapper 当前只吞异常（[github_app.py:1133](../../../backend/core/github_app.py#L1133) / `:1184`）；加 403/429/secondary-rate-limit 有界退避，每个 Check 保证一次最终 best-effort flush。
- **持久化写活跃行**：check id / error_reference 写入活跃 `PRReview` 行；仅当无 `review_id` 时才走 `_save_error_record` 独立错误行（[review_worker.py:1241](../../../backend/workers/review_worker.py#L1241)）。
- **wrapper 迁移**：现有 `report_*` 只接收 owner/repo/head_sha，保留旧签名作 head-sha-only 单 Check 兼容层；`review_id` 创建后的调用点迁移到 `ReviewRunKey`，再启用三 Check 模式。
