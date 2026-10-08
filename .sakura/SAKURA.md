# Sakura AI Reviewer 概述

## 1. 项目简介
基于 AI 的智能 GitHub Pull Request 代码审查与 Issue 分析机器人，具备主动探索代码库的能力。

## 2. 仓库信息
- 仓库名: Sakura520222/Sakura-AI
- 语言统计: Python: 8744585, HTML: 1130029, Shell: 357518, Dockerfile: 3135
- 累计反思 12 次

## 3. 核心审查原则
- 完整性验证：PR 描述/提交/文件/diff 一致核对；功能逐项勾选，差异 >10% 标 minor。
- 删减与配置核验：批量清理逻辑后须跑格式化（如 `ruff format`）；移除配置项须核验 `config.py`、`DYNAMIC_CONFIG_GROUPS`、`templates/`、`translations/`、`.env` 五要素；校验路径遵从代码引用 -> 配置声明 -> UI 渲染 -> i18n -> CI/部署环境；配置合法性校验物理前置于任何空数据/空候选逻辑早退分支（Guard Before Early-Exit）。
- 纯元数据/零行 Patch 隔离：解析器若放宽零行文件（`allow_no_hunk=True`），须强制伴随“底层结构禁绝文本引用（ungrounded）”断言；计数/切片入参须以 `type(val) is not int` 严防 `bool` 隐式转换伪装。
- 双轨评分与高分审视：增量+整体双轨；历史 major 未清零上限 8；quick 上限 8；10/10≠无风险，须主动查找安全/测试遗漏。
- 批准与增量规范：approve 须声明未解决问题；request_changes 对应 major/blocking；历史 major 未解决保持阻断，已闭环须结案并触发全库引用搜索；无新证据不重复报告历史未变代码项。

## 4. 硬规则（重点）
- CI 与语法防线：CI 失败标 error，合并前须全绿；workflow 权限最小化；CI 最早期必须全库 `py_compile` + `import-all`，语法/导入错误零容忍；语法判断以 Python 3.14+ 为准。
- 优雅降级与防御文案：宽泛捕获/回退机制（Fallback Recovery）中严禁对未明确验证的因果做出确定性断言（如区分 404 与 5xx/Timeout），模型/用户提示须使用条件式/可能性表述；Tool 返回结构化 recovery（含 action/retry_arguments）引导 LLM，Recovery 提示文案需与单测断言解耦。
- 第三方日志收敛与开关语义：第三方库（如 APScheduler, httpx）默认 INFO 日志须纳入 `logging_bridge` 噪音过滤，保留 WARNING/ERROR 并补全正负向单测；布尔配置与实际执行逻辑必须保持 UI/文档与行为一致。
- PEP 758 语法识别：Python 3.14+ 允许不带 `as` 的多异常捕获省略括号，`except TypeError, ValueError:` 与 `except (TypeError, ValueError):` 等价。禁止将其识别为“Python 2 遗留语法”、“硬 SyntaxError”或据此阻断合并。误报防复发按当前运行时复核，带 `as` 时仍须为 `except (A, B) as e:`。
- 并发与锁生命周期：并发所有权锁 (Ownership Lock) / Users 计数器递增必须在进入 acquire 等待队列前同步完成，清理/删键严格约束为 `users == 0`，禁止持有者 release 即删键，防止交接期竞争造成分裂锁。
- 异步与数据迁移：async 路由同步 I/O 用 `asyncio.to_thread()`；大数据去重/清理迁移必须下推至 DB（配多方言 AST 编译断言防 MySQL 1093 错误），禁用全表拉入内存。
- 契约与防御：防御性捕获需收敛为底层基类异常（如 `SQLAlchemyError`）且显式标记 best-effort，严禁裸 `except Exception:`；底层 AI 工具错误须返回结构化 recovery（含 action/retry_arguments），防纯文本重试消耗 Token。
- 依赖与格式：pyproject/requirements/uv.lock 同步（CI `uv sync --locked` 或 `uv lock --check`）；修改声明文件必须同 PR 更新 `uv.lock`，Dependabot 等机器人 PR 遗漏锁文件时需阻断（Fail-Closed）并指明运行 `uv lock` / `uv lock --upgrade-package <pkg>`；静态检测排查闲置依赖，升级核对 Release Notes；键值/元组契约变更需防范调用方无条件解包 TypeError 及“三态归一”（混淆拒绝与网络异常）隐患。

## 5. Agent/Worker/Issue 治理
- Worker 与恢复机制：禁用无日志无退避的异常忽略；通知与任务恢复循环中禁绝尾部 `asyncio.sleep(0)` 死循环；统一复用 `_cancel_events` 幂等取消。
- Epic 级 Issue 拆解：兼具架构演进与 Bug 修复的 Epic，须强制拆解“即时修复项（Quick Wins）”与“阶段性目标”双轨推进；重复检测严格区分整体架构提案与单业务域 Issue，保留规范标题防无谓改写。

## 6. 知识库与集中配置
- `.sakura/` 增量追加或精确修改，禁止覆盖式重写。
- 行号白名单改用语义化标记；单点真值源。配置集中 `backend/core/config.py`，新配置 env+文件双入口并 CI 校验。

## 7. 最新反思要点
- PR653~PR655：Dependabot 等机器人升级 PR 缺少 `uv.lock` 更新引发 CI `uv sync --locked` 崩溃，必须严格 Fail-Closed 阻断并提示使用 `uv lock` / `uv lock --upgrade-package <pkg>` 精准补全。
- PR649 (incr2)：Tool 优雅降级与防御性重试中防范状态混淆（网络超时等误判断为资源已删除），结构化 Hint 设计保持客观条件式表述，解耦 Recovery 提示文案与单测断言。
- ISSUE650：APScheduler 第三方心跳日志噪音收敛至 `logging_bridge`（保留 WARNING/ERROR）；保持配置开关与定时器运行语义的一致性。
- PR644/ISSUE645/646：零行补丁 ungrounded 断言、并发 Ownership 锁移交原子计数、DB 迁移 AST 编译断言、Epic 拆解与三态区分。

## 8. 技术栈
FastAPI (Python 3.14+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · 多协议 AI（OpenAI / Anthropic / Gemini / 兼容） · MySQL 8.0 + Redis + ChromaDB · GitHub App + OAuth · Docker Compose

*最后更新：整合 PR653~PR655、PR649_incr2、ISSUE650 最新反思，精确标注累计反思 12 次*