# CI ruff 质量检查修复 + Python 3.14 版本统一 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 CI `ruff check .` 失败 + 将代码库 Python 版本声明统一为 3.14，发布为 `hotfix/2.13.1`。

**Architecture:** 新增 `ruff.toml` 锁定规则基线（target py314 + 选择性 ignore），执行 `ruff --fix` 自动修复约 1474 处，人工修复剩余约 102 处，同步 12 处文档/配置的 Python 3.11→3.14 声明。

**Tech Stack:** ruff 0.16.1、Python 3.14.6、uv venv、Gitflow hotfix。

**执行约束（CLAUDE.md）：**
- 禁止自主 git commit / push；所有改动完成后由用户审阅统一提交
- 计划中不写 commit 步骤，改用"检查点"标注逻辑分组
- 不直接 push main/develop；走 hotfix PR 流程

**本地环境：** `.venv`（Python 3.14.6，uv 创建，依赖已装齐），ruff 0.16.1。命令前缀 `.venv/Scripts/python.exe -m ruff`。

---

### Task 1: 创建 hotfix 分支

**Files:** 无（git 操作）

- [ ] **Step 1: 确认在 main 且工作区干净**

Run: `git status --short && git branch --show-current`
Expected: 输出为空（clean）+ `main`

- [ ] **Step 2: 从 main 创建 hotfix/2.13.1**

Run: `git checkout -b hotfix/2.13.1`
Expected: `Switched to a new branch 'hotfix/2.13.1'`

---

### Task 2: 新增项目根 ruff.toml

**Files:**
- Create: `ruff.toml`

- [ ] **Step 1: 创建 ruff.toml**

```toml
target-version = "py314"

[lint]
ignore = [
    "B008",     # Depends()/Query() in argument defaults — FastAPI 标准用法
    "BLE001",   # except Exception — API 层兜底异常，后续单独治理
    "S110",     # try-except-pass — 有意的静默
    "RUF012",   # 可变类默认 — pydantic Model 常见模式
    "ISC004",   # 隐式字符串拼接 — 风格偏好
    "DTZ",      # datetime 时区 — 需业务决策
    "SIM102",   # collapsible-if — 可读性风格
]
```

- [ ] **Step 2: 验证 ruff 识别配置 + 统计剩余违规**

Run: `.venv/Scripts/python.exe -m ruff check . --statistics`
Expected: 总违规从 2844 降至约 1576（减去 1268 处 ignore）；顶部不再出现 B008/BLE001/S110/RUF012/ISC004/DTZ/SIM102

---

### Task 3: Python 版本声明统一（12 处文件）

**Files (Modify):**
- `CLAUDE.md`
- `AGENTS.md`
- `README.md`
- `README_EN.md`
- `.github/workflows/ci.yml`
- `.github/workflows/release-on-pr-merge.yml`
- `docker/Dockerfile`
- `.sakura/SAKURA.md`
- `.sakura/KNOWLEDGE_BASE.md`

**具体替换（3.11 → 3.14）：**

- [ ] **Step 1: CLAUDE.md:3** — `### Python 3.11+ / FastAPI` → `### Python 3.14+ / FastAPI`
- [ ] **Step 2: AGENTS.md:5** — `Python 3.11+ FastAPI service` → `Python 3.14+ FastAPI service`
- [ ] **Step 3: AGENTS.md:51** — `on Python 3.11` → `on Python 3.14`
- [ ] **Step 4: README.md:12** — badge `Python-3.11+` → `Python-3.14+`
- [ ] **Step 5: README.md:198** — `FastAPI (Python 3.11+)` → `FastAPI (Python 3.14+)`
- [ ] **Step 6: README_EN.md:12** — badge `Python-3.11+` → `Python-3.14+`
- [ ] **Step 7: README_EN.md:198** — `FastAPI (Python 3.11+)` → `FastAPI (Python 3.14+)`
- [ ] **Step 8: ci.yml:28** — `设置 Python 3.11` → `设置 Python 3.14`
- [ ] **Step 9: ci.yml:31** — `python-version: '3.11'` → `python-version: '3.14'`
- [ ] **Step 10: release-on-pr-merge.yml:474** — `设置 Python 3.11` → `设置 Python 3.14`
- [ ] **Step 11: release-on-pr-merge.yml:477** — `python-version: '3.11'` → `python-version: '3.14'`
- [ ] **Step 12: Dockerfile:2** — `FROM python:3.11-slim` → `FROM python:3.14-slim`
- [ ] **Step 13: SAKURA.md:5** — `Python 3.11+/FastAPI` → `Python 3.14+/FastAPI`
- [ ] **Step 14: KNOWLEDGE_BASE.md:71** — `后端语言: Python 3.11+` → `后端语言: Python 3.14+`

- [ ] **Step 15: 验证替换完整性**

Run:
```bash
grep -rn "3\.11" --include="*.md" --include="*.yml" --include="Dockerfile" . \
  | grep -vE "\.sakura/(memory|plans)|api-v1-reference|docs/superpowers"
```
Expected: 输出为空（所有项目声明已改）

---

### Task 4: ruff 自动修复

- [ ] **Step 1: 运行自动修复**

Run: `.venv/Scripts/python.exe -m ruff check . --fix --unsafe-fixes`
Expected: 输出 `Fixed N errors.` + 剩余约 102 处不可自动修

- [ ] **Step 2: 查看改动规模**

Run: `git diff --stat`
Expected: 数十个 `.py` 文件改动（导入排序、类型注解现代化、datetime.UTC 等）

- [ ] **Step 3: 抽查语义等价性（重点）**

Run:
```bash
git diff -- backend/api/v1/__init__.py backend/__init__.py backend/services/webauthn_service.py
```
Expected: 仅风格变化（导入顺序、`Optional[X]`→`X | None`、`List`→`list`），无逻辑改动

- [ ] **Step 4: 运行 pytest 确认无回归**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全通过。若因缺 MySQL/Redis 等环境导致部分测试 error，记录范围、聚焦 ruff 改动相关失败

- [ ] **检查点：自动修复完成，剩余违规进入人工修复**

---

### Task 5: 人工修复剩余违规（按规则分批）

对每类规则，流程：`ruff check . --select <RULE> --output-format=concise` 查看位置 → 逐处修复 → `ruff check . --select <RULE>` 确认清零。

- [ ] **Step 1: RUF013（约 30 处）— 隐式 Optional**

查看: `.venv/Scripts/python.exe -m ruff check . --select RUF013 --output-format=concise`
修复模式: `def f(x: str = None)` → `def f(x: str | None = None)`
验证: `ruff check . --select RUF013` → 无输出

- [ ] **Step 2: RUF059（约 13 处）— 未使用解包变量**

查看: `ruff check . --select RUF059 --output-format=concise`
修复模式: 删除未使用的解包变量，或用 `_` 占位
验证: 清零

- [ ] **Step 3: PIE810（约 10 处）— starts/ends 重复**

查看: `ruff check . --select PIE810 --output-format=concise`
修复模式: `x.startswith("a") or x.startswith("b")` → `x.startswith(("a", "b"))`
验证: 清零

- [ ] **Step 4: C401（约 8 处）— set 生成器**

查看: `ruff check . --select C401 --output-format=concise`
修复模式: `set(x for x in y)` → `{x for x in y}`
验证: 清零

- [ ] **Step 5: PLE1205（约 3 处）— logging 参数不匹配（真实 bug）**

查看: `ruff check . --select PLE1205 --output-format=concise`
修复模式: 对齐 format 占位符与参数数量
验证: 清零

- [ ] **Step 6: ASYNC230 / ASYNC221（约 3 处）— async 函数阻塞调用**

查看: `ruff check . --select ASYNC230,ASYNC221 --output-format=concise`
修复模式: 阻塞 `open()`/`subprocess` 改用 `asyncio.to_thread` 或 `aiofiles` 等异步替代
验证: 清零

- [ ] **Step 7: PLW0602（约 5 处）— global 未赋值**

查看: `ruff check . --select PLW0602 --output-format=concise`
修复模式: 补 global 赋值或移除多余 global 声明
验证: 清零

- [ ] **Step 8: UP046（约 1 处）— PEP 695 泛型类**

查看: `ruff check . --select UP046 --output-format=concise`
修复模式: 评估 `class Foo(Generic[T])` 是否改写为 `class Foo[T]:`（3.14 新语法）；若改动过大或不合适，在 ruff.toml 追加 `"UP046"` 到 ignore 并注释理由
验证: 清零或已 ignore

- [ ] **Step 9: TRY002（约 7 处）— raise vanilla class**

查看: `ruff check . --select TRY002 --output-format=concise`
决策: 若 raise 的是项目自定义异常的基类场景，改用具体子类；若属于跨边界通用异常，在 ruff.toml 追加 `"TRY002"` 到 ignore
验证: 清零或已 ignore

- [ ] **Step 10: 其他零散（C414/PERF102/RUF034/RUF015/TRY004/PLW1510 等单次出现）**

查看: `ruff check . --output-format=concise`（剩余全部）
逐处判断修复或 ignore
验证: 清零

- [ ] **Step 11: 全量 ruff 检查**

Run: `.venv/Scripts/python.exe -m ruff check .`
Expected: `All checks passed!`

- [ ] **检查点：人工修复完成**

---

### Task 6: 更新版本号到 2.13.1

**Files (Modify):**
- `backend/__init__.py`

- [ ] **Step 1: 更新 __version__**

`backend/__init__.py:3`: `__version__ = "2.13.0"` → `__version__ = "2.13.1"`

- [ ] **Step 2: 检查 README 是否有版本号引用需同步**

Run: `grep -rn "2\.13\.0" README.md README_EN.md`
Expected: 空（或仅历史 changelog 引用，无需改）

---

### Task 7: 最终验证

- [ ] **Step 1: ruff 全通过**

Run: `.venv/Scripts/python.exe -m ruff check .`
Expected: `All checks passed!` exit 0

- [ ] **Step 2: pytest 全通过**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全通过（或仅有与环境无关的已知跳过）

- [ ] **Step 3: Python 版本声明一致性**

Run:
```bash
grep -rn "3\.11" . \
  | grep -vE "\.venv|\.sakura/(memory|plans)|api-v1-reference|docs/superpowers|\.git/"
```
Expected: 空

- [ ] **Step 4: 改动总览**

Run: `git status --short && git diff --stat`
Expected: 文件清单合理（ruff.toml 新增 + 配置/文档版本替换 + 数十个 .py 自动修 + 少量 .py 人工修）

- [ ] **检查点：hotfix/2.13.1 就绪，等待用户审阅与提交**

---

## Self-Review

1. **Spec coverage:** spec 的 4 个设计章节（ruff.toml / 版本统一 / 自动修复 / 人工修复）分别对应 Task 2 / Task 3 / Task 4 / Task 5；Gitflow 对应 Task 1+7；版本号对应 Task 6。无遗漏。
2. **Placeholder scan:** Task 5 的人工修复给的是"查看命令 + 修复模式 + 验证"的方法模板，而非预知代码——这是必要的，因为确切位置依赖 `--fix` 后的实际剩余违规。每步都给出了具体命令和期望输出，无空泛描述。
3. **Type consistency:** 版本号（2.13.1）、规则代码（RUF013 等）、target-version（py314）全文一致。
