# Agent Team架构与状态机

## 核心架构层次

```
webhook.py (API层) → review_worker.py (业务逻辑/Worker层) → git_workspace_service.py (服务层)
```
变更应集中在各层实现优化，不跨越层边界。

## 多Worktree隔离架构

- `per-repo` 异步锁 + `per-task worktree` 实现任务间代码隔离。
- Git仓库clone(`base`)和锁(`_repo_locks`)被设计为模块级单例，服务于所有任务。
- 模块级缓存提高了效率，但也带来了状态管理和锁粒度的长期挑战。

## Agent Team PR状态机

关键事件序列：PR opened → external review started → review completed → blocking feedback → iteration started → push completed → new review requested → PR synchronized → PR closed / review dismissed。

审查须检查：
- 重复webhook事件的幂等处理
- 乱序/延迟事件的状态一致性
- PR closed后到达的事件处理
- branch复用、旧任务、同名分支的匹配稳定性
- 终态资源清理（锁、worktree）

## 同步I/O与异步框架融合

异步FastAPI路由和异步worker中使用同步Git命令时，必须用 `asyncio.to_thread` 包装，并确认包装方式不影响事务、锁等上下文的传递。

## 决策引擎中枢

`decision_engine.py` 作为核心枢纽，变更时必须扫描所有调用方（review_worker/template_builder/API）是否适配新数据语义。"数据保留/镜像"模式须配套废弃计划。

## 分层职责

- `AIReviewer`/`IssueAnalyzer`：AI审查核心
- `AgentTeamWorker`：任务编排
- `workspace_service`：工作区路径逻辑
- `git_workspace_service`：底层Git操作
- 活动系统：ActivityEvent/SSE/Checkpoint实时同步
