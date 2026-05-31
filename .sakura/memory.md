# 项目记忆

累计反思 395 次

## 常见代码问题和审查要点

- **PR 描述与 diff 一致性强制校验**：审查开始前必须核对 PR 描述、提交信息、文件清单和实际 diff；描述提到依赖/配置/迁移/测试/文档但 diff 未体现，至少标 minor；若是 Issue 核心或安全/数据库/依赖变更，标 major。
- **审查决策与严重程度一致性**：`request_changes` 必须对应至少一个明确、可执行、与本 PR 相关的 blocking/major 问题；“无重要问题/仅建议/高分”不得 request_changes。正向结论、摘要、文件列表不得标 critical/major。
- **历史问题状态需证据**：增量审查中不能把未重新验证的历史问题写成“已修复”；标记修复必须给出代码位置、提交或扫描依据，否则说明“本轮未验证”。
- **结构化评论质量**：只有具体、可定位、可操作的问题才能生成 review comment；报告摘要、评分、表格片段、“critical: 无”等不得转成评论；同一根因合并，避免重复 major。
- **公共/基类控制流变更调用方扫描**：修改基类核心循环、公共方法、依赖函数时，即使 diff 很小，也必须列出调用方并检查返回值、异常传播、日志、副作用、清理、状态更新语义，标 major。
- **大型删除/重构 PR 检查清单**：验证删除符号无残留引用、配置/i18n/模板同步清理、死代码扫描、相关模块规则统一应用。
- **配置生命周期管理**：新增/废弃配置需检查前端、后端、文档、配置中心默认值一致；数值配置需 min/max；读取点需 None/非法值降级与 debug 日志。
- **DB schema 变更全链路审查**：新增列/索引/唯一约束/枚举值必须检查迁移文件、nullable/default、历史数据回填、约束冲突、索引命名跨库兼容，以及应用是否依赖迁移而非自动建表。
- **i18n 与 UI 完整性**：新增 UI 元素必须同步翻译文件；缺失标 major。
- **明文敏感字段存储禁止**：新增 DB 列存储 api_key/secret/token 必须加密，标阻断。
- **新增状态枚举全链路审查**：DB ENUM/迁移、历史数据回填、API 返回、WebUI/i18n/过滤器同步检查。
- **延迟导入注释**：函数体内 import 必须说明循环依赖原因，格式如 `# 延迟导入：避免 A → B 循环依赖`；模块级异常类 import 不机械套用该规则。
- **loguru 日志格式**：使用 `logger.info("xxx {}", value)`，避免 f-string；但 `{}` 内不要放条件表达式。建议用 lint/grep 自动检查。
- **高频异常日志分级**：预期且高频的可恢复异常单次可 debug；连续失败、状态降级、触发重连/补偿必须 warning；关键补偿失败需监控/告警。
- **第三方异常继承需验证**：涉及 redis/aiohttp/SQLAlchemy 等异常分类时，必须查源码或 MRO，禁止仅凭异常名推断继承关系。
- **测试断言规范**：关键业务和兼容性断言必须有失败消息；直接导入第三方测试依赖需在依赖文件显式声明；测试文件职责需匹配功能。

## 架构与模式审查要点

- **长连接监听循环异常分层**：SSE/WebSocket/Redis PubSub/消息队列/后台 worker 中，禁止 `except ...: continue` 无日志、无退避、无退出条件。内层只处理明确可恢复的 idle timeout；连接断开/OSError/ConnectionReset/BrokenPipe 应传播到外层退避重连。可能 tight loop 标 major/阻断。
- **内外层恢复机制一致性**：已有外层重连、资源重建、指数退避时，内层新增异常处理必须证明不会吞掉应由外层处理的异常；连续 timeout 达阈值应重新抛出或重建资源。
- **SSE/PubSub 生命周期检查**：修改监听循环需检查 pubsub 对象是否可复用、是否重新 subscribe、unsubscribe/close/aclose、客户端断开和 task cancellation 是否正常传播，避免后台任务泄漏。
- **异步路由同步 I/O**：异步路由中的同步 I/O 必须 `asyncio.to_thread()` 包装，标 major。
- **FastAPI 参数校验先于业务逻辑**：认证/CSRF/权限字段若期望 401/403，不应依赖必填 `Form(...)`/`Header(...)` 触发 422；应使用默认值或自定义依赖显式转换错误状态码。
- **WebUI/Dashboard API 认证链路**：新增 `backend/webui/routes` 下 API 时，必须检查函数级、router 级、include_router 级、中间件级认证；未确认全局认证时只写“需确认”，已确认无保护再按数据敏感度标 minor/major。
- **CSRF 依赖模式区分**：表单路由用 `require_csrf`，Header/JSON 路由用 `require_csrf_header`；审查需区分缺失、空值、无效值、有效值。`Header(...)` 缺失通常是 HTTP 层 422，若项目要求统一 403 需改依赖默认值。
- **安全依赖测试分层**：直接函数调用只能测依赖内部逻辑；涉及 `Header/Form/Query` 解析和状态码时，必须补 TestClient/HTTP 层测试。
- **安全依赖行为测试**：认证、CSRF、权限、登录、登出、2FA 等安全路由变更，需覆盖缺失/空/无效/有效 token 的真实响应；若 PR 目标就是修复状态码但无行为测试，可标 major。
- **Shell 安全校验必须覆盖隐式执行**：除 `;`、`&&`、`||`、`|`、`&`、反引号外，必须检查 `$()`、process substitution、重定向组合、quoted/escaped 边界；白名单命令名不等于整条命令安全。
- **ShellTool 输出双层限制**：执行阶段限制 stdout/stderr 防内存压力；进入 LLM/tool message 前统一预算和截断提示；日志避免记录超长或敏感输出。
- **日志中的用户输入脱敏**：Shell 命令、URL、header、环境变量等入日志前默认限长并脱敏 token/password/key/secret/Authorization。
- **Agent 会话起始消息规范**：所有 agent conversation 优先使用 `[system, user]` 起始模式；只靠 system prompt 启动任务需说明原因并测试消息序列。
- **Agent 基类循环日志语义**：LLM 错误/空响应提前结束不得误报“达到最大迭代”；错误处理、重试、最大迭代、降级路径的日志语义应测试存在/不存在。
- **框架 API 迁移依赖约束**：适配 Starlette/FastAPI 等新 API 时，必须检查 requirements/pyproject、最低/最高版本、传递依赖、FastAPI-Starlette 兼容矩阵、fresh install/Docker 解析。
- **模板渲染封装一致性**：WebUI 路由优先使用统一 `render_template()`/`error_page()`；直接 `templates.TemplateResponse()` 需确认 HTMX 片段等例外，并验证每个调用上下文都有 `request`。
- **依赖/驱动迁移清单**：如 `aiomysql`→`asyncmy`，需检查 SQLAlchemy dialect、连接 URL、连接池参数、事务/字符集/时区/SSL、Docker/CI、版本矩阵和最小连接测试。
- **运行时/健康信息暴露审查**：新增 startup/uptime/health/dashboard status 字段时，检查认证与信息泄露、字段契约/文档/OpenAPI、缺字段/None 降级、多 worker 语义、外部监控兼容；多个端点共享时优先公共 helper。
- **时间语义审查**：duration/latency/elapsed/timeout 必须用 `time.monotonic()`/`perf_counter()`；真实事件时间用 timezone-aware datetime/ISO8601（含 Z/offset）；字段名区分 started/completed/duration/uptime，禁止 wall clock 做耗时差。

## 支付与资源安全

- **支付事务边界**：任何支付状态变更必须评估与服务发放、配额消耗等关联操作的事务原子性或补偿机制，标 major。
- **外部回调安全降级**：验签失败或配置缺失必须返回明确 401/403，禁止静默放行，标阻断。
- **GET 请求禁止写操作**：RESTful GET 路由不得修改数据库状态，标 major。
- **支付网关启用开关**：所有支付网关公共函数入口必须检查对应网关启用状态。
- **Webhook 幂等性与时区**：验证重复事件处理；API 时间戳参数需显式时区。
- **金额解析语义**：金额解析函数应返回 `Optional[int]`，`None` 表示失败，避免 0 歧义；需覆盖零/负数/非法输入。

## 审查流程与质量控制

- **审查先验步骤**：先读 PR 描述与文件清单，再核对 diff；标出“描述提到但未体现”和“代码改了但描述未提到”。自动生成 PR 描述尤其要警惕过期/幻觉。
- **增量审查范围声明**：明确本轮只验证增量还是全量；历史上下文不能替代当前 diff 校验。对核心业务、基类、公共依赖、配置、安全、支付、长连接变更需执行关联扫描。
- **评分反映 PR 整体目标**：不能只因局部代码正确给高分；若依赖约束、描述、测试或验收目标未完成，应降分并明确风险。
- **严重程度唯一**：同一问题不得同时标 minor/major；若正文称 minor，结构化评论不得 major；存在未修复 major 且仍 approve 必须解释非阻断原因。
- **测试覆盖审查**：安全解析辅助函数、金额/URL 校验、异常分类、框架兼容性、公共依赖行为需直接单测或 HTTP/集成测试，覆盖边界和失败路径。
- **观测/展示类测试**：health/status/dashboard metrics、启动耗时、格式化函数等低风险功能也需轻量测试字段存在、类型、缺状态降级、时间/格式边界；无测试通常 minor/suggestion，不机械阻断。
- **机械性替换审查**：全仓搜索旧调用点只是第一步，还需确认新参数在每个上下文可用、类型正确、运行时路径和 mock 签名同步。
- **小 PR 不放松元信息检查**：即使只是 lint/loguru 清理，也要核对 PR 描述、关联 Issue、历史问题状态和最终决策是否一致。

## 经验教训与最佳实践

- **小控制流变更也是契约变更**：`break`→`return`、`continue`、异常捕获范围变化都可能跳过日志、清理、指标、状态落库或外层恢复逻辑。
- **日志是可观测性契约**：误报最大迭代、静默吞异常、完整敏感命令日志都会影响排障和安全；日志变更按功能/安全变更审查。
- **异常处理不是捕获越多越好**：关键是捕获更准；网络 timeout、连接重置、Broken pipe、认证/协议错误应分类处理。
- **健康检查建议需验证对象**：如 `redis_client.ping()` 未必验证 Pub/Sub 专用连接，提出前需确认连接模型。
- **统一模式不等于统一行为**：迁移到公共依赖或封装后，要检查外部状态码、响应体、前端调用和历史契约是否改变。
- **公共抽取需审依赖方向**：消除重复不等于放进入口模块；路由/服务反向依赖 `backend.main` 是架构信号，应优先抽到 core/runtime、services/system_info 等独立层，并检查循环导入和调用方契约。
- **结构测试不能替代行为测试**：检查路由挂了依赖有价值，但安全修复必须验证真实请求响应。
- **Shell/Tool 输出是 AI 上下文风险**：超长输出会增加 token 成本、上下文溢出、prompt injection 面积；应统一预算管理。
- **直接访问私有属性的测试可接受但脆弱**：优先构造函数注入、monkeypatch 公开初始化点或 fake client。
- **Fake/mock 需贴近生产语义**：SQLAlchemy fake session、API fake、LLM fake 必须模拟生产过滤/排序/limit/异常/返回结构；复杂查询宜补 sqlite/集成测试，否则测试可能比生产“更正确”。
- **格式化契约按上下文判断**：时间/金额/大小/百分比等前后端或跨模块重复格式化时，列边界值并核对单位、round/floor/int、空值/NaN/负数；只有同一业务对象/展示/API 契约差异才算缺陷，跨场景差异多为建议。
- **绝对化表述要谨慎**：避免“完美解决/无遗留风险”；更稳妥写“已解决当前已知关键问题，未发现新的阻断风险”。
- **事件驱动闭环先画状态/时序表**：webhook+worker+DB/Agent Team 类变更重点查状态进入/退出、重复/乱序/延迟事件、PR closed/synchronized/dismissed、并发更新、唯一约束冲突捕获和终态资源清理。
- **匹配键不能依赖隐含唯一性**：`repo+branch` 常不足以定位任务/PR（分支复用、fork、旧任务）；优先要求 pr_number、head_sha、status 或模型层 active 唯一约束，并在代码中说明业务保证。
- **前端轻量逻辑也有生命周期**：Dashboard/模板内 JS 的 fetch、setInterval、事件监听、SSE/WebSocket 要检查失败/空/认证失效降级、`textContent` 安全渲染、组件销毁清理，生命周期建议需符合项目框架版本。
