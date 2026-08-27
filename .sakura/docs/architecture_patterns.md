# 架构模式与设计要点

## 记忆系统集成

- SakuraMemoryService 提供项目记忆与上下文能力，与 PR 分析器（pr_analyzer.py）有数据契约依赖。
- 双接入点：通过工具注入（sakura_tool.py）+ 直接服务调用（issue_analyzer.py）集成，替换或禁用需同步修改多处。

## 数据流与写入路径

- GitHubWriteService 新增写入路径，打破“只读审查”假设，需审查并发、权限、API 限制与 PR 创建失败兜底。

## GitHub Contents API 风险

- 在 .sakura/memory/ 场景下正常很少超限，但仍应关注截断后是静默丢数据还是报错；静默丢数据为 major。

## 单例模式一致性

- GitHubWriteService 使用 __new__ + 模块级变量双重保障；SakuraMemoryService 仅用模块级变量。风格不统一增加认知负担。

## 防御性编程一致性

- 同 PR 内路径校验、记忆注入防御标准不统一，建议各关键路径统一错误处理与校验策略。

## 路径处理规范

- 禁用字符串检查与 PurePosixPath（不解析符号链接）；应使用 pathlib.Path.resolve() + 前缀验证。
- 本次审查中的 PurePosixPath.resolve() 建议与规范冲突，应避免。