# 🌸 Sakura AI Reviewer - 审查批准功能实现总结

## 📋 功能概述

成功实现了**智能审查批准功能**，使 Sakura AI Reviewer 能够根据 AI 评分和问题严重程度自动做出审查决策，并直接提交到 GitHub。

## ✨ 核心特性

### 1. 多维度决策引擎

```python
决策逻辑：
┌─────────────────────────────────────────┐
│         AI审查完成，获得评分和问题         │
└──────────────┬──────────────────────────┘
               │
               ▼
    ┌──────────────────────┐
    │  Critical > 0 ?      │──Yes──► REQUEST_CHANGES (一票否决)
    └──────────┬───────────┘
               │ No
               ▼
    ┌──────────────────────┐
    │  Score < 4 ?         │──Yes──► REQUEST_CHANGES (低分阻断)
    └──────────┬───────────┘
               │ No
               ▼
    ┌──────────────────────┐
    │  Score >= 8 &&       │
    │  Major <= 1 ?        │──Yes──► APPROVE (批准合并)
    └──────────┬───────────┘
               │ No
               ▼
          COMMENT (中立评论)
```

### 2. 三种审查决策

- **APPROVE**：代码质量优秀，可以合并
- **REQUEST_CHANGES**：发现严重问题或评分过低，需要修复
- **COMMENT**：处于中间状态，建议人工复审

### 3. 幂等性保护

检查是否已存在相同类型的 Review，避免重复提交：

```python
def has_existing_review(
    repo_owner, repo_name, pr_number, 
    bot_username, event
) -> bool
```

## 🗂️ 文件变更清单

### 1. 配置文件

- **config/strategies.yaml**
  - 添加 `review_policy` 配置段
  - 支持仓库级别的覆盖配置
  - 可配置的审查模板

### 2. 数据库模型

- **backend/models/database.py**
  - 新增 `ReviewDecision` 枚举
  - `PRReview` 表添加 `decision` 和 `decision_reason` 字段

### 3. 决策引擎

- **backend/services/decision_engine.py** (新建)
  - `DecisionEngine` 类
  - `make_decision()` 方法：根据评分和问题做出决策
  - `format_review_body()` 方法：格式化审查评论

### 4. GitHub API 集成

- **backend/core/github_app.py**
  - `has_existing_review()`: 幂等性检查
  - `submit_review()`: 提交审查决定到 GitHub
  - `get_bot_username()`: 获取机器人用户名

### 5. 审查流程

- **backend/workers/review_worker.py**
  - 导入决策引擎
  - 添加 `_make_and_submit_decision()` 方法
  - 在审查完成后自动执行决策

## ⚙️ 配置说明

### 基础配置

```yaml
review_policy:
  # 是否启用自动批准功能（建议先设为false观察效果）
  enabled: false
  
  # 批准阈值：分数 >= 此值才考虑批准
  approve_threshold: 8
  
  # 阻断阈值：分数 < 此值自动请求变更
  block_threshold: 4
  
  # 是否在存在Critical问题时自动请求变更
  block_on_critical: true
  
  # 允许的Major问题数量上限
  max_major_issues: 1
  
  # 幂等性检查：是否检查已有Review避免重复提交
  enable_idempotency_check: true
```

### 仓库级别覆盖

```yaml
review_policy:
  repo_overrides:
    "owner/core-project":
      approve_threshold: 9  # 核心项目更严格
      block_threshold: 5    # 更宽松
```

## 🚀 部署建议

### 阶段1：观察期（推荐）

```yaml
review_policy:
  enabled: false  # 仅评论，不自动批准
```

观察 1-2 周，检查 AI 评分的准确性。

### 阶段2：逐步启用

```yaml
review_policy:
  enabled: true
  approve_threshold: 9  # 先设置高阈值
  block_on_critical: true
```

### 阶段3：正式运行

根据观察结果调整阈值。

## 🔍 工作流程

```
PR Opened
    ↓
AI 审查（评分 + 问题分类）
    ↓
决策引擎分析
    ↓
提交 GitHub Review
    ├─ APPROVE ✅
    ├─ REQUEST_CHANGES ❌
    └─ COMMENT 💬
    ↓
保存到数据库
    ├─ decision: approve/request_changes/comment
    └─ decision_reason: 决策理由
```

## 📊 数据库记录

每个审查记录现在包含：

```sql
SELECT 
    id,
    overall_score,  -- AI评分 1-10
    decision,       -- approve/request_changes/comment
    decision_reason -- 决策理由
FROM pr_reviews;
```

## ⚠️ 重要提示

1. **首次部署**：建议 `enabled: false`，先观察效果
2. **Critical 问题**：建议保持 `block_on_critical: true`
3. **测试环境**：先在测试仓库验证，再应用到生产
4. **监控日志**：密切关注决策引擎的日志输出
5. **人工复审**：COMMENT 状态的 PR 需要人工审查

## 🎯 使用场景

### 自动批准

- 评分 >= 8 分
- 无 Critical 问题
- Major 问题 <= 1 个

### 自动阻断

- 评分 < 4 分
- 存在 Critical 问题

### 人工复审

- 评分 4-7 分（中间状态）
- Major 问题过多

## 🔧 故障排查

### Review 未提交

- 检查 GitHub App 权限
- 查看日志中的错误信息
- 确认 `review_policy.enabled` 是否为 true

### 决策不符合预期

- 检查 AI 评分是否准确
- 查看 `overall_score` 和问题分类
- 调整 `approve_threshold` 或 `block_threshold`

### 重复提交 Review

- 确认 `enable_idempotency_check: true`
- 检查机器人用户名是否正确获取

## 📈 后续优化方向

1. **学习反馈**：记录人工修改的决策，优化评分模型
2. **审批链**：支持需要多个批准的场景
3. **统计分析**：展示批准率、阻断率等指标
4. **智能调优**：根据历史数据自动调整阈值

## 🎉 总结

成功实现了一个**稳健且具有威慑力**的审查批准系统：

✅ 多维度决策（评分 + 问题严重程度）
✅ 幂等性保护（避免重复提交）
✅ 灵活配置（支持仓库级别覆盖）
✅ 完整日志（可追溯所有决策）
✅ 渐进式部署（观察期 -> 逐步启用）

这个功能将大大提升团队研发效率，同时保证代码质量！🚀
