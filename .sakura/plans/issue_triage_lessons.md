# Issue 分析与 triage 规范

## 分类与优先级
- 简短 Issue 不应过度推断；必须区分事实、推测、待确认。
- 优先级应基于影响范围：是否默认配置、是否核心流程、是否阻塞开发、是否影响生产。
- “记录耗时”属于 observability/monitoring，不等同于性能优化。
- Gitflow `main -> develop` 同步失败通常是 maintenance/ci/merge-conflict，优先级取决于 develop 是否为活跃开发基线。

## 标签建议
优先使用仓库已有标签。若不确定，写“若存在则添加”。避免为了单个 Issue 创建过细标签。

AI API 失败可考虑：`bug`、`configuration`、`external-api`、`ai-provider`、`needs-investigation`。配置置信度不要过高，除非已确认模型/API Base/payload。

## 重复检测
- 自动 Gitflow conflict Issue 模板相似但通常不是重复；需比较 workflow run URL、run id、分支 head SHA、冲突文件、创建时间。
- 启动相关需求要区分 startup duration、startup timestamp、uptime。
- AI/API 问题需比较触发模块、错误类型、错误信息、模型/API Base、影响结果。
- 不确定时标“相关”，不要标“重复”。

## 标题改写
标题应保留需求边界，不擅自扩大范围。优先不在标题放 `[priority]`，用标签/字段表达。

## 可行性建议
给出分层工作量：最小实现、API 暴露、WebUI/i18n/测试、架构重构。涉及 workflow 的 Issue 应先查看日志确认失败步骤。
