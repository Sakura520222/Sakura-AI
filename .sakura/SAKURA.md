# Sakura AI Reviewer 项目概述

## 1. 项目简介与技术栈
基于大语言模型的智能 GitHub 代码审查与 Issue 分析机器人，具备跨文件依赖理解、全仓库扫描和项目记忆能力。
- **技术栈**：Python 3.11+ / FastAPI / MySQL & Redis / LLM & RAG / Docker / GitHub App & Telegram Bot

## 2. 架构设计与关键决策
采用后端分层架构，前后端同仓：
- `backend/api/`：路由；`core/`：配置与依赖注入；`models/`：数据结构
- `backend/services/`：核心业务（AI审查、记忆系统、RAG等）
- `backend/workers/`：异步任务；`telegram/`：Bot交互；`webui/`：前端
- `config/`：外部化配置（YAML）
- **关键决策**：业务规则 YAML 外部化热更新；耗时任务 Worker 隔离；容器化交付。

## 3. 已知问题与注意事项
### PR190 遗留缺陷（未闭环）
- **`github_write_service.py:76`**：多文件提交非原子性，数据一致性风险
- **`github_write_service.py:28-31`**：单例实现线程安全问题
- **`sakura_memory_service.py`**：800+行"上帝服务"，职责过重。存在双数据源过渡态（dict回退+DB直查）、格式化逻辑分散、无重试或回滚机制，已纳入下轮全量审查候选
- **`_fetch_comments_from_db`**：缺少对 `review_id` 空字符串或无效格式的防御校验

### 安全敏感点
- 路径处理禁用字符串检查（如 `if "../" in path`）、禁用 `PurePosixPath`（不解析符号链接，提供虚假安全感），必须用 `pathlib.Path.resolve()` + 前缀验证，建议升级为 AST 硬性拦截
- 写操作服务必须审查权限边界与异常链路，安全/并发相关问题禁止降级为 suggestion

## 4. 审查发现的重要模式
- **数据契约不严格**：跨模块传递混用 `dict` 与对象，缺类型注解，易致运行时 `AttributeError`
- **Prompt 防御性编码**：将 `{xxx}` 占位符转为自然语言指令，消除 LLM 对元字符的歧义
- **对称结构的一致性缺失**：A/B对称逻辑易出现边界条件不对等（如正则lookahead不对称、`lower()`归一化未贯穿）
- **防御性编程表面化**：使用高级API（如`PurePosixPath`）但不理解底层机制，产生虚假安全感
- **增量审查陷阱**：隧道视野（只看diff忽略架构）、零评论高分（走过场）、上下文浪费（无视PR描述历史）、quick策略退化为通用风格检查器
- **创可贴式修复**：连续多轮只处理截断等边缘 Bug，底层架构腐化（并发、原子性）始终未触碰
- **quick 策略凑数**：将"正确代码的优化建议"（如加断言、延迟导入）误拔高为 major

## 5. 团队约定与规范
### 代码规范
- 跨模块复杂数据禁止裸 `dict`，必须用 `Pydantic BaseModel`、`dataclass` 或 `TypedDict`
- 文件路径操作必须使用 `pathlib`，安全场景严格禁止使用 `Pure` 系列路径
- 同一语义数值字面量出现≥2次必须提取为常量（`constants.py`），禁止魔法数字
- 延迟导入只有确认无循环导入风险且无性能意图时，才可标记为问题
- 新增功能存在"主路径+回退路径"双数据源时，必须标注过渡期和收敛计划

### 审查规则（按优先级）
| 规则 | 级别 | 触发条件 |
|------|------|----------|
| PR级阻断表强制 | P0 | 遗留critical必须在每次追踪表中出现，禁止遗漏，状态只能更新不可删除 |
| quick策略approve检查 | P0 | approve前必须显式确认PR级遗留critical为0，否则自动降级 |
| 历史阻断机制 | P0 | 存在未闭环 error/critical 时，增量审查禁止 approve |
| 强制拦截词 | P0 | PR含"非原子性/线程安全/KeyError"时，锁定 approve 权限 |
| 遗留降级禁令 | P0 | 禁止将 Critical/Error 降级为 Suggestion 放入 `[