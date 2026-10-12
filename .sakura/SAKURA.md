# Sakura AI Reviewer 概述

## 1. 项目简介
基于 AI 的智能 GitHub PR 代码审查与 Issue 分析机器人，具备主动探索代码库的能力。

## 2. 仓库信息
- 仓库名: Sakura520222/Sakura-AI
- 语言统计: Python: 8744585, HTML: 1130029, Shell: 357518, Dockerfile: 3135
- 累计反思 28 次

## 3. 核心审查原则
- 完整性验证：PR 描述/提交/文件/diff 一致核对；功能逐项勾选，差异 >10% 标 minor。
- 删减与配置核验：批量清理后跑格式化（`ruff format`）；移除配置项核验 `config.py`、`DYNAMIC_CONFIG_GROUPS`、`templates/`、`translations/`、`.env` 五要素；遵从引用->声明->UI->i18n->CI 链路；配置合法性校验物理前置于空数据早退分支。
- 配置隔离与惰性解耦：`config.py` 严禁顶层静态导入含外部三方依赖模块；动态选项采用函数化惰性加载，防止 SDK 缺失崩溃。
- 前端 CSS/UI 截断核验：移除截断类时核验父容器弹性高度、全域无二次截断、`whitespace-pre-wrap` 防坍塌；UI 优先渐进增强。
- 双轨评分与批准规范：增量+整体双轨；历史 major 未清零上限 8；quick 上限 8；10/10≠无风险，主动排查测试遗漏；approve 须声明未解问题；request_changes 对应 major/blocking；历史 major 闭环须结案并搜引用。

## 4. 硬规则（重点）
- 依赖升级与 Lockfile 同步（Fail-Closed）：
  - 核心依赖修改（含子包 `updater/`, `sandboxer/`）必须同步 `uv.lock`。
  - Dependabot 或常规 PR 遗漏锁文件时（即使是 +1/-1 的 patch 升级），必须严格定级 `major / request_changes` 阻断合并。
  - 修复优先推荐最小化升级 `uv lock --upgrade-package <pkg>`；自动提锁 workflow 须绑定 `pull_request_target` 并校验 `dependabot[bot]`。
- CI、Workflow 与安全凭据防线：CI 失败标 error，合并前须全绿；机器人需 Secrets 时禁绝 `pull_request`，必用 `pull_request_target` 校验 actor；严禁将 PAT 拼入 git push URL；工作流变更补契约单测。
- PEP 758 语法识别：Python 3.14+ 允许 `except TypeError, ValueError:` 省略括号，严禁误判为 SyntaxError 阻断。
- 异步排空与状态恢复：`asyncio.shield` 排空捕获底层异常须记日志再重抛 `CancelledError`；历史检查点恢复下游投影须保持与 Validator 同级防御（统一 `.get()` / Schema）。
- 并发与锁生命周期：并发锁/计数器递增须在等待前同步完成，清理严格约束为 `users == 0`，禁止 release 即删键。
- AI 协议与容错兜底：输出 JSON 提取收敛为带自愈通用工具，兼容 `0` 与 `None` 边界；模型完全失败拒绝伪造结构化假报告，显式标注故障提示人工处理。

## 5. Agent/Worker/Issue 治理
- Worker 与恢复机制：禁用无日志无退避异常忽略；任务恢复循环禁绝尾部 `asyncio.sleep(0)`；复用 `_cancel_events` 幂等取消。
- 两段式准入/补偿：分布式任务采用 `prepare_admission` + `admit_operation` 状态机；客户端参数禁作为账单主体。
- 记忆架构与 Harness 演进：任务总结与记忆解耦文件写入，走 DB 事务防重与乐观锁；多角色 AI 流水线（总结->候选->裁决）严守 Schema 容错降级；零继承原则须防旧工具废弃导致的空引用。
- Epic 级 Issue 拆解：兼具架构与修复的 Epic 强制拆解即时修复与阶段目标；重复检测区分基础设施重构与上层业务演进。

## 6. 知识库与集中配置
- `.sakura/` 增量追加或精确修改，禁止覆盖式重写。
- 行号白名单改用语义化标记；配置集中 `backend/core/config.py`，新配置 env+文件双入口并 CI 校验。

## 7. 最新反思要点
- PR671/670/669/667：Dependabot 补丁升级（`python-slugify`, `httpx2`, `tzdata` 等）频发锁文件脱节与子包漂移阻断，固化依赖变更必勾稽 `uv.lock` 之 Fail-Closed 硬规则，建议指引 `uv lock --upgrade-package <pkg>`。
- ISSUE666：长期记忆系统重构与 Harness 2.0 集成，提炼多角色 AI 裁决容错、DB 事务防重与阶段拆解范式。
- PR664：Dependabot 工作流安全硬防线，确立 `pull_request_target`、凭据脱敏与 Workflow 契约测试规范。
- PR661：Agent Harness 取消排空保留诊断日志与反序列化边界防御一致性。
- PR660：Billing 2.0 两段式准入记账与全局配置禁止静态导入三方依赖隔离。

## 8. 技术栈
FastAPI (Python 3.14+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · 多协议 AI（OpenAI / Anthropic / Gemini / 兼容） · MySQL 8.0 + Redis + ChromaDB · GitHub App + OAuth · Docker Compose

*最后更新：整合 PR667, PR669, PR670, PR671, ISSUE666 最新反思，精确标注累计反思 28 次*
