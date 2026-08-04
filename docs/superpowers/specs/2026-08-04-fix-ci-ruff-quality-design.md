# 修复 CI Python 质量检查 + Python 版本声明统一（ruff 治理）设计

- 日期：2026-08-04
- 分支：`hotfix/2.13.1`（从 `main` 拉出）
- 版本：`2.13.0` → `2.13.1`
- 状态：已与用户确认设计（含 Python 版本统一补充），待实现

## 背景

### 问题一：CI `Python 质量检查` 全线失败

**Ruff v0.16.0（2026-07-23 发布）将默认规则集从 59 条扩展到 413 条**（新增 B/UP/RUF/SIM/PIE/C4/TRY/DTZ 等类别）。项目此前没有任何 ruff 配置文件，CI 通过 `pip install ruff>=0.8.0` 安装到 0.16.x 后，新规则暴露历史代码大量风格/质量问题。

### 问题二：Python 版本声明不一致

项目实际运行时已从 Python 3.11 升至 3.14（#441 合并到 develop、本地 venv 已用 3.14.6 重建并验证全部依赖兼容），但代码库多处仍声明 3.11：CI/Dockerfile 仍按 3.11 跑、文档/badge 仍写 3.11+。main 分支的 Dockerfile 仍是 `python:3.11-slim`（#441 只合到 develop）。

本次 hotfix 在 main 上一并落实两项修复。

## 目标

1. 让 CI `ruff check .` 转绿（exit 0）
2. 保留 ruff 0.16 默认规则中真实有价值的检测能力（不退回 59 条旧行为）
3. 将代码库所有 Python 版本声明统一为 3.14（配置、CI、Dockerfile、文档）
4. 改动可控、可回归验证
5. 建立 ruff 规则基线，避免未来 ruff 升级再次冲击 CI

## 设计

### 1. 新增 `ruff.toml`（项目根）

接受 ruff 0.16 的 413 条默认规则，target-version 设为 py314，显式 ignore 不适合本项目的类别：

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

关键决策：

- `target-version = "py314"`：项目运行时与最低支持版本均为 3.14（Dockerfile/CI/venv 统一），让 UP 规则按 3.14 语义现代化（比 py311 多触发 UP017/UP041/UP046 等约 64 处，均纳入本次修复）
- **不显式设 `line-length`**：保持默认 88（当前代码已合规，E501 = 0）
- **不显式 `select`**：继承 ruff 0.16 默认 413 条规则
- 配置文件放在**项目根**（符合 ruff 惯例，本地与 CI 一致发现）

**ignore 类别明细**（共约 1268 处）：

| 规则 | 数量 | 忽略理由 |
|------|------|----------|
| B008 | 506 | FastAPI `Depends()`/`Query()` 在默认参数里是框架标准用法，ruff 官方建议 FastAPI 项目忽略 |
| BLE001 | 596 | API 层 `except Exception` 全局兜底是本项目既有模式，量大，留作后续专项治理 |
| S110 | 28 | `try-except-pass` 多为有意的静默吞异常 |
| RUF012 | 44 | pydantic Model 的可变类默认值，pydantic v2 场景常见 |
| ISC004 | 40 | 隐式字符串拼接属风格偏好，不影响正确性 |
| DTZ | 45 | datetime 时区涉及业务语义，需逐处判断，不宜批量自动改 |
| SIM102 | 9 | collapsible-if 影响可读性，保留显式形态 |

### 2. Python 版本声明统一到 3.14

| 文件 | 位置 | 当前 | 改为 |
|------|------|------|------|
| [CLAUDE.md](CLAUDE.md) | :3 | `Python 3.11+` | `Python 3.14+` |
| [AGENTS.md](AGENTS.md) | :5 | `Python 3.11+ FastAPI service` | `Python 3.14+ FastAPI service` |
| [AGENTS.md](AGENTS.md) | :51 | `on Python 3.11` | `on Python 3.14` |
| [README.md](README.md) | :12 | badge `Python-3.11+` | `Python-3.14+` |
| [README.md](README.md) | :198 | `FastAPI (Python 3.11+)` | `FastAPI (Python 3.14+)` |
| [README_EN.md](README_EN.md) | :12 | badge `Python-3.11+` | `Python-3.14+` |
| [README_EN.md](README_EN.md) | :198 | `FastAPI (Python 3.11+)` | `FastAPI (Python 3.14+)` |
| [.github/workflows/ci.yml](.github/workflows/ci.yml) | :28,:31 | `设置 Python 3.11` + `'3.11'` | `设置 Python 3.14` + `'3.14'` |
| [.github/workflows/release-on-pr-merge.yml](.github/workflows/release-on-pr-merge.yml) | :474,:477 | `设置 Python 3.11` + `'3.11'` | `设置 Python 3.14` + `'3.14'` |
| [docker/Dockerfile](docker/Dockerfile) | :2 | `FROM python:3.11-slim` | `FROM python:3.14-slim` |
| [.sakura/SAKURA.md](.sakura/SAKURA.md) | :5 | `Python 3.11+/FastAPI/...` | `Python 3.14+/FastAPI/...` |
| [.sakura/KNOWLEDGE_BASE.md](.sakura/KNOWLEDGE_BASE.md) | :71 | `后端语言: Python 3.11+` | `后端语言: Python 3.14+` |

**不改**（历史记录或非版本声明）：

- `.sakura/memory/*.md`、`.sakura/plans/*.md` — 审查历史记录，反映当时事实
- [docs/api-v1-reference.md:2185](docs/api-v1-reference.md#L2185) `### 3.11 队列监控` — API 章节编号，非 Python 版本
- [backend/services/ai_reviewer/constants.py:263](backend/services/ai_reviewer/constants.py#L263) `'Python 3.12 新特性'` — 搜索查询示例文本

### 3. 自动修复（约 1474 处）

执行 `ruff check . --fix --unsafe-fixes`，修复全部可安全自动修复的违规：

- **UP045/UP006/UP035/UP032/UP037/UP011**：类型注解现代化（`Optional[X]`→`X | None`、`List`→`list`）
- **UP017**（py314 新增，56 处）：`datetime.timezone.utc`→`datetime.UTC`
- **UP041**（py314 新增，7 处）：`socket.timeout`→`TimeoutError`
- **I001**：导入排序
- **RUF010/RUF100/RUF022/RUF023**：ruff 专用清理
- **RET501/PLR1711/PIE790**：return/占位清理
- **SIM117/SIM114/SIM118**：with 合并、比较简化
- **PLE2515**：零宽字符清理（隐藏 bug）
- 其他单次出现的 FURB/PERF/PLR0402 等

### 4. 人工修复（约 102 处）

不可自动修复的违规，按真实问题优先级人工处理：

| 规则 | 数量 | 处理方式 |
|------|------|----------|
| RUF013 | 30 | 隐式 Optional：`x: str = None` → `x: str \| None = None` |
| RUF059 | 13 | 未使用解包变量：删除或改名 |
| PIE810 | 10 | starts/ends 重复调用合并 |
| C401 | 8 | set 生成器改集合字面量 |
| TRY002 | 7 | raise vanilla class：改用 Exception 子类，或追加到 ignore |
| PLW0602 | 5 | global 未赋值：补赋值或移除 |
| PLE1205 | 3 | logging 参数数量不匹配（真实 bug，必须修） |
| PLE2515 | 3 | 零宽字符（`--fix` 未完全清理时人工补） |
| ASYNC230/221 | 3 | async 函数里阻塞调用：改用 `asyncio.to_thread` / 非阻塞替代 |
| UP046 | 1 | PEP 695 泛型类（py314 新增）：评估是否改写为新语法 |
| 其他零散 | ~9 | C414/PERF102/RUF034/RUF015/TRY004 等单次出现，逐处判断 |

原则：真实 bug（PLE/ASYNC/PLW）必须修；风格类能修则修；确属项目模式的追加到 `ruff.toml` 的 `ignore`。

## Gitflow 执行

按 CLAUDE.md Gitflow 规范，**不直接 push main/develop**：

1. 当前 main 版本：`v2.13.0`（见 [backend/__init__.py:3](backend/__init__.py#L3)）
2. 从 main 创建 `hotfix/2.13.1`
3. 在 hotfix 分支完成：
   - 新增项目根 `ruff.toml`
   - 修改上述 12 处 Python 版本声明（3.11 → 3.14）
   - 执行 `ruff check . --fix --unsafe-fixes`
   - 人工修复剩余约 102 处
   - 更新 `backend/__init__.py` 的 `__version__ = "2.13.1"`
   - 检查两个 README 是否有其他版本号引用需同步
4. 本地验证（见下）
5. PR 到 `main`
6. 合并 main 后，按 Gitflow 将 main 回合到 `develop`（develop 已有 #441 的 Dockerfile 3.14 改动，回合时需注意协调）

**版本号仍为 2.13.1**：本次属 hotfix 性质——修复 CI 配置 + 在 main 上对齐已决策的 Python 3.14 升级（#441 已在 develop 合并），无新功能引入。

## 验证标准

- `ruff check .` exit code 0（本地 Python 3.14 venv + ruff 0.16.1，target-version py314）
- `pytest -q` 全量通过（自动修复涉及导入重排与类型注解变更，需确认无运行时回归）
- `git diff` 抽查，重点关注：
  - I001 导入顺序（`__init__.py` 的 `__all__`、循环导入）
  - UP006/UP045 类型注解（pydantic 模型字段、函数签名）
  - UP017 `datetime.UTC` 改动（确认 3.14 运行时可用）
- 全仓库 `grep "3\.11"` 仅剩历史记录类文件（.sakura/memory、.sakura/plans）与无关章节号
- `ruff.toml` 存在且被 CI 识别

## 范围外（Out of Scope）

- BLE001 的逐处收窄（另开治理任务）
- DTZ 时区规则的业务化处理（另开治理任务）
- CI workflow 的 actions 版本升级（checkout/setup-python 的 v4→v7 等）
- `.sakura/memory/`、`.sakura/plans/` 历史记录的版本号回填（保留历史原貌）
- 两个 README 的功能说明更新（仅改 badge 与技术栈版本号）

## 风险

- `--fix --unsafe-fixes`（约 108 处隐藏修复）理论上可能改变语义，依赖 pytest 覆盖
- I001 导入重排可能影响 `__init__.py` 的 `__all__` 或触发循环导入
- UP017 将 `datetime.timezone.utc` 改为 `datetime.UTC`，需确认所有调用点在 3.14 行为一致
- hotfix 合并 main 后回合 develop 时，develop 已有 #441 的 Dockerfile 改动，需确认无冲突
- 自动修复涉及数十个文件，diff 较大，PR 评审需聚焦语义变更
