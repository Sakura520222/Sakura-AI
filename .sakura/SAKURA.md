# Sakura AI Reviewer 概述

## 1. 项目简介
基于 AI 的智能 GitHub PR 代码审查与 Issue 分析机器人，具备主动探索代码库能力。

## 2. 仓库信息
- 仓库名: Sakura520222/Sakura-AI
- 语言统计: Python: 8744585, HTML: 1130029, Shell: 357518, Dockerfile: 3135
- 累计反思 27 次

## 3. 核心审查原则
- 完整性验证：PR 描述/提交/文件/diff 一致核对；功能逐项勾选，差异 >10% 标 minor。
- 删减与配置核验：清理后跑格式化（`ruff format`）；移除配置项核验 `config.py`、`DYNAMIC_CONFIG_GROUPS`、`templates/`、`translations/`、`.env` 五要素；配置合法性校验物理前置于空数据早退。
- 全局配置隔离与惰性解耦：`backend/core/config.py` 严禁顶层静态导入含外部三方依赖模块；动态选项采用函数化惰性加载（Lazy Provider），防止依赖缺失致系统崩溃。
- 前端 UI 截断核验：移除 `line-clamp-*`/`truncate` 时核验父容器弹性高度与 `mt-auto`、全域无二次截断、`whitespace-pre-wrap` 防坍塌；UI 优先用 Alpine.js/`<details>`。
- 双轨评分与批准规范：增量+整体双轨；历史 major 未清零上限 8；quick 上限 8；10/10≠无风险，主动排查安全/测试遗漏；approve 须声明未决问题；request_changes 对应 major/blocking。

## 4. 硬规则（重点）
- CI/Workflow 与凭据防线：CI 失败标 error，合并前须全绿；Dependabot 等机器人触发需访问 Secrets 时，禁绝 `pull_request`，必用 `pull_request_target` 并严格校验 `github.event.pull_request.user.login == 'dependabot[bot]'`；严禁 PAT 拼入 git push URL；工作流关键变更补 YAML 契约单测。
- PEP 758 语法识别：Python 3.14+ 允许省略异常元组括号（`except TypeError, ValueError:`）。严禁误判为 Python 2 遗留或硬 SyntaxError 并阻断合并。
- 依赖升级与锁文件联动（Fail-Closed）：
  - 依赖升级三要素：声明一致性（`pyproject.toml` vs `requirements.txt`）、锁文件同步性（`uv.lock` diff）、版本安全性。
  - Dependabot 依赖 PR 频繁缺失 `uv.lock` 致 CI `uv sync --locked` 必挂；强制核验 `uv.lock`，缺失直接阻断（major/request_changes）；推荐最小化升锁 `uv lock --upgrade-package <pkg>`。
- 异步排空与状态恢复防御：
  - Agent 取消排空（Cancellation Drain）：用 `asyncio.shield` 循环排空耗时/子进程任务时，捕获底层业务异常必须保留诊断日志（DEBUG/WARNING）再重抛 `CancelledError`，严禁吞没诊断链。
  - 检查点一致性（Checkpoint Guard）：恢复历史状态下游结构投影须与前置 Validator 同级防御（统一 `.get()` 或 Schema 解码），禁因前置漏判在后置硬下标引发未捕获异常。
- 并发与锁生命周期：并发所有权锁/计数器递增在进等待队列前同步完成，删键严格约束为 `users == 0`，禁持有者 release 即删键防分裂锁。
- AI 协议适配与故障兜底：
  - AI JSON 提取收敛为通用容错工具，禁裸调 `json.loads`；OpenAI 兼容端严格兼容 `0` 与 `None` 边界（如 `output_tokens=0`）。
  - 模型全线失败或返回全空时，拒绝伪造假数据，显式标注“AI 服务不可用/解析失败”，提示人工 triage。

## 5. Agent/Worker/Issue 治理
- Worker 与恢复机制：禁用无日志无退避的异常忽略；任务恢复循环禁尾部 `asyncio.sleep(0)`；统一复用 `_cancel_events` 幂等取消。
- 两段式准入/补偿：分布式任务采用 `prepare_admission` + `admit_operation` 状态机；客户端参数禁作账单主体，由服务端覆盖。
- 架构级 Issue 拆解：端到端重构 Issue 强制分阶段拆解（P0 数据模型、P1 核心管线、P2 上下文与 Worker、P3 WebUI）；切断旧链路时同步清理工具、防御废弃接口并更新 i18n/配置，补齐 `architecture`/`core` 与 `breaking-change` 标签。

## 6. 知识库与集中配置
- `.sakura/` 增量追加或精确修改，禁止覆盖式重写。
- 行号白名单改用语义化标记；配置集中 `backend/core/config.py`，新配置 env+文件双入口并 CI 校验。

## 7. 最新反思要点
- PR670/PR669/PR667：Dependabot 遗漏 `uv.lock` 模式，确立依赖三要素审查、缺失锁文件 Fail-Closed 阻断及 `uv lock --upgrade-package <pkg>` 最小化升锁推荐。
- ISSUE666：长期记忆重构与 Harness 2.0，确立架构级 Issue 分阶段拆解（P0-P3）、破坏性变更标注与弃用工具零继承防御。
- PR661：Agent Harness 取消排空诊断日志保留与反序列化 Checkpoint Guard 防御一致性。
- PR664：Dependabot 工作流安全凭据隔离与契约测试。
- PR660：Billing 2.0 两段式准入与全局配置惰性依赖解耦。

## 8. 技术栈
FastAPI (Python 3.14+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · 多协议 AI（OpenAI / Anthropic / Gemini / 兼容） · MySQL 8.0 + Redis + ChromaDB · GitHub App + OAuth · Docker Compose

*最后更新：整合 PR670, PR669, PR667, ISSUE666, PR661 等反思，精确标注累计反思 27 次*
