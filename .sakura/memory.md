# 项目记忆

累计反思 5 次

## 核心审查原则

- **"无评论"≠"无问题"**：空结果可能源于工具故障；结论须附验证依据（搜索命令、CI 链接）
- **高分警惕确认偏误**：高分仍须负向用例验证；已有 major 未决项时，即使增量干净也须保持阻断
- **审查策略动态升级**：≥30 commits / >500 行、或增量 ≥3 轮 / 累计 >1000 行时切 full；最简变更宜 medium
- **Fail-Closed**：宁可误报不可漏报；文档不可作验证依据
- **报告结构化**：摘要→关键风险→修复建议；阻断项须在摘要阶段可见

## 依赖与锁文件

- **锁文件同步=阻断项**：改 pyproject/requirements 必须同 PR 更新 uv.lock；CI 加 `uv lock --check`
- **升级必附 Release Notes**：核对 Minor/Major 升级 Breaking Changes 与隐式 Extra（如 SQLAlchemy [asyncio] / greenlet 解耦）
- **依赖清理契机**：依赖集中合并 PR 需全库检索无引用包（如闲置依赖 python-slugify），及时清退降低攻击面；子项目依赖须与根项目一致

## 大规模删除与重构

- **配置与清理完备度**：移除配置项须同步核验 5 要素：`config.py`、`DYNAMIC_CONFIG_GROUPS`、WebUI 模板 (`templates/`)、多语言字典 (`translations/`) 及 `.deploy/*.env.example`
- **契约变更防线**：显式移除/解耦业务路径时，须包含负向断言测试（Negative Tests），防逻辑回退；删后统一跑格式化（`ruff format`）
- **有意的架构收敛**：若删除了某底层功能且无直接替代，只要文档同步+负向测试覆盖+符合既定蓝图，判定为“有意的架构收敛”，不作无谓阻断

## 语法、静态解析与异常防护

- **CI 语法与全模块导入硬防线**：语法错误（如 Python 2 遗留 `except A, B:`）会导致 pytest 收集阶段崩溃；须在 CI 前置执行 `python -m py_compile` 及全模块 import 校验，0 容忍阻断
- **Best-Effort 防御规范**：辅助落库/诊断逻辑若用宽泛捕获，须收敛为具体基类异常（如 `SQLAlchemyError`/`OSError`），注释标注 `#INTERNAL_ERROR` 与 `best-effort` 说明，严禁裸用 `except Exception`
- **AI 工具与索引防御**：外部/模型传入的行号切片参数须物理钳制：`0 <= min_idx <= max_idx <= len(sequence)`，严禁依赖 Python 切片静默容错（防负索引倒序切片）；校验 1-based 与 0-based 映射转换

## 会话认证与中间件

- **Cookie 写入统一走 ASGI 中间件**（http.response.start 阶段）；显式 Secure/HttpOnly/SameSite；幂等守卫防重复写
- **续期前刷新权威 Claims**：重查 DB 防禁用用户续命；新增 DB 查询评估负载与缓存
- **特权入口单一校验**：管理员 API 不得绕过 MFA 链；401 处理器须删 Cookie 防死循环

## CI/工作流与 Issue 治理

- **Epic 级重构 Issue 治理**：跨周期大重构重点在于拆解路径与双轨共存风险；重复检测时严格区分整体架构提案与单业务域 Issue，避免误报
- **最小权限+契约测试**：workflow 显式 `permissions: {}` 并配套断言测试；正则扫 secrets./sudo

## 增量审查与协作

- **增量+全局双模**：每轮增量后全局抽查全部调用点，复查前轮 major 项闭环状态，结束后全局回归
- **模板强制列"前置未解决问题"**：未解决 → 阻断合并
