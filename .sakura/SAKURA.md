# Sakura AI Reviewer 概述

## 1. 项目简介
基于 AI 的智能 GitHub Pull Request 代码审查与 Issue 分析机器人，具备主动探索代码库的能力。

## 2. 仓库信息
- 仓库名: Sakura520222/Sakura-AI
- 语言统计: Python: 8744585, HTML: 1130029, Shell: 357518, Dockerfile: 3135
- 累计反思 11 次

## 3. 核心审查原则
- 完整性验证：PR 描述/提交/文件/diff 一致核对；功能逐项勾选，差异 >10% 标 minor。
- 删减与配置核验：批量清理逻辑后须跑格式化（如 `ruff format`）；移除配置项须核验 `config.py`、`DYNAMIC_CONFIG_GROUPS`、`templates/`、`translations/`、`.env` 五要素；校验路径遵从代码引用 -> 配置声明 -> UI 渲染 -> i18n -> CI/部署环境；配置合法性校验物理前置于任何空数据/空候选逻辑早退分支（Guard Before Early-Exit）。
- 纯元数据/零行 Patch 隔离：解析器若放宽零行文件（`allow_no_hunk=True`），须强制伴随“底层结构禁绝文本引用（ungrounded）”断言；计数/切片入参须以 `type(val) is not int` 严防 `bool` 隐式转换伪装。
- 双轨评分与高分审视：增量+整体双轨；历史 major 未清零上限 8；quick 上限 8；10/10≠无风险，须主动查找安全/测试遗漏。
- 批准与增量规范：approve 须声明未解决问题；request_changes 对应 major/blocking；历史 major 未解决保持阻断，已闭环须结案并触发全库引用搜索；无新证据不重复报告历史未变代码项。

## 4. 硬规则（重点）
- CI 与语法防线：CI 失败标 error，合并前须全绿；workflow 权限最小化；CI 最早期必须全库 `py_compile` + `import-all`，语法/导入错误零容忍；语法判断以 Python 3.14+ 为准。
- 锁文件同步强约束：更新依赖清单（`pyproject.toml`/`requirements.txt`）必须同 PR 同步落库 `uv.lock`；Dependabot 等自动 Bot 发起的升级若缺失 `uv.lock`，直接 Fail-Closed 阻断，优先推荐 `uv lock --upgrade-package <pkg>`。
- PEP 758 语法识别：Python 3.14+ 允许不带 `as` 的多异常捕获省略括号，`except TypeError, ValueError:` 与 `except (TypeError, ValueError):` 等价。禁止将其识别为“Python 2 遗留语法”、“硬 SyntaxError”或据此阻断合并。误报防复发按当前运行时复核，带 `as` 时仍须为 `except (A, B) as e:`。
- 并发与锁生命周期：并发所有权锁 (Ownership Lock) / Users 计数器递增必须在进入 acquire 等待队列前同步完成，清理/删键严格约束为 `users == 0`，禁止持有者 release 即删键，防止交接期竞争造成分裂锁。
- 异步与数据迁移：async 路由同步 I/O 用 `asyncio.to_thread()`；大数据去重/清理迁移必须下推至 DB（配多方言 AST 编译断言防 MySQL 1093 错误），禁用全表拉入内存。
- 契约与防御：防御性捕获需收敛为底层基类异常（如 `SQLAlchemyError`）且显式标记 best-effort，严禁裸 `except Exception:`；底层 AI 工具错误须返回结构化 recovery（含 action/retry_arguments），防纯文本重试消耗 Token。Recovery/Retry 载荷参数必须使用本地物理钳制后的生效值（`effective_*`）。若降级无法区分 404 与 5xx/Timeout，文案须用可能性表述，不得虚假断言“已被删除”。

## 5. Agent/Worker/Issue/日志治理
- Worker 与恢复机制：禁用无日志无退避的异常忽略；通知与任务恢复循环中禁绝尾部 `asyncio.sleep(0)` 死循环；统一复用 `_cancel_events` 幂等取消。
- Epic 级 Issue 拆解：兼具架构演进与 Bug 修复的 Epic，须强制拆解“即时修复项（Quick Wins）”与“阶段性目标”双轨推进；重复检测严格区分整体架构提案与单业务域 Issue，保留规范标题防无谓改写。
- 日志收敛与过滤：第三方库（如 APScheduler）后台心跳/轮询过程日志必须接入 `logging_bridge` 屏蔽（保留 WARNING/ERROR），禁止日志污染。日志过滤测试须同时补全正向（高级别正常输出）与负向（心跳拦截）断言。

## 6. 知识库与集中配置
- `.sakura/` 增量追加或精确修改，禁止覆盖式重写。
- 行号白名单改用语义化标记；单点真值源。配置集中 `backend/core/config.py`，新配置 env+文件双入口并 CI 校验。

## 7. 最新反思要点
- PR655/PR653：自动化依赖 Bot 更新清单时容易遗漏 Lockfile。采用 `uv sync --locked` 校验的项目必须强制 Lockfile 同步，缺者直接 Fail-Closed 阻断。
- PR649（incr1~2）：Agent 工具 Recovery 载荷参数需解耦并物理钳制（避免透传原始未钳制值）；错误降级文案解耦且避免对未知原因做确定性断言。
- ISSUE650：后台常驻定时器过程日志收敛，防止污染运维日志；`logging_bridge` 补充负向与正向断言测试。

## 8. 技术栈
FastAPI (Python 3.14+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · 多协议 AI（OpenAI / Anthropic / Gemini / 兼容） · MySQL 8.0 + Redis + ChromaDB · GitHub App + OAuth · Docker Compose

*最后更新：基于 PR655/PR653/PR649/ISSUE650 等最新反思整合，精确标注累计反思 11 次*