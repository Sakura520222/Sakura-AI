# 长期记忆架构与认知系统演进决策

## 一、长期记忆与 Harness 2.0 深度集成

### 1.1 架构动因与废弃切断
- **背景**：早期系统将记忆、反思以 Markdown 文件直接持久化并由 Agent 提交到用户 Git 仓库。该机制存在高频触发用户仓库 CI、污染提交树、权限扩散及并发写冲突的严重问题。
- **重构决策**：
  1. **彻底转向数据库持久化**：以 `sakura_summaries` 等核心数据表为依托，将任务总结、历史经验和结构化记忆全面下沉至数据库管理。
  2. **全面对接 Harness 2.0 契约**：复用 Harness 2.0 的 `RepositoryContext`、作用域解析与 Skills 发现机制，废弃原有 `read_sakura_memory` 文件读取工具。
  3. **零继承原则 (Zero-Inheritance)**：不强求全量清洗与迁移历史 Markdown 仓库文件，直接切断旧写入链路，新任务统一在数据库长期记忆系统中自愈与成长。

---

## 二、双向 Git 同步与 IDE 协作服务架构

### 2.1 官方扩展集成与服务下沉决策
- **业务逻辑下沉 Service 层**：为支持 VS Code 官方扩展与外部客户端双向交互，将原本耦合在 WebUI 路由（`backend/webui/routes/agent_team.py`）中的任务编排与 Git 状态逻辑下沉到独立 Service 层，暴露标准 REST API。
- **并发 Git 协作的 CAS 乐观锁防线**：在客户端与服务端双向推送/同步时，依托 `git_workspace_service.py` 实施异步工作区锁与 CAS 乐观锁，严禁破坏性的 `reset --hard` 或 `force-push` 踩踏用户未提交的代码。
- **准入零信任模型**：客户端传参（如用户身份、分支名、计费主体）禁止直接信任；服务端必须在 Service 入口依据 Bearer Token 和实际仓库权限重新计算并强制覆盖执行上下文。
