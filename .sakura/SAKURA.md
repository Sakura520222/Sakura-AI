# Sakura AI Reviewer 概述

## 1. 项目简介
基于 AI 的智能 GitHub Pull Request 代码审查与 Issue 分析机器人，具备主动探索代码库的能力

## 2. 仓库信息
- 仓库名: Sakura520222/Sakura-AI
- 语言统计: Python: 8446905, HTML: 1111943, Shell: 357518, Dockerfile: 3135
- 累计反思 35 次

## 3. 核心审查原则
- 完整性验证：PR描述/提交/文件/diff一致核对；功能逐项勾选，差异>10%标minor
- 双轨评分：增量+整体；历史major未清零上限8；quick上限8
- 批准规范：approve须声明未解决问题；request_changes须对应major/blocking
- 高分审视：10/10≠无风险，主动找安全/测试遗漏，警惕确认偏误
- 无评论须附验证依据：rg命令/CI链接等审查痕迹
- 增量审查须结案历史项并触发全库引用搜索；增量≠全局，环境变量/全局副作用须跨文件审查
- 增量+全局双模：>2-3次增量或累计>800行自动切full-review，报告须列历史major闭环状态

## 4. 硬规则（重点）
- CI失败标error，合并前须全绿run链接；workflow最小permissions；CI须全库py_compile/import-all防语法级阻断
- async路由同步I/O用asyncio.to_thread()
- 配置/函数签名增删改须全库rg验证消费点（含测试）并列命令与结果；env注入统一配置模块
- 守护进程/补偿扫描幂等：唯一约束/Redis锁防并发重复触发
- 镜像版本绑定：固定tag或digest并校验；脚本fail-closed标签一致性断言
- loguru统一logger.exception()；except标注#USER_INPUT_ERROR/#INTERNAL_ERROR并统一sanitize
- 全链路影响分析：schema修改检索所有读取点；迁移脚本幂等可回滚
- 禁止except Exception:；捕获具体异常；多异常捕获≤3类且as e
- Shell一等公民：fail-closed、ERR_前缀错误码、日志脱敏、幂等与回退
- 跨平台抽象/环形依赖检测/安全改动强制major或error；性能解析改动须微基准
- 三镜像部署原子更新：capability协商、版本漂移检测、manifest一致性、幂等回滚
- 依赖管理：pyproject/requirements逐行镜像+uv.lock同PR强制同步（CI加uv lock --check）；升级附Release Notes与回归验证
- 认证链路：响应副作用统一ASGI中间件；JWT续期前刷新DB claims；特权入口单一校验
- updater/daemon：install/uninstall幂等同退出码；关键配置原子写入+fsync；非systemd显式fallback
- systemd unit审查：User=/CapabilityBoundingSet=等字段最小化，缺失须提示
- Gitflow自动同步：main→develop直接merge遇冲突即失败；须冲突预检（merge --no-commit）后自动开Draft PR并@维护者，禁止盲目直接push（受保护分支必失败）；冲突Issue须附冲突文件列表；阻断develop CI时优先级升high

## 5. Agent/Worker/Webhook
- Shell白名单≠安全：检查$()反引号;&&|&；输出双层限额
- worker禁止无日志无退避的except:continue；统一取消机制复用_cancel_events
- 状态枚举后端/前端/i18n同步；errors.py映射HTTP码与翻译条目

## 6. 知识库维护
- .sakura/增量追加或精确修改，禁止覆盖式重写
- .sakura/等机器生成目录为冲突高发区：约定合并窗口降低并行编辑；同步冲突宜重跑生成脚本而非手编
- 单一真值源：URL/版本常量集中；配置集中backend/core/config.py，新配置env+文件双入口

## 7. 最新反思要点（累计35次）
- PR580(incr3)：CI最小权限permissions:{}+workflow contract tests断言——安全契约必须配套可验证测试；quick增量只看改动文件，未复核历史major（uv.lock漂移/递归调用），增量审查不等于全局风险闭环；文档doc-diff须同步
- ISSUE581-584（Gitflow同步冲突系列）：重复检测须按分支方向+冲突文件+时间窗二次判定，相似≠重复防误关；分类other过泛应细化workflow/branch-management；标签体系统一（gitflow/merge-conflict/ci），防automated/automation碎片化，可维护LABELS.md；低优Issue关闭前留commit依据注释
- 历史要点：PR579 滑动会话续期ASGI中间件化+claims权威刷新+MFA绕过与登录死循环治理；PR578 Gitflow分支前缀强制+禁止方法内递归self._request；PR576-577 增量阈值（≥30提交或>500行切full）与锁文件同步；ISSUE570 三镜像原子更新

## 8. 技术栈
FastAPI (Python 3.14+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · 多协议AI（OpenAI/Anthropic/Gemini） · MySQL 8.0 + Redis + ChromaDB · GitHub App + OAuth · Docker Compose

*最后更新：基于 ISSUE #581-#584 Gitflow 同步冲突治理与 PR #580 增量审查/CI权限契约反思（冲突预检+Draft PR、重复检测二次判定、增量+全局双模、workflow contract tests），累计反思 35 次*
