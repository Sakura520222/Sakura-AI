# 知识提取完成总结

## 执行时间
2025-01-10

## 提取范围
- Sakura520222/Sakura-AI 仓库
- 10 次审查反思（2026-04 至 2026-08）
- 已有知识文件：rules/、docs/、plans/

## 新增规则文件 (5个)

### 1. rules/ai_output_parsing_rules.md
- AI 输出解析鲁棒性规则
- 协议模板字段缺失处理、防御性编程、错误信息隔离

### 2. rules/asgi_contextvar_rules.md
- ASGI ContextVar 生命周期规则
- 显式依赖声明、Supervisor 管理、关闭路径安全

### 3. rules/toolchain_update_review_rules.md
- 大规模工具链更新审查规则
- 配置合理性审计、变更等价性验证、依赖兼容性验证

### 4. rules/knowledge_base_maintenance_rules.md
- 知识库维护规则
- 覆盖式更新禁止、删除规则规范、知识负债监控

### 5. rules/activity_checkpoint_rules.md
- Activity Checkpoint 审查规则
- 数据保留策略、状态机完整性、并发安全、DB 与 SSE 顺序

### 6. rules/python_syntax_rules.md
- Python 语法一致性规则
- Python 2/3 兼容性、基础语法错误、导入链验证

## 新增文档文件 (3个)

### 1. docs/toolchain_update_patterns.md
- 工具链更新模式
- 大规模更新的本质、常见场景、变更类型分类

### 2. docs/ai_output_parsing_architecture.md
- AI 输出解析架构
- 解析策略、契约设计、错误处理、测试策略

### 3. docs/activity_checkpoint_architecture.md
- Activity Checkpoint 架构
- 核心链路、组件、设计决策、状态机

## 新增经验教训文件 (5个)

### 1. plans/ai_output_robustness_lessons.md
- AI 输出鲁棒性经验教训
- 核心问题模式、解决方案、经验总结

### 2. plans/toolchain_update_lessons.md
- 大规模工具链更新经验教训
- 常见陷阱、审查要点、最佳实践

### 3. plans/knowledge_base_maintenance_lessons.md
- 知识库维护经验教训
- 核心问题模式、核心原则、维护规范

### 4. plans/python_syntax_lessons.md
- Python 语法一致性经验教训
- 核心问题模式、强制规则、审查优先级

### 5. plans/incremental_review_lessons.md
- 增量审查经验教训
- 核心问题模式、强制规则、审查策略

## 更新现有文件 (3个)

### 1. plans/common_issue_patterns.md
- 新增 5 个问题模式：
  - Python 2 语法残留
  - 知识库覆盖式更新风险
  - CI 工作流隐式行为变更
  - ASGI ContextVar 生命周期问题
  - AI 输出解析鲁棒性缺陷

### 2. docs/architecture_decisions.md
- 新增 5 个架构决策：
  - AI 输出解析架构
  - Activity Checkpoint 架构
  - ASGI ContextVar 使用规范
  - 工具链更新审查架构
  - 知识库维护架构

### 3. rules/review_hard_rules.md
- 新增 4 个硬规则章节：
  - Python 语法一致性规则
  - 知识库维护规则
  - Activity Checkpoint 规则

## 核心知识主题

### 1. AI 输出解析鲁棒性
- 防御性解析、字段缺失处理、错误信息脱敏
- 协议模板字段缺失导致全链路崩溃

### 2. Activity Checkpoint 架构
- 状态持久化 + 实时通知混合模式
- 事务顺序、并发安全、数据保留策略

### 3. ASGI ContextVar 使用
- 禁止隐式依赖、显式依赖注入
- Supervisor 管理、关闭路径保护

### 4. 工具链更新审查
- 配置合理性审计、变更等价性验证
- 依赖兼容性、审查分层

### 5. 知识库维护
- 禁止覆盖式更新、增量更新优先
- 删除规则必须说明理由

### 6. Python 语法一致性
- 禁止 Python 2 语法、基础模块导入链验证
- 语法错误的放射性影响

### 7. 增量审查优化
- 隧道视野、历史问题追踪
- approve 在阻断上下文中无效

## 文件大小控制
所有文件均控制在 3000 字符以内，符合约束要求。

## 提取质量
- 从 10 次审查反思中提取了核心模式
- 涵盖架构、规则、经验三个维度
- 与现有知识体系无缝集成
- 遵循宁缺毋滥原则，只提取最有价值的内容
