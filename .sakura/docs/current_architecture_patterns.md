# 架构观察：Runtime、Dashboard、Activity、Agent

## Runtime/System Info
启动耗时、版本、运行时长等不应由路由反向依赖 `backend.main` 提供。推荐依赖方向：

```text
core/runtime.py 或 services/system_info.py
  ↑ main.py 记录启动状态
  ↑ health/dashboard/webui 读取状态
```

这样可避免 `main -> include_router -> route -> main` 循环依赖，并集中维护字段契约。

## Dashboard 是横向聚合层
Dashboard 不是简单展示层，它聚合 Issue、Agent Team、Repo Scan、Token 等多模块数据。审查时按权限、口径、性能敏感模块处理：
- route 是否正确传递用户范围；
- service 是否强制应用 `scope_user`；
- `None` 表示不限制，空字符串不应绕过过滤；
- API 与 WebUI 相同统计应复用服务函数。

## Activity Checkpoint 架构
当前链路类似：

```text
Worker/Analyzer -> callback -> CheckpointService -> DB + SSE -> WebUI
```

它是可恢复状态同步系统，不是普通日志。关键约束：DB 与 SSE 顺序、REST snapshot 补偿、前端去重、权限在 snapshot 与 subscription 两端成立。

## Agent 模板方法
`SakuraAgentBase` 提供核心对话循环，具体 agent 实现任务。基类控制流变更会影响所有子类，应作为公共契约变更审查。

## 配置中心分散风险
配置项常需同步 Settings、动态配置 labels/ranges/options、WebUI route。长期建议引入单一配置 schema，减少人工"四一致"检查。

## Agent Team多Worktree隔离
`per-repo` 异步锁 + `per-task worktree` 实现任务间代码隔离。Git仓库clone和锁为模块级单例。变更集中在各层实现优化，不跨越层边界：webhook.py (API层) → review_worker.py (Worker层) → git_workspace_service.py (服务层)。

## AI输出协议演进
项目正从"灵活但易错"的解析（JSON+emoji）向"严格但可靠"的协议（行导向、标签化）演进。核心逻辑集中在 `review_protocol.py`，`result_parser.py` 作为薄适配层。协议变更属于架构级变更，需要完整的新旧映射和废弃策略。

## 决策引擎作为核心枢纽
`decision_engine.py` 变更时必须扫描所有调用方（review_worker/template_builder/API）是否适配新数据语义。"数据保留/镜像"模式须配套废弃计划。作为审查核心枢纽，其变更影响范围最广。
