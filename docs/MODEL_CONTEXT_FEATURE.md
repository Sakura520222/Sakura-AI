# AI 模型上下文管理功能

## 功能概述

Sakura-AI-Reviewer 现在支持自动获取和管理不同 AI 模型的上下文窗口大小，帮助优化代码审查的性能和准确性。

## 主要特性

### 1. 自动检测模型上下文

系统会自动识别并获取以下模型的上下文窗口大小：

- **OpenAI 模型**：GPT-4、GPT-4 Turbo、GPT-4o、GPT-3.5 Turbo 等
- **DeepSeek 模型**：DeepSeek-Chat、DeepSeek-Coder、DeepSeek-R1、DeepSeek-v3 等
- **Claude 模型**：Claude 3.5 Sonnet、Claude 3 Opus、Claude 3 Haiku 等
- **Gemini 模型**：Gemini 2.0 Flash、Gemini 1.5 Pro、Gemini 1.5 Flash 等
- **其他模型**：Llama、Mistral、Qwen 等

### 2. 多种获取方式

系统按以下优先级获取模型上下文：

1. **用户自定义配置**（优先级最高）
2. **API 动态获取**（如果启用）
3. **预定义映射表**（内置支持常见模型）
4. **默认值**（128K tokens）

### 3. 智能上下文管理

- 自动计算安全的上下文使用量（默认使用 80%）
- 支持 Token 估算（中英文混合文本）
- 格式化显示上下文大小（K/M）

## 配置说明

### 环境变量配置

在 `.env` 文件中添加以下配置：

```env
# 模型上下文配置
MODEL_CONTEXT_WINDOW=0  # 自定义上下文窗口大小（K tokens），0 表示自动检测
AUTO_FETCH_MODEL_CONTEXT=true  # 是否自动从 API 获取模型上下文
CONTEXT_SAFETY_THRESHOLD=0.8  # 上下文安全阈值（0-1），默认使用 80%
```

### 配置项详解

#### MODEL_CONTEXT_WINDOW

- **作用**：自定义模型的上下文窗口大小
- **单位**：K tokens（千 tokens）
- **默认值**：0（自动检测）
- **示例**：
  - `MODEL_CONTEXT_WINDOW=128` 表示 128K tokens
  - `MODEL_CONTEXT_WINDOW=200` 表示 200K tokens

**使用场景**：
- 当自动检测失败时手动指定
- 使用自定义模型或私有部署的模型
- 需要限制上下文使用量以节省成本

#### AUTO_FETCH_MODEL_CONTEXT

- **作用**：是否从 API 动态获取模型信息
- **默认值**：true
- **可选值**：true / false

**使用场景**：
- 启用：自动获取最新的模型信息（推荐）
- 禁用：仅使用预定义映射表，减少 API 调用

#### CONTEXT_SAFETY_THRESHOLD

- **作用**：上下文安全阈值，避免使用全部上下文
- **默认值**：0.8（使用 80%）
- **范围**：0.0 - 1.0
- **建议**：0.7 - 0.9

**使用场景**：
- 预留空间给输出 tokens
- 避免达到模型硬限制
- 提高稳定性

## 使用示例

### 示例 1：使用 GPT-4

```env
OPENAI_MODEL=gpt-4
MODEL_CONTEXT_WINDOW=0  # 自动检测，结果为 128K
```

系统会自动识别 GPT-4 的上下文窗口为 128K tokens。

### 示例 2：使用 DeepSeek-R1

```env
OPENAI_MODEL=deepseek-r1
MODEL_CONTEXT_WINDOW=0  # 自动检测，结果为 64K
```

系统会自动识别 DeepSeek-R1 的上下文窗口为 64K tokens。

### 示例 3：自定义模型上下文

```env
OPENAI_MODEL=custom-model-v1
MODEL_CONTEXT_WINDOW=256  # 自定义为 256K
```

当使用自定义模型时，可以手动指定上下文大小。

### 示例 4：限制上下文使用

```env
OPENAI_MODEL=gpt-4
MODEL_CONTEXT_WINDOW=0  # 自动检测
CONTEXT_SAFETY_THRESHOLD=0.5  # 只使用 50% 的上下文
```

即使 GPT-4 支持 128K，系统也只会使用约 64K tokens。

## API 使用

### 在代码中使用

```python
from backend.core.model_context import get_model_context_manager

# 获取模型上下文管理器
context_mgr = get_model_context_manager()

# 获取模型的上下文窗口大小（K tokens）
context_size = context_mgr.get_context_window("gpt-4")
print(f"GPT-4 上下文: {context_size}K tokens")

# 计算安全的上下文大小
safe_context = context_mgr.calculate_safe_context("gpt-4", safety_ratio=0.8)
print(f"安全上下文: {safe_context}K tokens")

# 估算文本的 token 数量
text = "这是一段中文文本 This is English text"
estimated_tokens = context_mgr.estimate_tokens(text)
print(f"估算 tokens: {estimated_tokens}")

# 格式化显示上下文大小
formatted = context_mgr.format_context_size(128)
print(f"格式化: {formatted}")  # 输出: 128K
```

## 支持的模型列表

### OpenAI 模型

| 模型名称 | 上下文窗口 |
|---------|-----------|
| gpt-4 | 128K |
| gpt-4-turbo | 128K |
| gpt-4-turbo-preview | 128K |
| gpt-4o | 128K |
| gpt-4o-mini | 128K |
| gpt-3.5-turbo | 16K |

### DeepSeek 模型

| 模型名称 | 上下文窗口 |
|---------|-----------|
| deepseek-chat | 128K |
| deepseek-coder | 128K |
| deepseek-r1 | 64K |
| deepseek-v3 | 64K |

### Claude 模型

| 模型名称 | 上下文窗口 |
|---------|-----------|
| claude-3-5-sonnet-20241022 | 200K |
| claude-3-5-sonnet-20240620 | 200K |
| claude-3-5-haiku-20241022 | 200K |
| claude-3-opus-20240229 | 200K |

### Gemini 模型

| 模型名称 | 上下文窗口 |
|---------|-----------|
| gemini-2.0-flash-exp | 1000K (1M) |
| gemini-1.5-pro | 1000K (1M) |
| gemini-1.5-flash | 1000K (1M) |

## 最佳实践

### 1. 使用自动检测

大多数情况下，让系统自动检测模型上下文即可：

```env
MODEL_CONTEXT_WINDOW=0
AUTO_FETCH_MODEL_CONTEXT=true
```

### 2. 设置合理的安全阈值

建议使用 0.7-0.9 的安全阈值：

```env
CONTEXT_SAFETY_THRESHOLD=0.8  # 推荐值
```

### 3. 监控上下文使用

查看日志中的上下文使用情况：

```
INFO: 从预定义表获取模型上下文: gpt-4 = 128K tokens
INFO: 计算安全上下文: 128K * 0.8 = 102K tokens
```

### 4. 优化成本

对于大型 PR，可以通过降低安全阈值来节省成本：

```env
CONTEXT_SAFETY_THRESHOLD=0.6  # 使用更少的上下文
```

## 故障排查

### 问题 1：无法识别模型

**症状**：日志显示"使用默认值: 128K tokens"

**解决方案**：
1. 检查模型名称是否正确
2. 手动设置 `MODEL_CONTEXT_WINDOW`
3. 向预定义列表添加新模型

### 问题 2：API 获取失败

**症状**：日志显示"从 API 获取模型上下文失败"

**解决方案**：
1. 检查网络连接
2. 检查 API 密钥是否有效
3. 设置 `AUTO_FETCH_MODEL_CONTEXT=false`

### 问题 3：上下文不足

**症状**：AI 返回不完整的审查结果

**解决方案**：
1. 检查 `CONTEXT_SAFETY_THRESHOLD` 是否过低
2. 考虑使用上下文更大的模型
3. 确保已去除 diff 截断限制

## 更新日志

### v1.0.0 (2026-03-10)

- ✅ 实现模型上下文自动检测功能
- ✅ 支持预定义模型映射表
- ✅ 支持 API 动态获取
- ✅ 支持用户自定义配置
- ✅ 实现 Token 估算功能
- ✅ 集成到 AI 审查器
- ✅ 更新配置文件

## 未来计划

- [ ] 支持更多模型
- [ ] 实现实时上下文监控
- [ ] 添加上下文使用统计
- [ ] 优化 Token 计算精度
- [ ] 支持动态调整上下文大小