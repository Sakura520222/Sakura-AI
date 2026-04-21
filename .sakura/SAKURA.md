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
- **`sakura_memory_service.py`**：819行"上帝服务"，职责过重，且无重试或回滚机制

### 安全敏感点
- 路径处理禁用字符串检查（如 `if "../" in path`），必须用 `pathlib.Path.resolve()` + 前缀验证
- 写操作服务必须审查权限边界与异常链路，安全相关问题禁止降级为 suggestion

## 4. 审查发现的重要模式
- **数据契约不严格**：跨模块传递混用 `dict` 与对象，缺类型注解，易致运行时 `AttributeError`
- **Prompt 防御性编码**：将 `{xxx}` 占位符转为自然语言指令，消除 LLM 对元字符的歧义
- **增量审查陷阱**：易陷入"隧道视野"（只看 diff 忽略架构）、"零评论高分"（走过场）、"上下文浪费"（无视 PR 描述中的历史问题）

## 5. 团队约定与规范
### 代码规范
- 跨模块复杂数据禁止裸 `dict`，必须用 `Pydantic BaseModel`、`dataclass` 或 `TypedDict`
- 文件路径操作必须使用 `pathlib`，禁止字符串拼接/替换
- 建议引入 `mypy`/`pyright` 严格模式拦截隐式类型错误

### 审查规则（按优先级）
| 规则 | 级别 | 触发条件 |
|------|------|----------|
| 历史阻断机制 | P0 | 存在未闭环 error/critical 时，增量审查禁止 approve |
| PR-SIZE-001 | P0 | 文件数 >10 禁止 quick 策略 |
| REVIEW-OUTPUT-001 | P0 | 评分必须有评论支撑，零评论高分属异常 |
| REVIEW-DECISION-001 | P0 | 禁止 unknown 决策 |
| 增量上下文检查 | P0 | PR含历史审查上下文时，必须引用历史问题状态 |
| 评分理由 | P1 | 9分以上需说明优点，扣分需明确原因 |
| SEC-PATH-001 | error | 文件路径使用字符串检查 `../` |
| SEC-WRITE-001 | error | 新增写服务未审查权限边界 |
| SEC-PATH-002 | warning | 文件操作未用 `pathlib.Path.resolve()` |
| ARCH-LARGE-FILE | warning | 单文件新增超 500 行 |
| PR-SCOPE-001 | P1 | 单PR同时含新子系统+配置变更+版本升级 |

### PR 审查原则
- **评分与决策解耦**：评分反映增量质量，decision 反映 PR 整体可合并性
- **大 PR 拆分**：>1000行按模块拆分（核心服务→安全敏感→配置），安全模块强制提升级别
- **quick 策略底线**：至少1条摘要+评分理由；即使不解决历史问题也需输出问题状态更新表标注`[遗留]`
- **点状修复警惕**：单行 Bug 修复需横向检查全局是否存在同类问题