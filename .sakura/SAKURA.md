# Sakura AI Reviewer 概述

## 1. 项目简介
基于 AI 的智能 GitHub Pull Request 代码审查与 Issue 分析机器人，具备主动探索代码库的能力。

## 2. 仓库信息
- 仓库名: Sakura520222/Sakura-AI
- 语言统计: Python: 8744585, HTML: 1130029, Shell: 357518, Dockerfile: 3135
- 累计反思 22 次

## 3. 核心审查原则
- 完整性验证：PR 描述/提交/文件/diff 一致核对；功能逐项勾选，差异 >10% 标 minor。
- 删减与配置核验：批量清理后跑格式化（如 `ruff format`）；移除配置项核验 `config.py`、`DYNAMIC_CONFIG_GROUPS`、`templates/`、`translations/`、`.env` 五要素；校验遵从代码引用 -> 配置声明 -> UI 渲染 -> i18n -> CI/部署环境；配置合法性校验物理前置于任何空数据早退分支（Guard Before Early-Exit）。
- 全局配置隔离与惰性解耦：`backend/core/config.py` 严禁顶层静态导入含外部三方依赖或可选组件的模块；动态选项（币种/模型列表等）采用函数化惰性加载（Lazy Provider），防止外部 SDK 缺失引发全系统启动崩溃。
- 前端 CSS/UI 截断核验：移除 `line-clamp-*` / `truncate` 时核验父容器弹性高度与 `mt-auto`、全域无残留二次截断、`whitespace-pre-wrap` 避免坍塌三要素；UI 优先利用 Alpine.js / `<details>` 等渐进增强。
- 双轨评分与批准规范：增量+整体双轨；历史 major 未清零上限 8；quick 上限 8；10/10≠无风险，主动排查安全/测试遗漏；approve 须声明未解决问题；request_changes 对应 major/blocking；历史 major 闭环须结案并触发全库引用搜索。

## 4. 硬规则（重点）
- CI、Workflow 与安全凭据防线：CI 失败标 error，合并前须全绿；Dependabot 等机器人触发需写入或访问 Secrets 时，禁绝 `pull_request`（secrets 恒为空），必须用 `pull_request_target` 并严格校验 `github.event.pull_request.user.login == 'dependabot[bot]'`；严禁将 PAT 拼入 git push URL（防日志泄露），统一走 `actions/checkout` 的 `token:`；涉及工作流关键变更须补 YAML 契约单测。
- PEP 758 语法识别：Python 3.14+ 允许不带 `as` 的多异常捕获省略括号（`except TypeError, ValueError:` 等价于 `except (TypeError, ValueError):`）。严禁误判为 Python 2 遗留或硬 SyntaxError 并阻断合并。
- 异步排空与状态恢复防御：
  - Agent/Worker 取消排空：用 `asyncio.shield` 排空耗时任务或子进程时，捕获到底层业务异常必须记录诊断日志后再重抛 `CancelledError`，严禁完全吞没异常。
  - 检查点一致性：历史状态恢复下游投影必须保持与前置 Validator 同级防御（统一 `.get()` 或 Schema 解码），禁止因前置漏判在后置阶段硬下标访问引发未处理异常。
- 并发与锁生命周期：并发所有权锁 (Ownership Lock) / Users 计数器递增必须在进入 acquire 等待队列前同步完成，清理/删键严格约束为 `users == 0`，禁止持有者 release 即删键，防交接期竞争产生分裂锁。
- AI 协议适配与故障兜底：
  - AI 输出 JSON 提取必须收敛为带自愈与 Recovery 的通用解析工具，严禁裸调 `json.loads`；OpenAI Compatible 网关需严格兼容 `0` 与 `None` 边界（如 `output_tokens=0`），防非标结构覆盖用量。
  - 模型调用完全失败或返回全空时，拒绝生成结构化假数据报告，显式标注“AI 治理服务暂时不可用/解析失败”，提示人工手动 triage。
- 依赖与格式：依赖声明修改必须同 PR 更新 `uv.lock`，Dependabot PR 遗漏锁文件时严格 Fail-Closed 阻断并指引运行 `uv lock`；端点级多语言（i18n）在各异常抛出分支必须统一解析语言（`lang=user_lang`）。

## 5. Agent/Worker/Issue 治理
- Worker 与恢复机制：禁用无日志无退避的异常忽略；任务恢复循环禁绝尾部 `asyncio.sleep(0)` 死循环；统一复用 `_cancel_events` 幂等取消。
- 两段式准入/补偿：分布式任务采用 `prepare_admission` + `admit_operation` 两段式状态机，记账防漏；客户端参数禁作为账单主体，由服务端上下文强行覆盖。
- Epic 级 Issue 拆解：兼具架构演进与修复的 Epic，强制拆解即时修复（P0 Quick Wins）与阶段目标；重复检测严格区分架构提案与单业务 Issue。

## 6. 知识库与集中配置
- `.sakura/` 增量追加或精确修改，禁止覆盖式重写。
- 行号白名单改用语义化标记；配置集中 `backend/core/config.py`，新配置 env+文件双入口并 CI 校验。

## 7. 最新反思要点
- PR661：Agent Harness 取消排空与反序列化边界治理，规范异步排空保留诊断日志与检查点反序列化防御一致性。
- PR660：Billing 2.0 两段式准入与预留补偿；提炼全局配置（`config.py`）禁止静态引入外部业务 SDK 的依赖隔离规范。
- PR664：Dependabot 工作流因 `pull_request` 无法获取 secrets 及 actor 名错误（`app/dependabot` vs `dependabot[bot]`）阻断，确立 `pull_request_target`、凭据脱敏与 Workflow 契约测试规范。
- ISSUE663：Issue 关系验证 `JSONDecodeError` 及 Gemini 思考信息/Token 统计适配，强化协议适配层 `0` 与 `None` 治理及 JSON 安全提取。
- ISSUE662：模型全线失败时兜底防护，确立 AI 故障拒绝伪造结构化假报告并显式标注入库原则。

## 8. 技术栈
FastAPI (Python 3.14+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · 多协议 AI（OpenAI / Anthropic / Gemini / 兼容） · MySQL 8.0 + Redis + ChromaDB · GitHub App + OAuth · Docker Compose

*最后更新：整合 PR660, PR661, ISSUE662, ISSUE663, PR664 最新反思，精确标注累计反思 22 次*
