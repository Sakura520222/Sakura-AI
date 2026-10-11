# 项目记忆

累计反思 22 次

## 核心审查原则

- **"无评论"≠"无问题"**：空结果可能源于工具故障；结论须附验证依据（搜索命令、CI 链接）
- **高分警惕确认偏误**：高分仍须负向用例验证；已有 major 未决项时，即使增量干净也须保持阻断
- **审查策略动态升级**：≥30 commits / >500 行、或增量 ≥3 轮 / 累计 >1000 行时切 full；最简变更宜 medium
- **Fail-Closed**：宁可误报不可漏报；文档不可作验证依据；在数据源/配置扰动或超限时严格实施 Fail-Closed 校验
- **报告结构化**：摘要→关键风险→建议；阻断项须在摘要阶段可见

## 依赖、CI 与工作流安全

- **锁文件同步=阻断项**：改 pyproject/requirements 必须同 PR 更新 uv.lock；CI 加 `uv sync --locked` 校验
- **Dependabot 触发器与 Secrets 硬防线**：机器人工作流若需写权限或 secrets 禁用于 `pull_request`（secrets 恒为空），须用 `pull_request_target` 并严格校验 `if: github.event.pull_request.user.login == 'dependabot[bot]'`；最小化 permissions，禁止将 Token 明文拼入 git push URL
- **uv 升级与断言**：升级依赖优先单包精准锁 `uv lock --upgrade-package <pkg>`；workflow 显式 `permissions: {}` 并配套 Python 测试断言关键 YAML 配置防回退

## 架构隔离与业务设计

- **核心配置层隔离 (Core Config Isolation)**：`backend/core/config.py` 禁止顶层静态导入含重型依赖或可选 SDK 的模块；动态选项须采用函数惰性加载，防止导入副作用致全系统瘫痪
- **端点多语言与准入零信任**：同一 API 端点内所有异常分支须统一用户语言（`lang=user_lang`）；计费准入上下文须由服务端强行覆盖，禁止信任客户端付款主体字段
- **配置清理 5 要素**：改动配置须同步 `config.py`、`DYNAMIC_CONFIG_GROUPS`、WebUI 模板、翻译字典及 `.deploy/*.env.example`
- **UI Clamp Removal 防线**：移除截断须核验容器弹性高度（`min-h-*`）、全域一致性与 `whitespace-pre-wrap`

## 异步并发与 Agent Harness

- **异步取消排空 (Cancellation Drain) 观测性**：使用 `asyncio.shield` 循环排空任务时，若底层异常被捕获，须保留诊断日志后方可 break 并重抛 `CancelledError`，严禁完全吞没异常
- **状态恢复防御一致性 (Checkpoint Guard)**：历史检查点恢复时，下游反序列化须保持与前置 Validator 同级防御（统一 `.get()`），禁止硬下标访问引发未处理异常
- **并发所有权锁**：引用计数递增须在 acquire 等待前完成；清理资源须严格为 `users == 0`，严禁 release 即删

## 语法、解析器与 AI 协议治理

- **已确认误报：无括号多异常捕获**：运行时为 Python 3.14+；依据 PEP 758，`except TypeError, ValueError:` 是合法语法，严禁误判阻断
- **AI 结构化解析与容错收敛**：严禁散落裸调 `json.loads`，统一使用带 Markdown 代码块剥离与 recovery 引导的解析函数；协议适配层严格区分 `0` 与 `None` 边界
- **AI 故障兜底与类型防线**：模型调用全盘失败时显式提示“治理服务暂时不可用”，严禁静默生成空报告；拒绝 `bool` 伪装 `int`（用 `type(x) is not int`）；行号参数物理钳制

## 增量审查与协作规范

- **增量+全局双模**：增量审查须与全局架构意图 cross-check，防范局部改动破坏全局降级契约
- **配置校验优先于早退 (Guard Before Early-Exit)**：配置合法性与安全边界检测置于任何逻辑早退之前
