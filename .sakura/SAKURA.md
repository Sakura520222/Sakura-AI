# Sakura AI Reviewer 概述

## 1. 项目简介
基于 AI 的智能 GitHub Pull Request 代码审查与 Issue 分析机器人，具备主动探索代码库的能力

## 2. 仓库信息
- 仓库名: Sakura520222/Sakura-AI
- 语言统计: Python: 8446905, HTML: 1111943, Shell: 357518, Dockerfile: 3135
- 累计反思 36 次

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

## 7. 最新反思要点（累计36次）
- ISSUE581-585（Gitflow同步冲突连发）：自动同步main→develop遇.sakura/并行编辑必冲突；同步工作流应先git merge --no-commit --no-ff预检，冲突即fail-fast建draft PR并附git diff --name-only文件清单，而非直接失败
- 冲突处理：解决前备份（临时分支/tar）防丢反思记录；受保护分支走PR流程不能直推；合并后须完整验证（ruff+pytest+构建）而非仅风格检查
- 重复检测：仅标题关键词易误报/漏报；须比对冲突文件清单+提交SHA+分支方向二次过滤，相似≠重复，用related标注并互链
- 分类与标签：Gitflow冲突禁用笼统other，细化workflow/merge-conflict/ci-failure；统一标签命名（automation优先于automated）避免碎片化，可维护LABELS.md
- Issue评估：工作量留最坏区间（10-45min）并列风险点（冲突文件类型、分支保护）；标题已清晰则不改写，避免无谓变更
- 历史要点：PR579 滑动会话续期中间件化+JWT续期前刷新DB claims+MFA绕过与登录死循环治理；PR578 Gitflow分支前缀强制+禁止方法内递归self._request+合约变更端到端故障注入；PR576-577 增量阈值（≥30提交或>500行切full）与uv.lock同PR同步；PR572 py_compile全库防线；ISSUE570 三镜像原子更新

## 8. 技术栈
FastAPI (Python 3.14+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · 多协议AI（OpenAI/Anthropic/Gemini） · MySQL 8.0 + Redis + ChromaDB · GitHub App + OAuth · Docker Compose

*最后更新：基于 ISSUE #581-#585 Gitflow main→develop 自动同步冲突反思（冲突预检与draft PR/备份防丢/重复检测二次过滤/分类标签细化），累计反思 36 次*
