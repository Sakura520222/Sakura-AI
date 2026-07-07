# 模型上下文管理

Sakura AI 会根据模型上下文窗口、当前 PR 内容和多轮工具调用历史，动态控制发送给 AI 的上下文大小。目标是在不截断关键代码信息的前提下，提升大型 PR 审查的稳定性和成本可控性。

## 功能概览

- **模型上下文窗口识别**：优先使用 WebUI 动态配置，也可通过 AI Provider 注册表自动获取模型列表和上下文窗口信息。
- **安全上下文预算**：按 `context_safety_threshold` 预留响应空间、工具调用空间和格式化开销。
- **大型 PR compact diff**：初始 diff 过大时，只发送文件元信息与增删行统计，AI 通过工具按需读取具体文件 diff。
- **对话历史自动压缩**：多轮工具调用导致历史接近阈值时，使用独立压缩会话总结历史，再继续审查。
- **辅助模型回退**：摘要、上下文压缩等轻量任务可使用辅助模型；未配置时回退主模型。

## 配置优先级

全局配置遵循：

```text
数据库 app_config（WebUI 动态配置） > Settings 默认值
```

用户偏好配置遵循：

```text
UserConfig > app_config > Settings 默认值
```

> 运行时建议通过 WebUI 配置管理调整上下文相关配置。环境变量和 `Settings` 字段主要作为部署默认值或首次初始化默认值，不应作为主要运行时修改方式。

## 主要配置项

| 配置项 | 说明 |
|--------|------|
| `model_context_window` | 手动指定模型上下文窗口（tokens）。为空或无效时使用模型自动识别结果或默认值 |
| `auto_fetch_model_context` | 是否尝试从 AI Provider API / 注册表获取模型上下文信息 |
| `context_safety_threshold` | 安全上下文比例，用于预留输出和工具调用空间 |
| `enable_context_compression` | 是否启用对话历史自动压缩 |
| `context_compression_threshold` | 压缩触发阈值：当前历史 tokens 超过安全上下文的该比例时触发 |
| `context_compression_keep_rounds` | 压缩时保留最近对话轮数的配置项 |
| `summary_model` / `summary_api_base` / `summary_api_key` | 辅助模型配置，用于摘要、压缩、标签推荐等轻量任务 |
| `ai_api_timeout_seconds` | AI API 单次 HTTP 请求超时 |
| `ai_api_total_timeout_seconds` | 一次 AI 调用在重试循环中的总耗时上限 |

## 模型上下文窗口来源

系统按以下顺序确定模型上下文窗口：

1. `model_context_window` 动态配置。
2. AI Provider 注册表中已知模型或模型列表返回的上下文窗口信息。
3. 模型名称规则匹配结果。
4. 默认安全值。

Setup Wizard 和 WebUI 配置页会使用 AI Provider 注册表展示内置厂商元数据，并在厂商支持时自动获取模型列表和上下文窗口。例如 OpenAI 兼容、DeepSeek、Qwen、Z.ai、Doubao、SiliconFlow、Gemini、Anthropic 兼容和自定义 OpenAI 兼容配置。

## 安全上下文预算

安全上下文预算按模型窗口和阈值计算：

```text
safe_context = model_context_window × context_safety_threshold
```

该预算用于容纳：

- 系统提示词
- PR 元数据
- PR diff 或 compact diff 文件列表
- RAG / 代码索引 / 项目记忆上下文
- 多轮工具调用历史
- AI 最终输出空间

建议保持默认阈值，只有在大型 PR、特殊模型或成本敏感场景下再调整。

## 大型 PR compact diff 模式

当初始消息接近上下文阈值时，PR 审查会自动切换到 compact diff 模式。

### 行为

普通模式会把 PR diff 直接放入初始 prompt。compact diff 模式不会发送完整 diff，而是发送：

- 变更文件列表
- 文件状态
- 增删行数
- 工具使用说明

AI 可按需调用：

- `list_changed_files()`：查看变更文件列表。
- `get_file_diff(file_path)`：读取指定文件 diff。
- `read_file(file_path)`：读取仓库文件内容。

### 优势

- 避免大型 diff 一次性占满上下文。
- 让 AI 将 token 用在真正需要审查的文件上。
- 与多轮工具调用和上下文压缩配合，提升大型 PR 完成率。

### 相关实现

- `backend/services/ai_reviewer/compact_diff.py`
- `backend/services/ai_reviewer/prompt_builder.py`
- `backend/services/ai_reviewer/constants.py`

## 对话历史自动压缩

当审查过程中发生多轮工具调用，对话历史超过阈值时，系统会自动压缩历史上下文。

### 触发条件

```text
当前对话历史 tokens > safe_context × context_compression_threshold
```

### 工作方式

1. 主审查会话持续执行代码审查和工具调用。
2. 触发压缩后，系统创建独立压缩会话。
3. 压缩会话总结历史中的关键发现、已读文件、行内评论位置和待处理事项。
4. 主审查会话用压缩摘要替换旧历史，并继续审查。

### 压缩保留内容

- 已发现的问题及严重程度。
- 行内评论文件路径、行号和内容。
- 已阅读的重要文件、目录结构、工具调用结论。
- 当前审查进度和仍需检查的区域。

### 压缩移除内容

- 重复对话轮次。
- 冗余工具调用细节。
- 已处理完成且不再影响结论的信息。

## 故障排查

### 无法识别模型上下文

处理建议：

1. 在 WebUI 中检查 AI Provider、API Base、模型名是否正确。
2. 尝试从配置页重新获取模型列表。
3. 必要时手动设置 `model_context_window`。

### 大型 PR 审查仍然失败

处理建议：

1. 确认 `enable_context_compression` 已启用。
2. 适当降低 `context_compression_threshold`，让系统更早压缩。
3. 检查 AI API 请求是否触发 `ai_api_timeout_seconds` 或 `ai_api_total_timeout_seconds`。
4. 对极大 PR，可考虑拆分 PR 或使用上下文窗口更大的模型。

### 频繁压缩导致成本升高

处理建议：

1. 适当提高 `context_compression_threshold`。
2. 使用辅助模型处理压缩任务。
3. 检查 PR 是否包含生成文件、锁文件或大体积文件，并在策略配置中过滤。

## 最佳实践

- 优先通过 WebUI 修改配置，避免依赖运行中不可见的环境变量变更。
- 对常见模型使用 AI Provider 注册表自动识别上下文窗口。
- 对大型仓库启用 RAG、代码索引和项目记忆，让 AI 按需检索而不是一次性塞入全部上下文。
- 保持 compact diff 与上下文压缩开启，以兼顾大型 PR 完成率和成本。
- 为摘要/压缩配置较便宜的辅助模型，主模型专注最终审查质量。

## 更新日志

### v2.10.0 (2026-05-11)

- ✅ 将文档更新为 WebUI 动态配置优先。
- ✅ 补充 AI Provider 注册表和模型上下文窗口自动发现说明。
- ✅ 补充大型 PR compact diff 模式说明。
- ✅ 补充 AI API 单次请求超时和重试总超时配置说明。

### v1.1.0 (2026-03-10)

- ✅ 实现上下文自动压缩功能。
- ✅ 支持所有审查策略。
- ✅ 使用主审查会话与压缩专用会话。
- ✅ 增加压缩失败回退机制。

### v1.0.0 (2026-03-10)

- ✅ 实现模型上下文自动检测功能。
- ✅ 支持预定义模型映射表。
- ✅ 支持 Token 估算功能。
- ✅ 集成到 AI 审查器。
