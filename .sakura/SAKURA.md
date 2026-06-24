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
- 语言统计: Python: 3194535, HTML: 900719, Shell: 16140, Dockerfile: 1637
- 累计反思 445 次

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
9. 提示词/配置文件中的输出格式约束如被下游解析代码隐式依赖，修改时必须追踪消费方验证隐式假设耦合，等同接口变更。
10. 新增 DB 表/索引必须对照同 PR 查询语句验证覆盖；状态机每个中间状态必须有超时/异常逃脱路径，无逃脱 = major。
11. 对等迁移（A→B）须双向行为验证——字段集、验证规则、降级条件逐项对比，禁止仅凭"风格一致"通过。

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
- 外部内容嵌入 GitHub markdown（如 suggestion 嵌入 comment）须评估 ``` 等语法元素干扰，防注入破坏渲染结构。
- AI 输出容错增强须补负面测试：错误/畸形输入不应被解析为有效结果。

## 10. 测试与报告质量
- 安全解析辅助函数必须直接单测边界矩阵，不能只依赖集成测试。
- fake/mock 行为必须贴近生产查询；复杂 SQL fake 应补轻量集成测试或真实 sqlite session。
- 格式化函数、观测 API、状态机、webhook 幂等适合补边界测试：0/None/falsy、<1s、60s、乱序、重复、唯一约束冲突。
- 新增测试断言应覆盖关键字段和失败消息；但缺消息通常是 minor，不应误标 major。
- 审查报告避免“完美解决”“无遗留风险”等绝对表述；评分、严重程度、最终决策必须一致。

## 11. 最新反思沉淀（PR #395–#403 增量审查队列、协议深化与提示词契约）
- PR #395 incr2 评论同步重构：字符串匹配关联删除是高危反模式；解耦操作须验证下游消费方适配（`issues` 字典行为变更）；"正确语义≠安全变更"——修正历史错误行为须验证下游隐式依赖；净减代码须区分"删除逻辑"还是"删除债务"。
- PR #396 审查协议容错增强：AI 输出容错须补负面测试（错误输入不应解析为有效）；Score clamping 是行为契约变更须验证边界；移除截断/限制等安全网须提供显式替代；Diff 解析功能须覆盖空/二进制/重命名/大文件；外部内容嵌入 GitHub markdown 须评估 ``` 注入；跨模块直通数据流是管道架构脆弱点。
- PR #397 Issue 分析协议迁移：对等迁移须双向行为验证（字段集、验证规则、降级条件逐项对比），禁止仅凭"风格一致"正面评价；防御性 `.get()` 掩盖字段缺失须标 minor；固定字符串 untrusted data 边界须验证注入逃逸；同源测试须在摘要中显式声明偏差风险；信封协议变更应有标准检查清单。
- PR #397 incr2 返回结构枚举化：早返回路径的字段集一致性须主动对比，不能依赖下游 `.get()` 容错；返回结构内部分类字段（`parse_source`/`error_type`）新增取值须列全已有取值并建议提取为常量或 Enum；增量审查应以新代码为线索重新审视旧代码完整性。
- PR #398 GitHub Checks 集成 Issue：集成类 Issue 降级策略是必问项；异步架构的状态可视化面临状态同步延迟；优先级应基于用户信任影响判定；状态枚举与第三方 API 模型对齐是非常规映射须指出。
- PR #399–#400 增量审查队列机制（核心架构变更）：数据源替换须三层检查——结构兼容性、消费方适配、行为规则迁移；"会话恢复"模式须验证 system prompt/tool 定义/运行时参数是否被当前配置覆盖；状态机中间状态（pending/reviewing）无逃脱路径 = major；新增核心服务文件（>200 行）须独立审查单元；大面积新增测试文件须抽样审查异常分支。
- PR #402 Webhook/队列集成：≥3 个≤10 行小变更文件须逐文件说明变更意图，删除行必须追溯；核心链路静默失败（数据丢失且无告警）至少 major；新增 DB 表须对照查询验证索引覆盖；低成本→高成本模型切换须论证必要性；PR 描述与 diff 量级差异（优化 vs 架构变更）应标 major；宽泛异常兜底日志须含异常类型或 `exc_info`。
- PR #403 提示词契约变更：提示词即接口契约，修改约束须追踪下游解析代码验证隐式假设耦合；"纯文本变更"≠零风险变更——被代码隐式依赖的文本格式约定修改与接口变更同级；全量重审应评估系统影响面而非仅文件数；approve 不等于沉默，零评论 approve 在审计追溯中等于"未审查"。
- 通用规则新增/强化：(1) 数据源-消费方联动删除须 grep 全仓逐项确认；(2) 替换式重构的半完成状态是高频 bug 根因；(3) 队列服务固定审查维度：入队幂等、消费并发、消息确认/重试、死信处理、积压监控；(4) 过滤操作须 debug 级输出被过滤元素标识；(5) 增量审查摘要须含"本轮增量范围说明"以消除决策完整性模糊性。

*最后更新：基于 PR #395–#403 增量审查队列、协议深化与提示词契约审查经验，累计反思 445 次*
