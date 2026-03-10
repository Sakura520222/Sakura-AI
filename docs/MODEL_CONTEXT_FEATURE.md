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

## 上下文压缩功能

### 功能概述

当审查 PR 时，如果对话历史（包括 AI 的工具调用、响应等）累积超过模型的上下文窗口阈值，系统会自动使用独立会话压缩历史对话，确保审查可以继续进行。

### 工作原理

#### 两个独立会话

1. **会话 1（主审查会话）**：
   - 持续的对话（user → assistant → tool → assistant → ...）
   - 执行代码审查和工具调用
   - 保留对话历史的连贯性

2. **会话 2（压缩专用会话）**：
   - 完全独立的会话
   - 专门用于压缩会话 1 的历史消息
   - 每次压缩都是全新的对话
   - 压缩完成后关闭，下次压缩再创建新的

#### 压缩触发条件

每次执行工具调用后，系统会检查：
```python
当前对话历史 tokens > 安全上下文窗口 × 压缩阈值
```

- **安全上下文窗口**：模型上下文 × 0.8（默认）
- **压缩阈值**：85%（默认）
- **触发条件**：当对话历史超过安全上下文的 85% 时

### 压缩策略

#### 保留内容

- ✅ **所有已发现的代码问题**（按严重程度分类）
- ✅ **所有行内评论的位置和内容**（文件路径:行号）
- ✅ **重要工具调用的结果**（文件内容、目录结构）
- ✅ **当前审查的进度**

#### 移除内容

- 🗑️ 重复的对话轮次
- 🗑️ 冗余的工具调用详情
- 🗑️ 已处理完成的问题

#### 压缩后格式

压缩后的摘要会保持与原始 PR 审查上下文相同的格式，确保 AI 能够理解并继续审查。

### 配置项

```env
# 上下文压缩配置
ENABLE_CONTEXT_COMPRESSION=true  # 是否启用上下文自动压缩
CONTEXT_COMPRESSION_THRESHOLD=0.85  # 压缩触发阈值（0-1），默认 85%
CONTEXT_COMPRESSION_KEEP_ROUNDS=2  # 保留最近几轮对话不压缩（预留功能）
```

### 使用示例

#### 场景 1：小型 PR（不需要压缩）

```
开始审查 → AI 调用工具 → 审查完成
（对话历史未超限，无需压缩）
```

#### 场景 2：中型 PR（触发一次压缩）

```
开始审查 → AI 调用工具1 → AI 调用工具2 → AI 调用工具3
→ 检查上下文 → 超限 85%
→ 触发压缩 → 创建会话 2 → 压缩历史
→ 替换会话 1 的历史 → 继续审查 → 完成
```

#### 场景 3：大型 PR（触发多次压缩）

```
开始审查 → 多轮工具调用 → 压缩 → 继续审查
→ 再次超限 → 再次压缩 → 继续审查 → 完成
```

### 日志示例

```
INFO: 开始AI审查（带工具支持），策略: standard
INFO: 执行工具 read_file: backend/services/user.py
INFO: 执行工具 list_directory: backend/services
...
WARNING: 🚨 上下文超限: 45000 tokens > 42500 (阈值 85.0%), 启动压缩...
INFO: 🗜️  开始压缩对话历史，当前大小: 45000 tokens
INFO: ✅ 压缩完成: 45000 → 12000 tokens
INFO: ✅ 压缩完成，继续审查...
INFO: 执行工具 read_file: backend/models/user.py
...
INFO: AI审查完成（使用了5轮对话），策略: standard
```

### 优势

1. **自动化**：无需手动干预，自动检测并压缩
2. **透明性**：通过日志清晰展示压缩过程
3. **智能保留**：保留关键信息，不影响审查质量
4. **适用所有策略**：quick、standard、deep、large 都支持
5. **容错性强**：压缩失败时自动回退到简化模式

### 故障排查

#### 问题 1：压缩失败

**症状**：日志显示"压缩失败，回退到简化模式"

**解决方案**：
- 这是正常的容错机制
- 系统会自动使用简化模式继续审查
- 检查网络连接和 API 密钥

#### 问题 2：频繁压缩

**症状**：每次审查都触发多次压缩

**可能原因**：
- PR 规模很大
- AI 频繁调用工具
- 压缩阈值设置过低

**解决方案**：
- 提高压缩阈值：`CONTEXT_COMPRESSION_THRESHOLD=0.9`
- 检查是否可以优化工具调用逻辑

#### 问题 3：压缩后质量下降

**症状**：压缩后的审查结果不如之前

**可能原因**：
- 压缩过于激进
- 丢失了关键信息

**解决方案**：
- 降低压缩阈值：`CONTEXT_COMPRESSION_THRESHOLD=0.8`
- 检查压缩 prompt 是否合理
- 调整 `CONTEXT_SAFETY_THRESHOLD`

### 最佳实践

1. **保持默认配置**：
   ```env
   ENABLE_CONTEXT_COMPRESSION=true
   CONTEXT_COMPRESSION_THRESHOLD=0.85
   ```
   默认配置适用于大多数场景。

2. **监控日志**：
   定期检查压缩日志，了解压缩频率和效果。

3. **调整阈值**：
   - 小型项目：可以提高阈值到 0.9（减少压缩次数）
   - 大型项目：可以降低阈值到 0.8（更早压缩，更稳定）

4. **结合上下文管理**：
   - 确保 `MODEL_CONTEXT_WINDOW` 设置正确
   - 确保 `CONTEXT_SAFETY_THRESHOLD` 合理（0.7-0.9）

## 更新日志

### v1.1.0 (2026-03-10)

- ✅ 实现上下文自动压缩功能
- ✅ 支持所有审查策略
- ✅ 使用两个独立会话（主审查 + 压缩专用）
- ✅ 智能压缩策略（保留关键信息）
- ✅ 容错机制（压缩失败回退）
- ✅ 详细的日志记录

### v1.0.0 (2026-03-10)

- ✅ 实现模型上下文自动检测功能
- ✅ 支持预定义模型映射表
- ✅ 支持 API 动态获取
- ✅ 支持用户自定义配置
- ✅ 实现 Token 估算功能
- ✅ 集成到 AI 审查器
- ✅ 更新配置文件

## 未来计划

- [x] 实现上下文自动压缩功能（已完成）
- [ ] 支持更多模型
- [ ] 实现实时上下文监控
- [ ] 添加上下文使用统计
- [ ] 优化 Token 计算精度
- [ ] 支持动态调整上下文大小
