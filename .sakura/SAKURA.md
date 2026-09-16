# Sakura AI Reviewer 概述

## 1. 项目简介
基于 AI 的智能 GitHub Pull Request 代码审查与 Issue 分析机器人，具备主动探索代码库的能力

## 2. 仓库信息
- 仓库名: Sakura520222/Sakura-AI
- 语言统计: Python: 8446905, HTML: 1111943, Shell: 357518, Dockerfile: 3135
- 累计反思 32 次

## 3. 核心审查原则
- 完整性验证：PR描述/提交/文件/diff一致核对；功能逐项勾选，差异>10%标minor
- 双轨评分：增量+整体；历史major未清零上限8；quick上限8
- 批准规范：approve须声明未解决问题；request_changes须对应major/blocking
- 高分审视：10/10≠无风险，须主动找安全/测试遗漏，警惕确认偏误
- 无评论须附验证依据：rg命令/CI链接等审查痕迹
- 增量审查须结案历史项并触发全库引用搜索；增量≠全局，环境变量/全局副作用须跨文件审查
- 增量后快速全局抽查：git grep关键常量/中间件全部引用链；已有major未解决须重评并保持阻断，高分approve不得掩盖

## 4. 硬规则（重点）
- CI失败标error，合并前须全绿run链接；workflow最小permissions: {}；CI须全库py_compile/import-all防语法级阻断；workflow权限/凭证改动须配套合约测试断言
- async路由同步I/O用asyncio.to_thread()
- 配置/函数签名增删改须全库rg验证消费点（含直接调用的测试）并列命令与结果；env注入统一配置模块，禁止业务代码setdefault
- 守护进程/补偿扫描幂等：唯一约束/Redis锁防并发重复触发
- 镜像版本绑定：固定tag或digest并校验；start.sh等脚本须fail-closed标签一致性断言
- loguru统一logger.exception()；except块标注#USER_INPUT_ERROR/#INTERNAL_ERROR并统一sanitize
- 全链路影响分析：schema修改检索所有读取点；迁移脚本幂等可回滚
- 禁止except Exception:；捕获具体异常；多异常捕获≤3类且as e
- Shell一等公民：fail-closed、ERR_前缀统一错误码、日志脱敏、幂等与回退路径；start.sh超100行改动须拆分PR或专项运维审查；非systemd环境显式检测并报错/fallback
- 跨平台抽象/环形依赖检测/安全改动强制major或error；性能解析改动须微基准
- 三镜像部署原子更新：capability协商、版本漂移检测、manifest一致性、幂等回滚；unit文件/PIDFile用os.replace原子写入并清理残留
- 依赖管理：pyproject/requirements/uv.lock同PR强制同步（CI uv lock --check）；子包updater依赖须与根项目一致；升级须附Release Notes、消费点核对与兼容性回归（如pydantic/redis/openai破坏性变更）
- 认证链路：响应副作用统一ASGI中间件，JWT续期前刷新DB claims，特权入口单一校验；Set-Cookie必检Secure/HttpOnly/SameSite；中间件加载顺序文档化；SESSION_MAX_AGE等安全常量变更须审计日志
- updater/daemon：install/uninstall幂等同退出码；关键配置原子写入+fsync
- 网络请求封装禁止方法内递归self._request（超时叠加/传输泄漏）；AST静态检测自调用；CI业务脚本统一JSON输出状态供工作流消费，禁止返回裸字符串

## 5. Agent/Worker/Webhook
- Shell白名单≠安全：检查$()反引号;&&|&；输出双层限额
- worker禁止无日志无退避的except:continue
- 统一取消机制：复用_cancel_events，取消幂等
- 状态枚举后端/前端/i18n同步；errors.py映射HTTP码与翻译条目

## 6. 知识库维护
- .sakura/增量追加或精确修改，禁止覆盖式重写
- 单一真值源：URL/版本常量集中；监控关键状态；行号白名单脆弱（随导入漂移），改用语义化标记（装饰器/关键字）+CI自动校验
- 配置集中backend/core/config.py，新配置env+文件双入口并CI校验

## 7. 最新反思要点（累计32次）
- PR580（主审+3轮增量）：uv.lock未同步与updater_client递归_request两个major多轮未闭环仍approve=误判，后续增量必须重评历史major否则阻断；CI permissions:{}+合约测试；superseded统一JSON输出；增量≥3次或累计>1000行自动切full-review
- ISSUE581：Gitflow同步冲突分类应为workflow/git非other；标签统一automation；补merge-conflict/ci-failure；重复检测=标题+关键字+近30天过滤；关闭前附解决commit注释；冲突自动建PR而非直接合并
- PR579(incr5)：行号白名单随导入变化失效→语义化标记；续期须在MFA校验后；has_webui_cookie守卫防覆盖；强制负向用例（token篡改/MFA未完成/用户禁用）
- PR579：滑动会话续期统一ASGI中间件（request.state挂起+http.response.start写入，覆盖RedirectResponse）；JWT续期前刷新DB权威claims防降权续期；特权入口统一校验防MFA绕过；401删Cookie防登录死循环；会话常量集中禁硬编码
- PR578：Gitflow分支前缀强制；合约类变更（三镜像）须端到端兼容+故障注入测试；审查摘要阻断项前置显眼；文档doc-diff同步
- 历史要点：PR576-577 增量阈值（≥30提交或>500行切full）、零调用依赖删除前查非代码消费点；PR572 py_compile全库防线；ISSUE570 三镜像原子更新

## 8. 技术栈
FastAPI (Python 3.14+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · 多协议AI（OpenAI/Anthropic/Gemini） · MySQL 8.0 + Redis + ChromaDB · GitHub App + OAuth · Docker Compose

*最后更新：基于 ISSUE581 与 PR580/PR579 反思（锁文件与递归调用增量闭环、CI最小权限合约测试、行号白名单语义化、Gitflow同步冲突分析），累计反思 32 次*
