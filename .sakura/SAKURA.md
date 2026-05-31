# Sakura AI Reviewer 项目概述

## 1. 项目简介
基于大语言模型的智能 GitHub PR 代码审查、Issue 分析、仓库扫描与 Agent 自动修复平台，具备主动探索代码库和 `.sakura/` 项目记忆能力。

**技术栈**：Python 3.11+、FastAPI、Jinja2/HTMX/Alpine、MySQL、Redis、ChromaDB、Docker、GitHub App/OAuth、Telegram Bot

## 2. 架构设计
- 分层架构：API/Webhook/WebUI、服务层、模型/存储层分离。
- 配置体系：WebUI 动态配置优先，Settings 默认值兜底；用户配置可覆盖部分全局偏好。
- AI 审查引擎：工具调用、RAG、代码索引、历史上下文、compact diff 大 PR 模式。
- Agent 架构：`SakuraAgentBase` 会话循环 + 具体 Agent 子类，受控工作区、工具执行、PR 创建闭环。
- WebUI 实时链路：Redis Pub/Sub → SSE → 前端；属于运行时稳定性热点。
- 安全与计费：MFA/Passkey、安全审计、配额、外部支付与退款闭环。

## 3. 仓库信息
- 仓库名: Sakura520222/Sakura-AI-Reviewer
- 语言统计: Python: 2931333, HTML: 842114, Shell: 16140, Dockerfile: 1637
- 累计反思 385 次

## 4. 审查核心原则
- 行数少 ≠ 风险低；公共依赖、基类、长循环、安全参数、支付/计费变更必须扩大关联扫描。
- 增量审查先校验 PR 描述、提交信息、文件清单、实际 diff 是否一致；描述漂移必须指出。
- `request_changes` 必须对应明确、可执行、与本 PR 相关的 major/blocking 问题；无 major 不得阻断。
- 历史问题只能在有提交、文件位置或重新扫描证据时标记“已修复”；否则写“本轮未验证”。
- 主要评论只发布具体可操作问题；正向评价、摘要、“critical: 无”等不得生成结构化评论。

## 5. 硬规则（major/critical）
1. async 路由中的同步 I/O 必须 `asyncio.to_thread()` 包装。
2. 函数体内延迟导入必须注释原因；模块级异常类导入不机械套用该规则。
3. 配置废弃必须从代码、WebUI、动态配置键、文档/测试全链路清理。
4. 新增状态枚举必须检查 DB ENUM/迁移、历史数据、API、前端显示。
5. 测试函数必须以 `test_` 命名；安全/状态码修复必须有行为测试。
6. 数据库迁移、依赖/驱动切换、支付状态变更必须输出兼容性与事务检查。
7. 未修复历史问题必须关联 issue 或明确后续跟踪。
8. 同一问题严重程度必须唯一；存在未修复 major 时不得直接 approve，除非说明非阻断依据。

## 6. 长连接与异常处理规则
- SSE、WebSocket、Redis Pub/Sub、消息队列、后台 worker 等长期循环中，禁止 `except ...: continue` 无日志、无退避、无退出条件。
- 内层只处理明确可恢复的空闲超时；连接断开、BrokenPipe、ConnectionReset、宽泛 `OSError/Exception` 必须传播到外层重连/退避逻辑。
- 若外层已有重连/资源重建/指数退避，内层新增异常处理必须证明不会绕过外层恢复机制。
- 高频预期异常单次可 `debug`，连续失败、触发重连、状态降级必须 `warning`。
- 第三方异常继承关系必须验证 MRO，禁止仅凭名称推断。
- 修改 SSE/PubSub 循环必须检查取消传播、客户端断开、unsubscribe/close/aclose、重新订阅与 tight loop CPU 风险。

## 7. FastAPI/WebUI 安全规则
- CSRF、认证、权限参数不得依赖 `Form(...)` / `Header(...)` 必填校验来表达安全失败；若期望 401/403，应使用默认值并在依赖内显式校验。
- 区分表单 CSRF `require_csrf` 与 Header CSRF `require_csrf_header`：缺失 header 若使用 `Header(...)` 会在 HTTP 参数解析层返回 422。
- 安全依赖测试分两层：直接函数测试覆盖空/无效/有效值；TestClient 覆盖缺失字段/header 的真实状态码。
- WebUI CSRF 应优先统一走依赖注入；禁止新增路由直接调用 `validate_csrf_token()`，除非注释说明。
- 认证相关路由（登录、登出、2FA、安全设置、API key）需检查 CSRF、session、redirect、状态码、错误响应泄露。

## 8. Agent/Shell 工具安全规则
- Agent 基类核心控制流变更必须列出所有调用方，检查返回值、异常传播、日志、循环结束、副作用语义。
- Agent conversation 推荐 `[system, user]` 起始消息；仅靠 system prompt 启动任务需说明并测试消息序列。
- LLM 失败、空响应、最大迭代、重试/降级路径的日志语义属于可观测性契约，应测试关键日志存在/不存在。
- Shell 白名单不等于整条命令安全；必须检查 `$()`、反引号、process substitution、`;`、`&&`、`||`、`|`、后台 `&`。
- Shell/Agent tool 输出必须双层限额：执行阶段防内存膨胀，进入模型上下文前防 token/成本/注入风险。
- 日志中的命令、URL、header、环境变量必须默认脱敏并限长，避免 token/password/key/secret 泄露。

## 9. 依赖、框架与模板规则
- 框架 API 迁移不仅搜索旧调用点，还必须确认新实参上下文存在、最低版本满足、主框架/传递依赖兼容。
- PR 描述声称修改 `requirements.txt`、配置、迁移、文档、测试但 diff 不存在对应文件，至少标 minor；若是核心修复则 major。
- FastAPI/Starlette/Pydantic 等强耦合依赖需检查兼容矩阵、Docker/fresh install、锁文件或约束。
- WebUI 模板响应优先使用统一 `render_template()` / `error_page()`；直接 `TemplateResponse()` 需确认 HTMX 片段等合理例外。

## 10. 测试与报告质量
- 安全解析辅助函数必须直接单测边界矩阵，不能只依赖集成测试。
- 机械性 API 替换需同时验证旧调用点清零和新增参数类型/可用性。
- 新增测试断言应覆盖关键字段和失败消息；但缺消息通常是 minor，不应误标 major。
- 审查报告避免“完美解决”“无遗留风险”等绝对化表述，改用“已解决当前已知关键问题”。
- 重复根因合并为一个评论，减少噪音；评分、严重程度、最终决策必须一致。

## 11. 最新反思沉淀（PR #363–#372）
- PR 描述漂移是高频漏检：SSE/依赖、CSRF、TemplateResponse、Agent 变更均出现描述与 diff 不一致，必须前置校验。
- Redis Pub/Sub/SSE 是运行时稳定性热点，重点审查异常分类、退避、资源释放、取消传播、重连与日志频率。
- FastAPI 安全状态码修复必须验证真实 HTTP 行为，区分框架 422 与业务 403。
- ShellTool 输出、日志和命令解析都是安全边界；完整命令日志与无限制 tool 输出应按安全风险审查。
- Agent 会话循环的小控制流改动（如 `break`→`return`）也会改变日志/状态契约，需扫描子类和调用方。
- 审查系统自身输出需质控：非问题不得标 critical，major 与 approve/request_changes 不得矛盾。

*最后更新：基于 PR #363–#372 增量审查经验，累计反思 385 次*