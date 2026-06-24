# GitHub Check Runs 集成 实现计划

> **执行方式：** 本会话 inline 执行（用户 /goal 指令：直接在 develop 分支完整实现，不建 worktree、不提交）。

**Goal:** 将 PR 审查生命周期映射到 GitHub Check Runs，在 Checks 面板可视化审查进度。

**Architecture:** `GitHubAppClient` 新增 3 个 Check Run 底层方法；新建 `CheckRunService`（无状态，封装状态映射/中英 output/find_or_create/异常吞掉/配置开关）；`ReviewWorker` 在生命周期 9 个节点调用，`output_language` 提前获取。

**Tech Stack:** Python 3.11+ / PyGithub / asyncio / pytest / Ruff

**依据 spec:** `docs/superpowers/specs/2026-06-24-check-runs-integration-design.md`

---

## 文件结构

| 动作 | 文件 | 职责 |
|---|---|---|
| Modify | `backend/core/config.py` | 新增 `enable_check_runs` 字段 + 注册 BASIC_CONFIG_KEYS |
| Modify | `backend/core/github_app.py` | 新增 create/update/find_check_run 底层方法 |
| Create | `backend/services/check_run_service.py` | CheckRunService（状态映射、中英 output、find_or_create、异常吞掉） |
| Modify | `backend/workers/review_worker.py` | 持有 service、提前 output_language、9 个集成点 |
| Create | `tests/test_github_app_check_run.py` | GitHubAppClient check run 方法单测 |
| Create | `tests/test_check_run_service.py` | CheckRunService 单测（映射/中英/幂等/吞异常/无 emoji） |
| Create/Modify | `tests/test_review_worker_check_run.py` | ReviewWorker 生命周期集成测试 |
| Modify | `README.md` + `backend/README.md` | 文档更新 |

## 关键接口契约（跨任务一致）

- `GitHubAppClient.create_check_run(owner, name_repo, name, head_sha, status="queued", conclusion=None, output_title=None, output_summary=None, output_text=None) -> dict | None`（返回 `{"id": int}` 或 None）
- `GitHubAppClient.update_check_run(owner, name_repo, check_run_id, status=None, conclusion=None, ...) -> bool`
- `GitHubAppClient.find_check_run_for_sha(owner, name_repo, head_sha, name) -> int | None`
- `CheckRunService.CHECK_RUN_NAME = "Sakura AI Review"`
- `CheckRunService.report_{queued,progress,completed,failed,cancelled,skipped}(...)` —— 全部 `async`，返回 `None`，内部吞异常
- decision→conclusion：`approve→success`，`comment/request_changes→neutral`
- stage 取值：`indexing / summary / reviewing / reporting`

## 实现锚点（review_worker.py）

| 集成点 | 行号 | 调用 |
|---|---|---|
| output_language 初始化 | L264 | （保留，提前赋值） |
| should_skip | L290 后 | report_skipped(reason, lang) |
| _create_review_record 后 | L304 后 | 提前获取 output_language + report_queued |
| 代码索引 | L319 分支内 | report_progress("indexing") |
| PR 总结 | L454 分支内 | report_progress("summary") |
| REVIEWING 设置 | L693 后 | report_progress("reviewing") |
| 决策完成后 | L938 后 | report_progress("reporting") 紧接 L932 COMPLETED 后 report_completed |
| COMPLETED | L932-938 后 | report_completed(decision, score, comment_count, summary_excerpt) |
| except Exception | L1017 后 | report_failed |
| except CancelledError | L1055 后 | report_failed |
| _cancel_and_cleanup | L253 后 | report_cancelled |

## 任务清单

### Task 1: 配置项
- Modify `config.py`：加 `enable_check_runs: bool = Field(True, description="是否启用 GitHub Check Runs 审查进度可视化")`，注册到 `BASIC_CONFIG_KEYS`。

### Task 2: GitHubAppClient 底层方法 + 单测
- 加 3 方法（复用 get_repo_client，PyGithub create_check_run/get_check_run/get_check_runs_for_ref）。
- 单测 mock PyGithub，验证调用参数、id 返回、None 容错。

### Task 3: CheckRunService + 单测
- 新建 service：中英文本常量表、6 个 report_* 方法、find_or_create、配置开关、try/except 吞异常。
- 单测：decision→conclusion 映射、中英 output、find_or_create 幂等、异常吞掉、enable_check_runs=False 跳过、output 无 emoji。

### Task 4: ReviewWorker 集成 + 集成测试
- `__init__` 加 `self.check_run_service = CheckRunService()`。
- 提前 output_language 到 L304 后。
- 9 个集成点插入调用。
- 集成测试 mock CheckRunService，验证各节点按序调用、各分支正确。

### Task 5: Ruff + README
- `ruff check` + `ruff format`。
- 两个 README 增加Check Runs 可视化 + enable_check_runs 配置说明。

## 验证
- `ruff check backend/ tests/`
- `pytest tests/test_check_run_service.py tests/test_github_app_check_run.py tests/test_review_worker_check_run.py -v`
- 现有测试不回归：`pytest tests/test_review_worker_timeout.py -v`
