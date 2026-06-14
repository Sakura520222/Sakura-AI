# Sakura AI Reviewer 项目概述

## 1. 项目简介
基于大语言模型的智能 GitHub PR 代码审查、Issue 分析、仓库扫描与 Agent 自动修复平台，具备主动探索代码库、RAG/代码索引、历史上下文和 `.sakura/` 项目记忆能力。

**技术栈**：Python 3.11+、FastAPI、Jinja2/HTMX/Alpine、MySQL、Redis、ChromaDB、Docker、GitHub App/OAuth、Telegram Bot

## 2. 架构设计
- 分层架构：API/Webhook/WebUI、服务层、模型/存储层分离；API 与 WebUI 常共享业务数据，公共构造逻辑宜进入 service/helper，而非继续膨胀 `main.py`。
- 配置体系：WebUI 动态配置优先，Settings 默认值兜底；用户配置可覆盖部分全局偏好。
- AI 审查引擎：工具调用、RAG、代码索引、历史上下文、compact diff 大 PR 模式。
- Agent 架构：`SakuraAgentBase` 会话循环 + 具体 Agent 子类；受控工作区、工具执行、PR 创建与审查反馈迭代闭环。
- WebUI 实时链路：Redis Pub/Sub → SSE → 前端；长连接、后台 worker、webhook 状态机是稳定性热点。
- 安全与计费：MFA/Passkey、安全审计、配额、外部支付与退款闭环。

## 3. 仓库信息
- 仓库名: Sakura520222/Sakura-AI-Reviewer
- 语言统计: Python: 3091665, HTML: 888699, Shell: 16140, Dockerfile: 1637
- 累计反思 425 次

## 4. 审查核心原则
- 行数少 ≠ 风险低；公共依赖、基类、长循环、安全参数、支付/计费、DB schema、webhook/worker 状态机必须扩大关联扫描。
- 审查开始先核对 PR 描述、提交信息、文件清单、实际 diff；描述声称文档/迁移/测试/依赖/API/health 已更新但 diff 无证据，至少标 minor，核心上线条件缺失可 major。
- 增量审查必须声明范围：本轮验证了什么、未重新验证什么；历史问题只有在有提交/文件位置/重新扫描证据时才写“已修复”，否则写“本轮未验证”。
- `request_changes` 必须对应明确、可执行、与本 PR 相关的 major/blocking；approve 时不得残留未解释的 major/critical。
- 结构化评论只发布具体、可定位、可操作问题；摘要、评分、正向评价、checklist、“critical: 无”等不得转成 review comment；同一根因只保留一条。

## 5. 硬规则（major/critical）
1. async 路由中的同步 I/O 必须 `asyncio.to_thread()` 包装。
2. 函数体内延迟导入必须注释原因；若路由/服务依赖 `backend.main`，优先考虑抽到 `core/runtime.py`、`services/system_info.py` 等独立模块。
3. 配置废弃必须从代码、WebUI、动态配置键、文档/测试全链路清理。
4. 新增状态枚举/任务状态必须检查 DB ENUM/迁移、历史数据、API、前端显示、状态转移矩阵。
5. 数据库模型新增列、索引、唯一约束、枚举值必须检查迁移、nullable/default、历史数据、事务与并发冲突处理。
6. 测试函数必须以 `test_` 命名；安全/状态码/schema/观测 API 修复应有行为测试。
7. 支付、计费、依赖/驱动切换、DB schema 变更必须输出兼容性、事务与回滚检查。
8. 同一问题严重程度必须全篇唯一；硬规则若降级为非阻断，必须解释业务约束或防护依据。

## 6. 长连接、Webhook 与状态机规则
- SSE、WebSocket、Redis Pub/Sub、消息队列、后台 worker 长循环禁止 `except ...: continue` 无日志、无退避、无退出条件。
- 内层只处理明确可恢复的空闲超时；连接断开、BrokenPipe、ConnectionReset、宽泛 `OSError/Exception` 必须传播到外层重连/退避。
- webhook 变更固定检查签名验证覆盖新增分支、action 过滤、缺字段安全失败、重复投递幂等、乱序/延迟事件、sha/timestamp/status 防护。
- worker/task/status 变更需列状态转移矩阵：进入、退出、失败、取消、重复事件、乱序事件、终态资源清理。
- `_cancel_events`、`_background_tasks`、pending prompts 等资源字典新增 early return/finally 时必须检查清理路径；finally 内 await/DB 更新也需防止掩盖原异常。

## 7. FastAPI/WebUI/观测 API 规则
- CSRF、认证、权限参数不得依赖 `Form(...)` / `Header(...)` 必填校验表达安全失败；期望 401/403 时用默认值并在依赖内显式校验。
- 新增 `backend/webui/routes` 下 API 必须确认函数级、router 级、include_router 级、中间件级认证链路；未确认时写“需确认”，不要直接定性漏洞。
- `/health`、版本化 health、Dashboard/system-info 字段变更需检查字段同步、信息暴露、外部监控兼容、OpenAPI/文档/测试。
- 启动/运行时间：duration/latency 用 `time.monotonic()`/`perf_counter()`，绝对时间用 timezone-aware datetime；字段名区分 started/completed/duration/uptime；多 worker 语义需说明。
- `app.state` 读取用 `getattr(..., None)` 防御；前端 fetch 需处理非 2xx、失败、null/undefined、认证失效，渲染优先 `textContent`。
- WebUI 新字段必须检查 i18n；装饰 SVG 默认 `aria-hidden="true" focusable="false"`。

## 8. Agent/Shell 工具安全规则
- Agent 基类核心控制流变更必须列出所有调用方，检查返回值、异常传播、日志、循环结束、副作用语义；`break`/`return` 可能改变日志和状态契约。
- Agent conversation 推荐 `[system, user]` 起始消息；仅靠 system prompt 启动任务需说明并测试消息序列。
- LLM 失败、空响应、最大迭代、重试/降级路径的日志语义属于可观测性契约，应测试关键日志存在/不存在。
- Shell 白名单不等于整条命令安全；必须检查 `$()`、反引号、process substitution、`;`、`&&`、`||`、`|`、后台 `&`。
- Shell/Agent tool 输出必须双层限额：执行阶段防内存膨胀，进入模型上下文前防 token/成本/注入风险。
- 日志中的命令、URL、header、环境变量必须默认脱敏并限长，避免 token/password/key/secret 泄露。

## 9. 依赖、格式化与模板规则
- 框架 API 迁移不仅搜索旧调用点，还必须确认新实参上下文存在、最低版本满足、主框架/传递依赖兼容。
- FastAPI/Starlette/Pydantic 等强耦合依赖需检查兼容矩阵、Docker/fresh install、锁文件或约束。
- WebUI 模板响应优先使用统一 `render_template()` / `error_page()`；直接 `TemplateResponse()` 需确认 HTMX 片段等合理例外。
- 前后端重复格式化时间、金额、大小、百分比、状态文案时，必须核对单位、round/floor/int、边界值、空值；展示精度差异先判断是否同一业务链路/契约，通常 minor/suggestion。
- 私有同名工具函数（如 `_format_duration`）需按模块、调用方、展示场景核对，不得只凭函数名判定一致性。

## 10. 测试与报告质量
- 安全解析辅助函数必须直接单测边界矩阵，不能只依赖集成测试。
- fake/mock 行为必须贴近生产查询；复杂 SQL fake 应补轻量集成测试或真实 sqlite session。
- 格式化函数、观测 API、状态机、webhook 幂等适合补边界测试：0/None/falsy、<1s、60s、乱序、重复、唯一约束冲突。
- 新增测试断言应覆盖关键字段和失败消息；但缺消息通常是 minor，不应误标 major。
- 审查报告避免“完美解决”“无遗留风险”等绝对表述；评分、严重程度、最终决策必须一致。

## 11. 最新反思沉淀（PR #386–#387 协议重构与健壮性增强）
- PR #386 配置刷新审查：`_resolve_provider_credentials` 动态 `key_name` 必须白名单校验；`EmbeddingService.close()` 遍历资源关闭必须 `try...finally` 确保单个失败不影响剩余；配置变更比较（元组/字典）须注释字段顺序或改用 dataclass `__eq__`。
- PR #387 协议重构核心洞察：从 JSON+emoji 解析迁移到严格 `<SAKURA_REVIEW>` 标签协议是架构级变更，审查须提供新旧协议映射、旧协议全场景排查、废弃策略；`_extract_unique_envelope` 全局定位+原子提取替代"假设首尾"是健壮性关键提升。
- 协议/格式重构审查规则：(1) 新旧协议明确映射；(2) 旧协议全场景排查；(3) 废弃策略或兼容层说明。删除旧测试前必须建立"功能已转移"的可追溯证据链。
- 重构类变更必须声明"行为契约"变更：以对比表格列出每种输入在新旧行为下的差异（如 minor/suggestion 从仅标题计数变为显示最多3条具体内容），评估下游消费者影响。
- 重构 PR 完成度声明：架构迁移类 PR 审查时必须明确"本次完成目标的哪部分"和"已知遗留技术债"，避免部分重构带来持续混乱。
- 修复操作"安全性/无损性"验证：涉及数据清洗、格式转换或协议重构时，测试必须断言核心业务数据完整性（如 findings 标签数量不变）。
- 决策引擎作为核心枢纽变更审查：`decision_engine.py` 变更须强制扫描所有调用方（review_worker/template_builder/API）是否适配新数据语义；"数据保留/镜像"模式须配套废弃计划。
- 防御性冗余代码审查：同一验证逻辑多处出现时须评估是否提取公共函数、维护一致性风险、性能影响。防御性过滤/验证必须测试空列表边界。
- 历史问题长期未修复升级机制：跨越 3 轮及以上审查仍未修复的 suggestion 应标注为"技术债"并建议创建 Issue 跟踪（如 `prompt_builder.py:59` 的 `int()` 冗余调用）。
- 配置降级路径必须有 warning 日志：任何 `except` 块内静默回退，除非有明确注释说明原因，否则必须记录 `logger.warning`，这是可观测性硬规则。
- 重构完整性验证：全局性模式替换（如 emoji 常量集中化）审查时，要求作者提供全仓静态搜索证据（grep/lint），仅检查 diff 中显式修改文件不够——diff 只显示部分行，同类遗漏可能藏在未显示的上下文中。
- `output_lang` 冗余赋值语义审查：同一变量连续赋值使用不同条件（`or ''` vs `is not None`）须验证语义一致性，避免隐蔽 bug。
- 审查评分应反映交付整体完成度：高分 PR 仍可能存在重构不彻底、测试有效性不足、技术债累积等问题，评分须综合考虑"当前变更质量"与"代码库整体健康度"。
- 展示逻辑变更（HTML/Markdown 折叠）须验证用户可见性影响：`<details>` 块引入是否改变前端交互界面，是否有工具链依赖评论结构。
- 配置解析函数责任边界：配置解析应尽量做纯函数，参数契约（如 `key_name` 是否必须在预定义列表）须明确声明。
- 增量审查"完整性幻觉"防范：对于"清理/统一"类 PR，即使只看增量也应检查同类问题是否残留；审查深度随迭代衰减是系统性风险，须引入"增量风险清单"模板强制包含：历史问题复核、隐式契约影响评估、测试有效性验证。
- 技术债跟踪机制：对反复出现（≥3轮）的 suggestion 自动创建 issue 并标记 `debt` 标签，避免审查意见被淹没。
- 测试文件显著变动（如 +41/-4）是重要审查信号：必须覆盖测试变更的有效性分析，评估是否正确验证了生产代码新行为。

*最后更新：基于 PR #386–#387 协议重构与健壮性增强增量审查经验，累计反思 425 次*
