# Sakura AI Reviewer 概述

## 1. 项目简介
基于 AI 的智能 GitHub Pull Request 代码审查与 Issue 分析机器人，具备主动探索代码库的能力。

## 2. 仓库信息
- 仓库名: Sakura520222/Sakura-AI
- 语言统计: Python: 8714382, HTML: 1115469, Shell: 357518, Dockerfile: 3135
- 累计反思 5 次

## 3. 核心审查原则
- 完整性验证：PR描述/提交/文件/diff一致核对；功能逐项勾选，差异>10%标minor。
- 删减类/重构核验：批量清理逻辑后须跑格式化（如 ruff format）；移除配置项须核验 config.py、DYNAMIC_CONFIG_GROUPS、templates/、translations/、.env 五要素；验证路径遵从代码引用 -> 配置声明 -> UI渲染 -> i18n -> CI/部署环境。
- 双轨评分：增量+整体；历史 major 未清零上限 8；quick 上限 8。
- 批准规范：approve 须声明未解决问题；request_changes 须对应 major/blocking。行为变更符合收敛意图且有文档/测试守护的不阻断。
- 高分审视：10/10≠无风险，须主动找安全/测试遗漏，警惕确认偏误。
- 痕迹与边界：无评论须附验证依据；AI 工具索引必须物理钳制 `0 <= min_idx <= max_idx <= len` 防负切片；重试契约须单测断言闭环。
- 增量审查：增量须结案历史项并触发全库引用搜索；历史 major 未解决须保持阻断。

## 4. 硬规则（重点）
- CI 与语法防线：CI 失败标 error，合并前须全绿；workflow 权限最小化；CI 最早期必须全库 `py_compile` + `import-all`，语法/导入错误零容忍；多异常捕获禁止 Python 2 遗留 `except A, B:` 语法，强制 `except (A, B):`。
- 异步与配置：async 路由同步 I/O 用 `asyncio.to_thread()`；配置/函数签名增删改须全库 rg 校验；env 统一配置模块，禁止业务代码 `setdefault`。
- 辅助落库/诊断：非主流程防御性捕获需收敛为底层基类异常（如 `SQLAlchemyError`），标注 `#INTERNAL_ERROR` 与 best-effort 说明，严禁裸 `except Exception:`。
- 幂等与锁：守护进程/扫描唯一约束与锁防并发重复；updater/daemon 操作幂等且原子写入+fsync。
- 依赖管理：pyproject/requirements/uv.lock 同步（CI `uv lock --check`）；依赖 minor 升级须查 Release Notes（如 SQLAlchemy 2.1 解耦 greenlet 的 extra 标记）；升级排查闲置依赖（Unused Dependencies）。
- 认证与安全：响应副作用统一 ASGI 中间件，JWT 续期前刷新 DB claims；Set-Cookie 必检 Secure/HttpOnly/SameSite。
- 网络请求：禁止方法内递归 `self._request`；超时与重试参数校验防死循环。

## 5. Agent/Worker/Webhook/Issue 治理
- Shell 安全：检查 `$()` 反引号及逻辑运算符；白名单+双层限额输出。
- Worker：禁止无日志无退避的异常忽略；统一复用 `_cancel_events` 幂等取消。
- Epic 级 Issue 治理：大版本/架构迁移重点放在拆解路径（Phases）与双轨共存兼容（认证/路由/状态）；区分 Epic 提案与单点优化防误报重复；规范标题保留无改写。

## 6. 知识库与集中配置
- .sakura/ 增量追加或精确修改，禁止覆盖式重写。
- 行号白名单改用语义化标记；单点真值源。配置集中 `backend/core/config.py`，新配置 env+文件双入口并 CI 校验。

## 7. 最新反思要点
- PR642：大范围删减重构需验证配置/UI/i18n/环境 5 要素；移除废弃通知路径配齐负向断言测试守护契约。
- PR641：基础语法错误与导入失败须在 CI 最早期（`py_compile` / `import-all`）0 容忍阻断；多异常捕获禁用 Python 2 语法；辅助落库限制底层异常类并显式标记 best-effort。
- PR637：大模型工具切片防负数倒序索引；重试契约参数必须通过自动化测试闭环验证。
- PR636：依赖集中清理合并时静态检测闲置依赖；库升级核对 Release Notes 防止隐式 Extra 缺失。
- ISSUE638：Epic 级重构评估双轨共存风险与阶段拆解，优化分类标签与重复检测识别。

## 8. 技术栈
FastAPI (Python 3.14+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · 多协议 AI（OpenAI / Anthropic / Gemini） · MySQL 8.0 + Redis + ChromaDB · GitHub App + OAuth · Docker Compose

*最后更新：基于 PR642/PR641/PR637/PR636/ISSUE638 最新反思整合，精确标注累计反思 5 次*
