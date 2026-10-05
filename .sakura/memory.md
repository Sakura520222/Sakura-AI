# 项目记忆

累计反思 12 次

## 核心审查原则

- **"无评论"≠"无问题"**：空结果可能源于工具故障；结论须附验证依据（搜索命令、CI 链接）
- **高分警惕确认偏误**：高分仍须负向用例验证；已有 major 未决项时，即使增量干净也须保持阻断
- **审查策略动态升级**：≥30 commits / >500 行、或增量 ≥3 轮 / 累计 >1000 行时切 full；最简变更宜 medium
- **Fail-Closed**：宁可误报不可漏报；文档不可作验证依据；在数据源/配置存在扰动或超限时严格实施 Fail-Closed 校验，确保分析结论和配置加载绝对可靠
- **报告结构化**：摘要→关键风险→修复建议；阻断项须在摘要阶段可见

## 依赖与锁文件

- **锁文件同步=阻断项**：改 pyproject/requirements 必须同 PR 更新 uv.lock；CI 加 `uv sync --locked` / `uv lock --check` 校验；Dependabot 等机器人 PR 若漏更新锁文件一律 Fail-Closed 阻断
- **Dependabot/Bot PR 防御**：机器人生成的依赖提升 PR 默认易漏 `uv.lock`，审查时优先检查锁文件改动，缺失则直接阻断并提示使用 `uv lock` 或 `@dependabot rebase`
- **uv 升级指引**：升级依赖优先使用精准单包锁命令 `uv lock --upgrade-package <package_name>`，避免删锁引致非预期依赖变动
- **升级必附 Release Notes**：核对 Minor/Major 升级 Breaking Changes 与隐式 Extra（如 SQLAlchemy [asyncio] / greenlet 解耦）
- **依赖清理契机**：依赖集中合并 PR 需全库检索无引用包，及时清退降低攻击面；子项目依赖须与根项目一致

## 大规模删除与重构

- **配置与清理完备度**：移除/调整配置项须同步核验 5 要素：`config.py`、`DYNAMIC_CONFIG_GROUPS`、WebUI 模板 (`templates/`)、多语言字典 (`translations/`) 及 `.deploy/*.env.example`
- **契约变更防线**：显式移除/解耦业务路径时，须包含负向断言测试（Negative Tests），防逻辑回退；删后统一跑格式化（`ruff format`）
- **有意的架构收敛**：若删除了某底层功能且无直接替代，只要文档同步+负向测试覆盖+符合既定蓝图，判定为“有意的架构收敛”，不作无谓阻断

## 语法、静态解析与防护

- **CI 语法与全模块导入硬防线**：经 Python 3.14+ 确认的语法错误可能导致 pytest 收集阶段失败；须在相同版本的 CI 环境前置执行 `python -m py_compile` 及全模块 import 校验，0 容忍阻断
- **已确认误报：无括号多异常捕获（PR641/PR643）**：本项目运行时为 Python 3.14+；依据 PEP 758，`except TypeError, ValueError:` 是合法语法。不得将此误报复用或判为阻断项
- **Best-Effort 防御规范**：辅助落库/诊断逻辑若用宽泛捕获，须收敛为具体基类异常（如 `SQLAlchemyError`/`OSError`），注释标注 `#INTERNAL_ERROR` 与 `best-effort` 说明，严禁裸用 `except Exception`
- **AI 工具与索引防御**：外部/模型传入的行号切片参数须物理钳制：`0 <= min_idx <= max_idx <= len(sequence)`；遇到工具错误参数时须返回结构化 recovery 引导
- **解析器与布尔防线**：拒绝 `bool` 伪装 `int`（用 `type(x) is not int`）；零行/纯元数据 Patch 解析放宽时必须限制 downstream 生成结构禁用文本引用（ungrounded）

## 错误治理、降级与日志规范

- **回退断言约束 (Fallback Reason Guard)**：宽泛捕获/降级逻辑若无法区分“资源不存在 (404)”与“瞬时网络错误 (5xx/Timeout)”，面向模型或 UI 的文案必须使用条件式表述（如“可能已删除或暂不可用”），严禁硬编码确定性断言
- **Recovery 提示与测试解耦**：结构化 Recovery 字段在优化文案时，需兼顾语义准确度与现有单测子串匹配契约，可采用追加上下文或保留前缀方式
- **第三方心跳日志收敛**：第三方库（如 APScheduler, httpx）即使保持后台观察运行，其 INFO 级心跳日志必须加入 `logging_bridge` 过滤屏蔽，避免日志污染；过滤修改须同时包含正向与负向测试

## 并发、数据库与异步模式

- **并发所有权锁 (Ownership Lock)**：引用计数递增必须在进入 acquire 等待之前完成；清理资源的条件必须严格为 `users == 0`，严禁 release 即删键
- **数据迁移 SQL 下推与编译防线**：大数据量去重/清理严禁全表加载到 Python 内存，必须下推至 DB 并包含 ORM 方言编译测试，同步限制单次 SQL 执行上限
- **IO/RPC 校验防重**：同一生命周期内多次 RPC 调用须判断状态是否突变，避免无谓重复请求

## 会话认证与中间件

- **Cookie 写入统一走 ASGI 中间件**（http.response.start 阶段）；显式 Secure/HttpOnly/SameSite；幂等守卫防重复写
- **续期前刷新权威 Claims**：重查 DB 防禁用用户续命；新增 DB 查询评估负载与缓存
- **特权入口单一校验**：管理员 API 不得绕过 MFA 链；401 处理器须删 Cookie 防死循环

## CI/工作流与 Issue 治理

- **Epic 级重构 Issue 治理**：跨周期大重构须拆解路径与双轨共存风险；优先独立合并阶段 1 的紧急 Bug；重复检测严格区分整体提案与单业务 Issue
- **最小权限+契约测试**：workflow 显式 `permissions: {}` 并配套断言测试；正则扫 secrets./sudo

## 增量审查与协作

- **增量+全局双模**：每轮增量后全局抽查全部调用点，复查前轮 major 项闭环状态，结束后全局回归
- **模板强制列"前置未解决问题"**：未解决 → 阻断合并
- **配置校验优先于早退 (Guard Before Early-Exit)**：配置合法性校验与安全边界检测必须置于任何逻辑早退分支之前
