# 项目记忆

累计反思 28 次

## 核心审查原则

- **"无评论"≠"无问题"**：空结果可能源于工具故障；结论须附验证依据（搜索命令、CI 链接）
- **高分警惕确认偏误**：高分仍须负向用例验证；已有 major 未决项时，即使增量干净也须保持阻断
- **审查策略动态升级**：≥30 commits / >500 行、或增量 ≥3 轮 / 累计 >1000 行时切 full；依赖声明改动即便 quick 亦须强制核验锁文件与子包一致性
- **Fail-Closed**：宁可误报不可漏报；文档不可作验证依据；在数据源/配置扰动或超限时严格实施 Fail-Closed 校验
- **报告结构化**：摘要→关键风险→建议；阻断项须在摘要阶段可见

## 依赖、CI 与工作流安全

- **依赖三要素与锁文件阻断项**：改动 `pyproject.toml`/`requirements.txt` 必须同 PR 纳入 `uv.lock` 且对齐子项目（`sandboxer`/`updater`）依赖下限；CI 必跑 `uv sync --locked` 拦截。依赖升级第 1 顺位：核查 lockfile 是否在 diff 且 CI `--locked` 是否通过
- **精准锁升级与 Bot 审查防线**：升级优先 `uv lock --upgrade-package <pkg>` 防无关依赖大范围漂移；Dependabot PR 默认易漏 `uv.lock` 导致冒烟测试 exit code 1，审查切忌因版本 patch/纯数据包而麻痹 Approve；提供建议须区分维护者推分支与 Bot 特殊操作
- **Dependabot 自动追锁与 Secrets 硬防线**：机器人工作流若需写权限或 secrets 禁用于 `pull_request`，须用 `pull_request_target` 并严格校验 `if: github.event.pull_request.user.login == 'dependabot[bot]'`；最小化 permissions，禁止将 Token 明文拼入 git push URL；建议配置自动补锁工作流闭环 Dependabot 遗漏

## 架构隔离与业务设计

- **核心配置层隔离 (Core Config Isolation)**：`backend/core/config.py` 禁止顶层静态导入含重型依赖或可选 SDK 的模块；动态选项须采用函数惰性加载，防止导入副作用致全系统瘫痪
- **端点多语言与准入零信任**：同一 API 端点内所有异常分支须统一用户语言（`lang=user_lang`）；计费准入上下文须由服务端强行覆盖，禁止信任客户端付款主体字段
- **配置清理 5 要素**：改动配置须同步 `config.py`、`DYNAMIC_CONFIG_GROUPS`、WebUI 模板、翻译字典及 `.deploy/*.env.example`
- **UI Clamp Removal 防线**：移除截断须核验容器弹性高度（`min-h-*`）、全域一致性与 `whitespace-pre-wrap`

## 异步并发与 Agent Harness

- **异步取消排空 (Cancellation Drain) 观测性**：使用 `asyncio.shield` 循环排空任务时，若底层异常被捕获，须保留诊断日志后方可 break 并重抛 `CancelledError`，严禁完全吞没异常
- **状态恢复与废弃切断一致性 (Checkpoint Guard)**：历史检查点恢复时下游反序列化须保持与前置 Validator 同级防御（统一 `.get()`）；废弃旧持久化/工具（如切断用户仓写入）时，需防御历史未决任务硬访问引发空指针
- **并发事务与所有权锁**：跨多段裁决管线（如总结→主代理）须设事务级防重/乐观锁；引用计数递增须在 acquire 前完成，资源清理严格为 `users == 0`

## 语法、解析器与 AI 协议治理

- **已确认误报：无括号多异常捕获**：运行时为 Python 3.14+；依据 PEP 758，`except TypeError, ValueError:` 是合法语法，严禁误判阻断
- **多段 AI 流水线解析与容错收敛**：严禁散落裸调 `json.loads`，统一使用带 Markdown 剥离与 recovery 引导的解析函数；多角色（辅助/主控）管线需防御跨阶段 Schema 降级与解析异常；协议适配层严格区分 `0` 与 `None` 边界
- **AI 故障兜底与类型防线**：模型调用全盘失败时显式提示“治理服务暂时不可用”，严禁静默生成空报告；拒绝 `bool` 伪装 `int`（用 `type(x) is not int`）；行号参数物理钳制

## 增量审查与协作规范

- **增量+全局双模**：增量审查须与全局架构意图 cross-check，防范局部改动破坏全局降级契约；重构/破坏性变更须规范打标（`breaking-change`, `architecture`）
- **配置校验优先于早退 (Guard Before Early-Exit)**：配置合法性与安全边界检测置于任何逻辑早退之前
