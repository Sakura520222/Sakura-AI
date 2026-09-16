# Sakura AI Reviewer 概述

## 1. 项目简介
基于 AI 的智能 GitHub Pull Request 代码审查与 Issue 分析机器人，具备主动探索代码库的能力

## 2. 仓库信息
- 仓库名: Sakura520222/Sakura-AI
- 语言统计: Python: 8446905, HTML: 1111943, Shell: 357518, Dockerfile: 3135
- 累计反思 33 次

## 3. 核心审查原则
- 完整性验证：PR描述/提交/文件/diff一致核对；功能逐项勾选，差异>10%标minor
- 双轨评分：增量+整体；历史major未清零上限8；quick上限8
- 批准规范：approve须声明未解决问题；request_changes须对应major/blocking
- 高分审视：10/10≠无风险，须主动找安全/测试遗漏，警惕确认偏误
- 无评论须附验证依据：rg命令/CI链接等审查痕迹
- 增量审查须结案历史项并触发全库引用搜索；增量≠全局，环境变量/全局副作用须跨文件审查
- 增量模板强制列出"前置未解决问题"：历史major未修复须保持阻断，quick approve不得掩盖全局风险（PR580教训）

## 4. 硬规则（重点）
- CI失败标error，合并前须全绿run链接；workflow最小permissions并配套contract test断言；改动须提供影响矩阵（作业/env/secrets），正则扫secrets./sudo/write-all
- CI须全库py_compile/import-all防语法级阻断
- async路由同步I/O用asyncio.to_thread()
- 配置/函数签名增删改须全库rg验证消费点（含直接调用的测试）并列命令与结果；env注入统一配置模块，禁止业务代码setdefault
- 锁文件即发布根基：pyproject/requirements/updater子包依赖与uv.lock同PR强制同步，CI跑uv lock --check+uv sync --locked阻断；主流库升级（pydantic/redis等）须附Release Notes、破坏性迁移说明与兼容性测试（如redis RESP3默认协议）
- 递归调用禁令：网络封装层禁self._request递归（超时叠加/资源泄漏/死锁），用独立低层函数或@transport_once装饰器，AST静态检测自调用
- 守护进程/补偿扫描幂等：唯一约束/Redis锁防并发重复触发
- systemd/脚本：unit/PIDFile原子写入os.replace+异常清理；单实例锁；权限最小化；非systemd环境显式报错或fallback
- loguru统一logger.exception()；except块标注#USER_INPUT_ERROR/#INTERNAL_ERROR并统一sanitize；禁止except Exception:，捕获具体异常≤3类且as e
- 全链路影响分析：schema修改检索所有读取点；迁移脚本幂等可回滚
- Shell一等公民：fail-closed、ERR_前缀错误码、日志脱敏、幂等与回退；>100行大块改动拆分PR+专项安全/运维审查（shellcheck/actionlint辅助）
- 跨平台抽象/环形依赖检测/安全改动强制major或error；性能解析改动须微基准
- 三镜像部署原子更新：capability协商、版本漂移检测、manifest一致性、幂等回滚；业务脚本影响CI时输出统一JSON状态（如superseded），禁返回裸字符串
- 认证链路：响应副作用统一ASGI中间件，JWT续期前刷新DB claims，特权入口单一校验；Cookie续期路径CSRF校验；中间件加载顺序文档化
- updater/daemon：install/uninstall幂等同退出码；关键配置原子写入+fsync

## 5. Agent/Worker/Issue流程
- Shell白名单≠安全：检查$()反引号;&&|&；输出双层限额
- worker禁止无日志无退避的except:continue
- 统一取消机制：复用_cancel_events，取消幂等
- 状态枚举后端/前端/i18n同步；errors.py映射HTTP码与翻译条目
- Gitflow自动同步冲突：合并前预检自动建Issue标ci-blocking优于CI失败；分类用workflow/gitflow禁笼统other，阻断CI提high；标签复用已有集合防碎片化（维护LABELS.md，automation/automated统一）；重复检测比对冲突SHA区分duplicate/related；关闭时注明解决commit便于追溯

## 6. 知识库维护
- .sakura/增量追加或精确修改，禁止覆盖式重写
- 单一真值源：URL/版本常量集中；监控关键状态
- 配置集中backend/core/config.py，新配置env+文件双入口并CI校验

## 7. 最新反思要点（累计33次）
- PR580(含incr2/incr3)：3.2.1大型发布PR（70+文件/5668行）——两major（uv.lock未同步、_request递归）多轮增量仍未闭环却被approve；CI权限最小化permissions:{}须配套contract test；发布渠道回滚保护基于Git HEAD标签漂移检测；CI决策依赖脚本outputs.advanced须全工作流统一消费；大改动分层审查，PR超3次增量或累计>800行自动切full-review并报告历史major闭环状态
- ISSUE581/582：main→develop自动同步冲突治理——预检建Issue优于CI直接失败；分类workflow、阻断标high；标签去碎片化；重复检测比对冲突SHA
- 历史要点：PR579 滑动会话续期ASGI中间件化、JWT续期前刷新DB claims、MFA绕过与登录死循环治理；PR578 Gitflow分支前缀强制、doc-diff同步；PR576-577 增量阈值（≥30提交或>500行切full）、删依赖前查非代码消费点；PR572 py_compile全库防线；ISSUE570 三镜像原子更新

## 8. 技术栈
FastAPI (Python 3.14+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · 多协议AI（OpenAI/Anthropic/Gemini） · MySQL 8.0 + Redis + ChromaDB · GitHub App + OAuth · Docker Compose

*最后更新：基于 PR #580 及增量轮次（依赖锁同步/递归调用/CI契约测试/脚本-CI耦合）与 ISSUE #581-#582（Gitflow 同步冲突治理）反思，累计反思 33 次*
