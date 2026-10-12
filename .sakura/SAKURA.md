# Sakura AI Reviewer 概述

## 1. 项目简介
基于 AI 的智能 GitHub PR 代码审查与 Issue 分析机器人，具备主动探索代码库的能力。

## 2. 仓库信息
- 仓库名: Sakura520222/Sakura-AI
- 语言统计: Python: 8744585, HTML: 1130029, Shell: 357518, Dockerfile: 3135
- 累计反思 38 次

## 3. 核心审查原则
- 完整性验证：PR 描述/提交/文件/diff 一致核对；功能逐项勾选，差异 >10% 标 minor。
- 删减与配置核验：批量清理后跑格式化（如 `ruff format`）；移除配置项核验 `config.py`、`DYNAMIC_CONFIG_GROUPS`、`templates/`、`translations/`、`.env` 五要素；校验遵从代码引用 -> 配置声明 -> UI 渲染 -> i18n -> CI/部署环境；配置合法性校验物理前置于任何空数据早退分支。
- 配置中心事实源（Ground Truth）：恢复默认值严禁依赖运行时已被修改的单例对象，须自省静态 Schema（如 Pydantic `model_fields[key].default`）；字段级 PATCH 须防并发写无序覆盖；全局配置隔离：`backend/core/config.py` 禁顶层静态导入外部依赖模块，动态选项采用惰性加载。
- 前端 CSS/UI 截断核验：移除 `line-clamp-*` / `truncate` 时核验父容器弹性高度与 `mt-auto`、全域无残留二次截断、`whitespace-pre-wrap` 避免坍塌三要素；优先利用 Alpine.js 增强。
- 双轨评分与批准规范：增量+整体双轨；历史 major 未清零上限 8；quick 上限 8；10/10≠无风险，主动排查安全与测试遗漏；approve 须声明未解决问题；request_changes 对应 major/blocking；历史 major 闭环须结案并触发全库引用搜索。

## 4. 硬规则（重点）
- CI 与凭据防线：CI 失败标 error/major，合并前全绿；Dependabot 等机器人需写 Secrets 时禁绝 `pull_request`，必用 `pull_request_target` 并校验 `user.login == 'dependabot[bot]'`；严禁 PAT 拼入 git push URL；工作流变更补契约单测。
- 依赖声明与锁文件原子一致性（Fail-Closed）：
  - 实行 `pyproject.toml` + `requirements.txt` + `uv.lock` 三重强一致；清单修改同 PR 提锁更新，遗漏定级 Major 并 Request Changes。
  - Dependabot 依赖更新推荐单包最小化（`uv lock --upgrade-package <pkg>`），防全量 `uv lock` 引发依赖漂移与 `uv-lock-sync` 缺失静默跳过；子项目（`updater`/`sandboxer`）依赖下限全局对齐。
- 跨平台路径与文件系统安全防线：
  - Windows 路径规范化：涉底层句柄与准入校验时，根路径统一显式 `resolve()`，防 8.3 短路径与驱动器映射导致脱靶引发 `ELOOP`；白名单重定向单入口开启并拦截符号链接。
  - `os.path.normcase` 跨平台安全陷阱：`normcase` 仅在 Windows 下转小写，在 POSIX（含 macOS APFS）恒等。敏感文件准入若支持 macOS 宿主，须评估大小写不敏感别名绕过。
  - 引导凭据（Bootstrap/Setup Token）安全交接：原子写（`mkstemp` + `os.replace`）、最小权限（`0600` / Windows ACL）、启动轮换与消费即焚。
- 日志脱敏热路径防线（Log Patcher Hot-Path Guard）：
  - Loguru/logging 桥接层脱敏逻辑严禁在单条日志内重复执行昂贵操作（如无缓存全量扫 Settings/环境变量）；规则须在进程级缓存或绑定单条 Record 生命周期，防高吞吐 DEBUG 性能劣化；转义引号正则防范 ReDoS 与大文本回溯开销。
- 模板清洗与兜底追加原子性：模板剔除空字段且兜底追加时，必须校验数据完备性（如 `in target_container`），防半成品脏数据逃逸到页脚；异常降级解析时累加型变量须显式回滚。
- 子包隔离与语法识别：独立子包（`updater/`）严禁隐式依赖根目录 `conftest.py`，公共测试工具须自包含；PEP 758（Python 3.14+ 省略括号多异常捕获 `except A, B:`）严禁误报为语法错误。
- 异步排空与并发锁：`asyncio.shield` 排空耗时任务捕获底层异常须记日志再重抛 `CancelledError`；并发锁清理严格约束为 `users == 0`，禁止 release 即删键。

## 5. Agent/Worker/Issue 治理
- Worker 与恢复机制：禁用无日志无退避的异常忽略；任务恢复循环禁绝尾部 `asyncio.sleep(0)` 死循环；统一复用 `_cancel_events` 幂等取消。
- 两段式准入/补偿：分布式任务采用 `prepare_admission` + `admit_operation` 两段式状态机，记账防漏；客户端参数禁作为账单主体，由服务端上下文强行覆盖。
- Epic 级 Issue 拆解：兼具架构演进与修复的 Epic，强制拆解即时修复与阶段目标；重复检测严格区分架构提案与单业务 Issue。

## 6. 知识库与集中配置
- `.sakura/` 增量追加或精确修改，禁止覆盖式重写。
- 行号白名单改用语义化标记；配置集中 `backend/core/config.py`，新配置 env+文件双入口并 CI 校验。

## 7. 最新反思要点
- PR676：Loguru/logging 桥接脱敏热路径规则缓存防性能劣化；Bootstrap Token 文件交接最小权限（`0600`）与原子替换规范；多层转义引号正则 Fail-Closed 脱敏与 ReDoS 防御。
- ISSUE677：配置中心重置默认值必须自省静态 Schema（`model_fields`）作为不可变真理源，防运行时单例污染；字段级 PATCH 防范无序并发覆盖。
- PR675：自适应模板清洗与末尾兜底追加原子一致性，防半成品逃逸；降级解析累加型变量显式回滚。
- PR674：揭示 `os.path.normcase` 仅在 Windows 转小写而在 POSIX（含 macOS APFS）恒等的非对称安全陷阱；Windows 8.3 路径双向 `resolve()` 准入防线；子包自包含 `conftest.py` 隔离规范。
- PR669~PR671：依赖与锁强原子性；推荐 `uv lock --upgrade-package <pkg>` 最小升级；防范工作流静默降级。

## 8. 技术栈
FastAPI (Python 3.14+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · 多协议 AI · MySQL 8.0 + Redis + ChromaDB · GitHub App + OAuth · Docker Compose

*最后更新：整合 PR674~PR676 与 ISSUE677 关于日志脱敏热路径、Token安全交接、配置事实源与模板清洗原子性反思，精确标注累计反思 38 次*
