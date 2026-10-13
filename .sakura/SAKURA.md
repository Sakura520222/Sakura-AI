# Sakura AI Reviewer 概述

## 1. 项目简介
基于 AI 的智能 GitHub PR 代码审查与 Issue 分析机器人，具备主动探索代码库的能力。

## 2. 仓库信息
- 仓库名: Sakura520222/Sakura-AI
- 语言统计: Python: 8744585, HTML: 1130029, Shell: 357518, Dockerfile: 3135
- 累计反思 43 次

## 3. 核心审查原则
- 完整性验证：PR 描述/提交/文件/diff 一致核对；差异 >10% 标 minor。
- 删减与配置核验：批量清理后跑 `ruff format`；移除配置项核验 `config.py`、`DYNAMIC_CONFIG_GROUPS`、`templates/`、`translations/`、`.env` 五要素；校验遵从代码引用→配置声明→UI 渲染→i18n→CI/部署环境；配置合法性校验物理前置于空数据早退分支。
- 配置中心事实源：恢复默认值自省静态 Schema（Pydantic `model_fields[key].default`）防运行时单例污染；字段级 PATCH 防并发无序覆盖；`backend/core/config.py` 禁顶层静态导入外部依赖，动态选项惰性加载。
- 前端 CSS/UI 截断：移除 `line-clamp-*`/`truncate` 时核验父容器弹性高度、无残留二次截断、`whitespace-pre-wrap` 三要素。
- 双轨评分与批准：增量+整体双轨；历史 major 未清零上限 8；10/10≠无风险；approve 须声明未解决问题；历史 major 闭环须结案并全库引用搜索。

## 4. 硬规则（重点）
- CI 与凭据防线：CI 失败定 Major 须追至行号级证据（文件:行、退出码），合并前全绿；机器人写 Secrets 必用 `pull_request_target` 并校验 actor；严禁 PAT 拼入 push URL。新增 workflow 须核验 `permissions:` 最小权限、Secret 缺失降级、矩阵 fail-fast，并以首次绿色运行为合并前置。
- 依赖三重一致：`pyproject.toml`+`requirements.txt`+`uv.lock` 原子一致，遗漏定 Major；Dependabot 用 `uv lock --upgrade-package <pkg>` 单包升级防漂移。
- 路径与文件系统安全：Windows 根路径显式 `resolve()` 防 8.3 短路径脱靶；`normcase` 仅 Windows 转小写、POSIX（含 macOS APFS）恒等，防大小写别名绕过；Bootstrap Token 原子写（`mkstemp`+`os.replace`）+最小权限 `0600`+启动轮换、消费即焚。
- 日志脱敏热路径（Log Patcher Hot-Path Guard）：桥接层脱敏严禁单条日志内重复昂贵操作，规则须进程级缓存或绑定单条 Record 生命周期（依赖同步多 sink 语义；异步 sink/池化复用会跨记录泄漏密钥，须注释声明）；正则引入 DOTALL/未锚定 `.*?`/反向引用时须配病态输入基准测试或长度钳制防 ReDoS，推演结论须有测试锚点；`(?<!\\)` 识别不了偶数反斜杠，补 `\\"` 类边界断言。
- `.sakura` 记忆文件脱敏红线：反思/记忆文件提交前须扫描引用的日志片段、配置示例、diff 摘要无明文凭据或可逆前缀——审查覆盖必须显式包含 AI 自身产物。
- 审查过程纪律：增量审查过滤 reflection/merge 噪声提交并显式声明有效变更集；PR 分支 ≥3 次反向合入 main 时须声明并核验 diff 基线（三点 diff）；重复 suggestion 跨 ≥2 轮未处理须转独立 tracked issue 或要求作者显式回应，禁车轱辘重述。
- 其他：模板清洗与兜底追加须校验数据完备防脏数据逃逸，降级解析累加变量显式回滚；独立子包（`updater/`）公共测试工具自包含；PEP 758 多异常捕获非语法错误；`asyncio.shield` 排空捕获后须记日志重抛 `CancelledError`；并发锁清理仅限 `users == 0`。

## 5. 事件流与外部边界（PR678 新增）
- 外部控制反转值硬钳制：`Retry-After` 等不可信响应字段用于调度/延时须强制 `min(val, MAX)` 上限（≤1h）——不可信输入包括 Response 头，不仅是 Request。
- Provider 异常全翻译契约：严禁 TimeoutError/ConnectionResetError 等裸异常穿透至 Web 层，须收敛为领域瞬态/永久异常类型。
- 容量保护：订阅规则等扇出实体创建须配额上限，全量载入查询加 LIMIT 熔断，防内存扇出 DoS。
- 生命周期闭环：新增事件/投递/审计表须同步检查 Retention/Purge 策略，防存储膨胀与索引劣化。
- 竞态测试断言不变式：只断言计数、幂等性与终态一致性，禁断言事件顺序等 incidental 产物；播种 ORM fixture 可经状态 history 派生事件（单测/live-DB 语义分叉信号）。
- 阻断措辞须区分"证据待补型"与"缺陷型"阻断，防止评分被误读为质量恶化。

## 6. Agent/Worker/Issue 治理
禁无日志无退避的异常忽略；恢复循环禁尾部 `sleep(0)` 死循环；`_cancel_events` 幂等取消；两段式准入 `prepare_admission`+`admit_operation`，账单主体由服务端上下文覆盖；Epic 强制拆解即时修复与阶段目标，重复检测区分架构提案与单业务 Issue。

## 7. 知识库
`.sakura/` 增量追加或精确修改，禁止覆盖重写；行号改语义标记；新配置 env+文件双入口并 CI 校验。

## 8. 技术栈
FastAPI (Python 3.14+) · Jinja2 + Tailwind + HTMX + Alpine.js · 多协议 AI · MySQL 8.0 + Redis + ChromaDB · GitHub App + OAuth · Docker Compose

*最后更新：整合 PR676（记忆文件脱敏红线、建议升级机制、反向合并基线核验、record 缓存 sink 语义依赖）与 PR678（外部响应钳制、Provider 异常翻译、容量保护、生命周期闭环、竞态断言规范）反思，累计反思 43 次*
