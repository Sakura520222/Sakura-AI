# Sakura AI Reviewer 项目概述

## 1. 项目简介与技术栈
Sakura AI Reviewer 是一款基于大语言模型的智能 GitHub 代码审查与 Issue 分析机器人，具备主动探索代码库、跨文件依赖理解和全仓库扫描能力，旨在自动化提升代码质量管理与日常协作效率。

- **后端语言**：Python 3.11+
- **Web 框架**：FastAPI
- **前端界面**：HTML（自包含 WebUI）
- **数据库**：MySQL（关系型存储）、Redis（Pub/Sub 实时通信与缓存）
- **AI 与检索**：LLM API 对接、RAG 向量语义检索、DuckDuckGo/Tavily Web 搜索
- **基础设施**：Docker、Docker Compose
- **代码规范**：Ruff（Linter 与 Formatter）
- **集成渠道**：GitHub App（Webhook/OAuth）、Telegram Bot

## 2. 架构设计与关键决策
- **分层架构**：API 路由 → 服务层 → 数据层，业务逻辑内聚于 `services/`
- **配置与代码分离**：标签策略、审查模式通过 `config/` 下 YAML 外部化
- **异步任务隔离**：耗时任务（仓库扫描、AI 推理）由 `workers/` 独立处理
- **降级链路设计**：真实 GitHub API → 本地适配器降级，遵循渐进增强原则
- **前后端一体仓**：WebUI 存放于 `backend/webui/`，由 FastAPI 托管静态资源
- **容器化交付**：Docker Compose 编排环境依赖与数据库初始化

## 3. 已知问题和注意事项
- **repo 对象无接口定义**：缺少 Protocol/ABC/文档定义，所有工具通过鸭子类型隐式依赖。删除实现无法静态发现影响，替换实现无法验证完备性。在 `sakura_memory_service.py` 全量审查时应评估引入接口定义的可行性。
- **适配器契约债务**：`local_repo_adapter.py` 曾连续 6 轮补边缘而 sha/url 契约未补齐，最终被删除后引入更差替代品（轻量 mock），问题未消失而是降级。
- **result_parser 与 decision_engine 隐式耦合**：解析结果结构直接影响决策引擎分支逻辑，`parse_source` 字段引入了多态解析路径标记，下游需感知此维度。
- **降级链状态污染风险**：JSON 解析失败降级到 emoji 解析时，中间变量可能未正确重置。

## 4. 审查中发现的重要模式
- **适配器选择性实现**：只实现搜索用到的接口而非全面契约，首次全量审查须逐一校验公共方法实现状态
- **异常语义混用**：用 NotImplementedError 表达"不支持"的降级意图，与 Python 原生语义冲突，应使用明确的自定义异常
- **防御性守卫的双重语义**：`isinstance` 检查可能同时承担隐式接口契约验证职责，移除时须评估防御流失
- **"假回滚"模式**：声称 revert 但混入新实现，破坏性回滚比破坏性新增更危险——审查者预期无害而放松警惕
- **动态类型反模式**：`type('Repo', (), {...})()` 绕过所有静态检查，是危险的 mock 方式
- **try/except 边界审计**：审查解析代码须标记所有可能抛异常的表达式（`.get()` 对非 dict、`int()` 对非数字、`for` 对非 iterable），确认异常点在预期范围内、捕获类型匹配

## 5. 团队约定和规范
- **代码风格**：Ruff 作为唯一检查和格式化工具，通过 `run_ruff.py` 统一执行
- **结构化数据防御**：处理外部 JSON 时每个字段的类型假设须有显式校验或独立 try/except 隔离，禁止依赖"上游 schema 已约束"跳过防御
- **类型假设显式化**：对列表、字典等复合类型须先 `isinstance` 校验再操作，禁止隐式假设
- **常量集中治理**：枚举值统一收归 `constants.py`，禁止散落硬编码
- **测试覆盖底线**：即使 quick 策略，新增测试文件须扫描负面用例、边界值用例，用例命名体现场景而非实现细节
- **评分决策解耦**：增量质量高≠可合并，未闭环 critical 时 decision 必须降级为 request_changes
- **遗留关注点必须触发**：变更文件命中遗留关注点时须显式确认状态（`[遗留确认] 文件名: 问题状态`），禁止静默跳过
- **Revert PR 必须追溯**：须对比目标 commit 与当前 diff 确认真实性，追溯原 PR 被回滚原因，未追溯时评分上限 7
- **quick 策略边界**：降低的是纯风格偏好建议粒度（命名、注释格式），不降低对已知遗留问题、防御缺口的敏感度
- **接口实现删除检查**：删除实现某接口的类时须列出所有消费者、逐一确认替换完备性，无法确认时按 critical 标记
- **文档双语化**：面向开源社区提供中英文双语 README 及详细文档

## 仓库信息
- 仓库名: Sakura520222/Sakura-AI-Reviewer
- 语言统计: Python: 1327103, HTML: 396178, Shell: 2790, Dockerfile: 862, url: https://api.github.com/repos/Sakura520222/Sakura-AI-Reviewer/languages
- 累计反思次数: 25（此数值已精确计算，必须原样使用，禁止自行加减或重算）