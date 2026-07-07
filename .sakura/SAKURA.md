# Sakura AI Reviewer 概述

## 1. 项目简介
LLM 驱动的 PR 审查、Issue 分析、仓库扫描与 Agent 自动修复平台。具备 RAG、代码索引、项目记忆与增量审查能力。
**技术栈**：Python 3.11+/FastAPI/Jinja2+HTMX+Alpine/MySQL/Redis/ChromaDB/Docker/GitHub App/Telegram Bot

## 2. 仓库信息
- 仓库名: Sakura520222/Sakura-AI-Reviewer
- 语言: Python: 3561605, HTML: 901178, Shell: 16140, Dockerfile: 1637
- 累计反思 475 次

## 3. 审查核心原则
- 审查须核对 PR 描述、提交信息(type/scope 须与文件匹配)、文件清单与 diff 一致性。
- 增量审查须声明范围+双轨评分(增量质量+PR 整体评估)；历史 major 未清零评分上限 8；quick 上限 8。
- approve 须逐项声明未解决已知问题(≥minor)状态；request_changes 须对应明确 major/blocking。
- 正面评价越多越先做负面检查(隧道视野防范)。
- 结构化评论只发布具体可操作问题；摘要/评分不得转 review comment。

## 4. 硬规则
1. async 路由同步 I/O 须 asyncio.to_thread()。
2. 延迟导入须注释；配置废弃须全链路清理。
3. DB 枚举/状态新增须检查迁移+状态转移矩阵+超时逃脱。
4. DB schema 变更须检查 nullable/default/事务/并发。
5. LLM prompt 外部输入须有截断+清洗/包裹；移除截断须验证等效控制否则 major。
6. 终态方法缓存须验证清理(finalize=True)，遗漏=major。
7. 布尔/状态枚举新增须 grep 所有消费点。
8. 重复检测须含证据链(标题+相似+差异+结论)。
9. 大 PR(≥5k行)须逐模块审查；重构≥100行须声明测试/返回值/异常等价。
10. constants 变更须列消费方；软删除引入须追问累积/清理/恢复。
11. 安全相关须区分漏洞 vs 误杀(三角验证)。
12. 支付/计费变更须输出兼容性/回滚检查。
13. 新增 worker 须验证防重/重试/超时；映射表须有清理计划。

## 5. Agent/Worker/Webhook
- Agent 基类变更须列调用方检查返回值/异常/状态语义。
- Shell 白名单≠安全须检查 $()反引号;&&|&等；输出须双层限额。
- 长连接/worker 禁止 except:continue 无日志无退避。
- webhook 须检查签名/幂等/乱序/重复；成本是安全新维度。

## 6. FastAPI/WebUI
- CSRF/认证不得依赖必填校验；新路由须确认认证链路。
- /health 字段变更须检查同步与监控兼容；新字段须检查 i18n。

## 7. 测试与报告
- 安全解析须单测边界；fake 须贴近生产；审查输出须合规性自检。

## 8. 最新反思(PR #415–#418)
- #415 依赖图：正则解析须交叉验证字符集；不可逆转义须评估影响；审查输出自检(quick 9 分是规则自违反)；增量描述须声明范围边界。
- #417 大版本：10k+行"概览陷阱"须逐模块深挖；事件驱动须系统检查防重/乱序/并发/清理；prompt 注入防护为 hard rule；新增状态须全仓 grep；"正确修复清单"≠免检；跨增量 minor 安全问题须升级策略。
- #418 Star Aid：辅助功能存储须声明增长/清理/TTL；权限检查层级一致性(路由 vs 服务)；前序问题≥2轮未修须升级；测试静默失效比 bug 更危险；quick 省验证深度非声明义务。

*最后更新：基于 PR #415–#418，累计反思 475 次*
