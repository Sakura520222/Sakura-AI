# Sakura AI Reviewer 概述

## 1. 项目简介
基于 AI 的智能 GitHub Pull Request 代码审查与 Issue 分析机器人，具备主动探索代码库的能力

## 2. 仓库信息
- 仓库名: Sakura520222/Sakura-AI
- 语言统计: Python: 8277389, HTML: 1112221, Shell: 324499, Dockerfile: 3061
- 累计反思 27 次

## 3. 核心审查原则
- 完整性验证：PR描述/提交/文件/diff一致核对；功能逐项勾选，差异>10%标minor
- 双轨评分：增量+整体；历史major未清零上限8；quick上限8
- 批准规范：approve须声明未解决问题；request_changes须对应major/blocking
- 高分审视：10/10≠无风险，须主动找安全/测试遗漏，警惕确认偏误
- 无评论须附验证依据：rg命令/CI链接等审查痕迹
- 增量审查须结案历史项并触发全库引用搜索；增量≠全局，环境变量/全局副作用须跨文件审查

## 4. 硬规则（重点）
- CI失败标error，合并前须全绿run链接；workflow最小permissions；CI须全库py_compile/import-all防语法级阻断
- async路由同步I/O用asyncio.to_thread()
- 配置/函数签名增删改须全库rg验证消费点（含直接调用的测试）并列命令与结果；env注入统一配置模块，禁止业务代码setdefault
- 守护进程/补偿扫描幂等：唯一约束/Redis锁防并发重复触发
- 镜像版本绑定：固定tag或digest并校验；start.sh等脚本须fail-closed标签一致性断言
- loguru统一logger.exception()；except块标注#USER_INPUT_ERROR/#INTERNAL_ERROR并统一sanitize
- 全链路影响分析：schema修改检索所有读取点；迁移脚本幂等可回滚
- 禁止except Exception:；捕获具体异常；多异常捕获≤3类且as e
- Shell一等公民：fail-closed、ERR_前缀统一错误码、日志脱敏、幂等与回退路径
- 跨平台抽象/环形依赖检测/安全改动强制major或error；性能解析改动须微基准
- 三镜像部署原子更新：capability协商、版本漂移检测、manifest一致性、幂等回滚
- 依赖管理：pyproject/requirements逐行镜像+uv.lock同PR强制同步；升级须附Release Notes、全库消费点核对与回归验证
- 认证链路：响应副作用（Cookie/Header）统一ASGI中间件，JWT续期前刷新DB claims，特权入口单一校验
- updater/daemon：install/uninstall幂等同退出码；关键配置原子写入+fsync；非systemd环境须显式fallback或报错

## 5. Agent/Worker/Webhook
- Shell白名单≠安全：检查$()反引号;&&|&；输出双层限额
- worker禁止无日志无退避的except:continue
- 统一取消机制：复用_cancel_events，取消幂等
- 状态枚举后端/前端/i18n同步；errors.py映射HTTP码与翻译条目

## 6. 知识库维护
- .sakura/增量追加或精确修改，禁止覆盖式重写
- 单一真值源：URL/版本常量集中；监控关键状态
- 配置集中backend/core/config.py，新配置env+文件双入口并CI校验

## 7. 最新反思要点（累计27次）
- PR579(incr2-4)：滑动会话续期——响应阶段副作用（Cookie/Header）统一交ASGI中间件（queue_webui_token_renewal+request.state），禁止依赖注入Response污染路由签名；Set-Cookie须在http.response.start写入并覆盖RedirectResponse等特殊响应；同请求续期须幂等守卫只写一次
- PR579：JWT续期前须刷新DB权威claims（角色/active/邮箱），防禁用降权用户旧token续期；特权入口统一走公共校验防MFA绕过（error）；401处理器删Cookie防登录重定向死循环；会话常量集中定义禁硬编码漂移；Cookie属性Secure/HttpOnly/SameSite必检；Bearer/Query不续期须文档同步
- PR578：Gitflow分支前缀强制（feature/fix/refactor/docs/test/chore/perf/ci等）违规即阻断；禁止方法内递归self._request（短超时误判/递归死锁）；合约类变更（三镜像合约）须端到端兼容检查+故障注入测试；审查摘要阻断项必须前置显眼；文档与代码doc-diff同步
- 历史要点：PR576-577 增量阈值（≥30提交或>500行切full）、锁文件同PR同步（uv lock --check）、零调用依赖删除前查非代码消费点、升级核对Release Notes；PR573 alembic零导入≠可删；PR572 py_compile全库防线；ISSUE570 三镜像原子更新

## 8. 技术栈
FastAPI (Python 3.14+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · 多协议AI（OpenAI/Anthropic/Gemini） · MySQL 8.0 + Redis + ChromaDB · GitHub App + OAuth · Docker Compose

*最后更新：基于 PR #578-#579 WebUI 会话续期中间件化与部署链路反思（滑动会话/claims权威刷新/MFA绕过与登录死循环治理/Gitflow分支策略/递归请求治理），累计反思 27 次*
