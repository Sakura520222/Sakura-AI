# AI Provider 与 Agent 审查规则

## AI Provider 兼容性
遇到 AI API BadRequest/Unauthorized/Forbidden/模型不存在时，必须区分：
- 已确认事实、日志推断、待验证假设；
- 模型名、API Base、Provider 类型、SDK 版本；
- 调用接口类型：Chat Completions / Responses / Completions；
- 实际 payload 是否含 messages/input/prompt；
- 是否经过第三方 OpenAI 兼容代理。

不要过早断定“配置问题而非代码缺陷”。兼容代理可能路径兼容但语义不兼容。

## 不可重试错误
- 400 参数错误、401、403、模型不存在、API 格式不兼容应标记不可重试。
- 不可重试错误应尽早终止当前 agent，不应循环到 max_iterations 后报告“无变更”。
- 任务状态、activity、WebUI 应显示可操作原因，如检查模型/API Base/Provider。
- 配置保存或启动阶段可提供模型/API Base 校验或测试调用。

## Agent conversation 契约
- Agent 会话应优先使用 `[system, user]` 起始消息：system 描述角色约束，user 给出当前任务。
- 只依赖 system prompt 启动任务时，需要注释原因并测试消息序列。
- 修改 agent 基类核心循环、错误处理、break/return 语义时，必须列出所有子类/调用方并检查日志、清理、返回状态、副作用。
- LLM 失败路径不应误报“达到最大迭代次数”。日志语义变化应有测试验证。

## 测试注入
测试可使用 fake client，但优先通过公开注入点、构造参数或 monkeypatch 初始化函数；直接写私有属性只作为非阻断建议。
