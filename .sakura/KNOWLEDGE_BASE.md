# Sakura AI Reviewer 知识库

## 目录结构

### 1. 规则 (rules/)
- **review_hard_rules.md** - 审查硬规则（Major/Critical）
- **review_patterns.md** - 审查中发现的重要模式

### 2. 文档 (docs/)
- **architecture_decisions.md** - 架构设计与关键决策

### 3. 计划 (plans/)
- **experience_lessons.md** - 经验教训与常见问题模式
- **review_checklist.md** - 审查检查清单
- **common_issue_patterns.md** - 常见问题模式

## 知识分类说明

### rules/ - 审查规则、编码规范、项目约定
包含项目审查中发现的硬规则和重要模式，所有开发者必须遵守。

### docs/ - 架构文档、设计决策、技术栈信息
包含项目的架构设计、技术决策和关键设计模式。

### plans/ - 经验教训、常见问题模式、开发计划
包含从审查中总结的经验教训、常见问题模式和审查检查清单。

## 重要规范摘要

### 配置管理
- 配置默认值必须唯一声明于`config.py`
- 整数配置必须同时校验类型+范围+物理上限
- 动态配置读取调用方须处理None/非法值并记录warn
- 配置迁移必须输出"迁移完整性检查表"

### 错误处理
- `except Exception`必须记录日志
- 静默异常必须记录debug日志
- 全局异常处理器不得引用业务依赖

### 安全规范
- 所有JSON API端点必须使用CSRF依赖
- 明文敏感字段存储必须加密
- Shell命令白名单禁止前缀匹配

### 异步编程
- 异步调用方式必须统一
- asyncio.create_task创建的后台任务必须在服务shutdown时有机会完成
- 同步函数中使用get_running_loop().create_task时必须评估线程安全性

### 前端规范
- 模板JS插件安全规范：强制使用`|tojson`进行JS上下文转义
- SVG装饰图标必须添加`aria-hidden="true"` + `focusable="false"`
- 新增WebUI模块必须检查"四一致"

## 审查流程

### 增量审查必须输出
1. 已检查的规则清单（最少10条）
2. 历史未解决问题清单并升温
3. 未直接测试但依赖的调用链

### 重大问题升级机制
- 第1轮未修复：评估降级
- 第2轮未修复：强制降级
- 第3轮未修复：强制归档
- 第4轮未修复：自动生成issue

## 项目信息
- 仓库名: Sakura520222/Sakura-AI-Reviewer
- 后端语言: Python 3.11+
- Web框架: FastAPI
- 前端界面: HTML、Alpine.js、HTMX
- 数据库: MySQL、Redis
- 累计反思次数: 241