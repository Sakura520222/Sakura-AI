# Sakura AI Reviewer 概述

## 1. 项目简介
基于 AI 的智能 GitHub PR 代码审查与 Issue 分析机器人，具备主动探索代码库的能力。

## 2. 仓库信息
- 仓库名: Sakura520222/Sakura-AI
- 语言统计: Python: 8744585, HTML: 1130029, Shell: 357518, Dockerfile: 3135
- 累计反思 28 次

## 3. 核心审查原则
- 完整性验证：PR 描述/提交/文件/diff 一致核对；功能逐项勾选，差异 >10% 标 minor。
- 删减与配置核验：批量清理后跑格式化（如 `ruff format`）；移除配置项核验 `config.py`、`DYNAMIC_CONFIG_GROUPS`、`templates/`、`translations/`、`.env` 五要素；校验遵从代码引用 -> 配置声明 -> UI 渲染 -> i18n -> CI/部署环境；配置合法性校验物理前置于任何空数据早退分支。
- 全局配置隔离与惰性解耦：`backend/core/config.py` 严禁顶层静态导入含外部依赖或可选组件的模块；动态选项采用惰性加载，防 SDK 缺失引发系统崩溃。
- 前端 CSS/UI 截断核验：移除 `line-clamp-*` / `truncate` 时核验父容器弹性高度与 `mt-auto`、全域无残留二次截断、`whitespace-pre-wrap` 避免坍塌三要素；优先利用 Alpine.js 增强。
- 双轨评分与批准规范：增量+整体双轨；历史 major 未清零上限 8；quick 上限 8；10/10≠无风险，主动排查安全与测试遗漏；approve 须声明未解决问题；request_changes 对应 major/blocking；历史 major 闭环须结案并触发全库引用搜索。

## 4. 硬规则（重点）
- CI、Workflow 与安全凭据防线：CI 失败标 error，合并前须全绿；Dependabot 等机器人触发需写入或访问 Secrets 时，禁绝 `pull_request`，必须用 `pull_request_target` 并严格校验 `github.event.pull_request.user.login == 'dependabot[bot]'`；严禁将 PAT 拼入 git push URL，统一走 `actions/checkout` 的 `token:`；关键工作流变更须补 YAML 契约单测。
- 依赖声明与锁文件原子一致性（Fail-Closed）：
  - 实行 `pyproject.toml` + `requirements.txt` + `uv.lock` 三重强一致；清单修改必须同 PR 提交锁更新，遗漏一律定级 Major 并 Request Changes。
  - Dependabot 原生仅改清单，缺乏 `uv.lock` 同步能力：若 `uv-lock-sync.yml` 因缺少 Secrets 静默跳过，CI `uv sync --locked` 必挂。审查应推荐单包最小化更新（`uv lock --upgrade-package <pkg>`）并警示工作流配置缺失，禁止无参全量 `uv lock` 引发无关依赖漂移；多包/子项目（如 `updater`、`sandboxer`）依赖下限必须全局对齐。
- PEP 758 语法识别：Python 3.14+ 允许不带 `as` 的多异常捕获省略括号（`except TypeError, ValueError:`）。严禁误判为硬语法错误阻断合并。
- 异步排空与状态恢复防御：Agent/Worker 用 `asyncio.shield` 排空耗时任务时，捕获底层异常须记日志再重抛 `CancelledError`，严禁吞没；历史状态恢复下游投影与前置 Validator 同级防御，统一 `.get()` 或 Schema 解码。
- 并发与锁生命周期：并发所有权锁 / Users 计数器递增必须在进入 acquire 等待队列前同步完成，清理/删键严格约束为 `users == 0`，禁止持有者 release 即删键，防分裂锁。
- AI 协议适配与故障兜底：AI 输出 JSON 提取必须收敛为带自愈的通用解析工具；OpenAI 兼容网关严格兼容 `0` 与 `None` 边界；模型完全失败时拒绝伪造假报告，显式标注故障并提示人工 triage。

## 5. Agent/Worker/Issue 治理
- Worker 与恢复机制：禁用无日志无退避的异常忽略；任务恢复循环禁绝尾部 `asyncio.sleep(0)` 死循环；统一复用 `_cancel_events` 幂等取消。
- 两段式准入/补偿：分布式任务采用 `prepare_admission` + `admit_operation` 两段式状态机，记账防漏；客户端参数禁作为账单主体，由服务端上下文强行覆盖。
- Epic 级 Issue 拆解：兼具架构演进与修复的 Epic，强制拆解即时修复与阶段目标；重复检测严格区分架构提案与单业务 Issue。

## 6. 知识库与集中配置
- `.sakura/` 增量追加或精确修改，禁止覆盖式重写。
- 行号白名单改用语义化标记；配置集中 `backend/core/config.py`，新配置 env+文件双入口并 CI 校验。

## 7. 最新反思要点
- PR667~PR671（依赖锁机制与 Dependabot 治理）：确立依赖清单与 `uv.lock` 强原子性审查防线；深挖 Dependabot 无法原生锁定的系统性缺陷，明确推荐 `uv lock --upgrade-package <pkg>` 单包最小破坏升级；防范 `uv-lock-sync.yml` 因缺少 Secrets 静默跳过的降级陷阱，并核验子项目（`updater`/`sandboxer`）依赖版本下限漂移。
- PR664：Dependabot 工作流安全隔离，明确 `pull_request_target`、凭据脱敏与 Workflow 契约测试规范。
- PR661：Agent Harness 取消排空诊断日志保留与检查点反序列化防御一致性。
- PR660：Billing 2.0 两段式准入记账；确立全局配置隔离与外部 SDK 惰性加载规范。
- ISSUE662~ISSUE663：AI 协议 `0`/`None` 健壮适配，故障全线熔断拒绝假报告原则。

## 8. 技术栈
FastAPI (Python 3.14+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · 多协议 AI（OpenAI / Anthropic / Gemini / 兼容） · MySQL 8.0 + Redis + ChromaDB · GitHub App + OAuth · Docker Compose

*最后更新：整合 PR667~PR671 最新依赖治理与锁机制反思，精确标注累计反思 28 次*
