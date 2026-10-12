# 项目记忆

累计反思 27 次

## 核心审查原则

- **"无评论"≠"无问题"**：空结果可能源于工具故障；结论须附验证依据（搜索命令、CI 链接）
- **高分警惕确认偏误**：高分仍须负向用例验证；已有 major 未决项时，即使增量干净也须保持阻断
- **审查策略动态升级**：≥30 commits / >500 行、或增量 ≥3 轮 / 累计 >1000 行切 full；最简改动宜 medium
- **Fail-Closed**：宁可误报不可漏报；文档不作验证依据；数据源/配置扰动或超限时严格 Fail-Closed
- **报告结构化**：摘要→关键风险→建议；阻断项在摘要阶段可见

## 依赖、CI 与工作流安全

- **依赖改动三要素（阻断项）**：
  1. 声明双向对齐（`pyproject.toml` 与 `requirements.txt`）；
  2. 锁文件强制勾稽（改依赖必带 `uv.lock`，CI 冒烟失败且无锁文件必阻断）；
  3. 精准单包升级优先（推荐 `uv lock --upgrade-package <pkg>`，防无关依赖漂移）。
- **Dependabot 联动与工作流安全**：
  - Dependabot 原生仅改声明不锁 `uv.lock`，警惕 Release Notes 麻痹心理；
  - 自动补锁严禁裸用 `pull_request`，须用 `pull_request_target` 并严格校验 `user.login == 'dependabot[bot]'`；最小化权限，禁止 Token 明文拼入 URL；
  - 建议适配机器人场景，区分“维护者/CI 追加提交”与“Dependabot 指令（`@dependabot rebase`）”。

## 架构重构、隔离与业务设计

- **记忆重构与多段管线**：切断写入用户仓库逻辑（防权限污染与 CI 循环），记忆重构转数据库事务与防重锁；“总结→候选→裁决”管线防 Schema 降级与 NPE；零继承废弃工具防 Checkpoint 异常。
- **核心配置层隔离**：`backend/core/config.py` 禁止顶层静态导入重型依赖/可选 SDK；动态选项函数惰性加载。
- **端点多语言与准入零信任**：同一端点各异常分支统一 `lang=user_lang`；计费准入服务端强行覆盖。
- **配置清理 5 要素**：改动配置同步 `config.py`、`DYNAMIC_CONFIG_GROUPS`、WebUI、翻译字典及 `.deploy/*.env.example`。
- **UI Clamp Removal**：移除截断须核验容器弹性高度（`min-h-*`）、全域一致性与 `whitespace-pre-wrap`。

## 异步并发与 Agent Harness

- **异步取消排空观测性**：使用 `asyncio.shield` 循环排空时，若底层异常被捕获，必须打诊断日志后方可 break 并重抛 `CancelledError`，严禁吞没。
- **Checkpoint 防御一致性**：检查点反序列化须与前置 Validator 保持同级防御（统一 `.get()` 或 Schema 解码），禁硬下标访问引发未处理异常。
- **并发所有权锁**：引用计数递增须在 acquire 等待前完成；清理资源严格为 `users == 0`。

## 语法、解析器与 AI 协议治理

- **已确认误报：PEP 758**：运行时为 Python 3.14+；`except TypeError, ValueError:` 是合法语法，严禁误判。
- **AI 结构化解析容错**：统一带 Markdown 剥离与 recovery 解析，严格区分 `0` 与 `None`。
- **AI 故障兜底与类型防线**：全盘失败显式提示“服务暂时不可用”，严禁静默空报告；拒绝 `bool` 伪装 `int`（`type(x) is not int`）；行号物理钳制。

## 增量审查与协作规范

- **增量+全局双模**：增量审查须 cross-check 全局契约，配置校验优先于早退。
