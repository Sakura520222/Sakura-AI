# Sakura AI Reviewer 概述

## 1. 项目简介
基于 AI 的智能 GitHub Pull Request 代码审查与 Issue 分析机器人，具备主动探索代码库的能力

## 2. 仓库信息
- 仓库名: Sakura520222/Sakura-AI
- 语言统计: Python: 8277389, HTML: 1112221, Shell: 324499, Dockerfile: 3061
- 累计反思 22 次

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
- 配置增删改须全库rg验证消费点并列命令与结果；env注入统一配置模块，禁止业务代码setdefault
- 守护进程/补偿扫描幂等：唯一约束/Redis锁防并发重复触发
- 镜像版本绑定：固定tag或digest并校验；start.sh等脚本须fail-closed标签一致性断言
- loguru统一logger.exception()；except块标注#USER_INPUT_ERROR/#INTERNAL_ERROR并统一sanitize
- 全链路影响分析：schema修改检索所有读取点；迁移脚本幂等可回滚
- 禁止except Exception:；捕获具体异常；多异常捕获≤3类且as e
- Shell一等公民：fail-closed、ERR_前缀统一错误码、日志脱敏、幂等与回退路径
- 跨平台抽象/环形依赖检测/安全改动强制major或error；性能解析改动须微基准
- 三镜像部署原子更新：capability协商、版本漂移检测、manifest一致性、幂等回滚
- 依赖管理：pyproject/requirements逐行镜像+uv.lock同PR强制同步；升级须附Release Notes、全库消费点核对与回归验证
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

## 7. 最新反思要点（累计22次）
- PR577(incr2/3)：增量阈值——PR≥30提交或>500行自动切full策略；未结案阻断项(锁文件)每轮复查，未标记≠已解决；审查摘要须前置关键阻断风险；merge commit噪声多须rebase/squash
- PR577：锁文件漂移升error级；零调用依赖删除前须查Dockerfile/CI/scripts非代码消费点(python-slugify手写slug)；CI与发布链路统一uv sync --locked消除双轨解析
- PR576(incr2)：升级前核对Release Notes与Supported Python版本(watchfiles弃3.9)；升级后默认行为变化加snapshot回归测试；大文件(uv.lock超限)分块读取防漏检
- PR576：依赖声明改动须同PR更新锁文件，CI强制uv lock --check；单点消费API兼容逐一核对
- PR575：pydantic等核心库升版须跑完整回归测试；sandboxer/updater子项目版本下限与根项目一致防漂移；约束建议加上限
- PR574：面向Python 3.14升级须验证cp314 wheel可用性；审查结论须附CI run链接作证据；文档硬编码版本号同步检查
- PR573：零导入点≠可删(alembic)，须检索CI/脚本/镜像等非代码消费点；升级关注默认行为变化并附行为对比
- 历史要点：PR572 Python2语法全库编译防线；ISSUE571 通知链路清理审计与灰度；PR567 全局env全库引用审查；ISSUE570 三镜像原子更新

## 8. 技术栈
FastAPI (Python 3.14+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · 多协议AI（OpenAI/Anthropic/Gemini） · MySQL 8.0 + Redis + ChromaDB · GitHub App + OAuth · Docker Compose

*最后更新：基于 PR #576-#577 依赖与 updater 系列反思（锁文件同步/零调用依赖/增量审查阈值/systemd 幂等与fallback），累计反思 22 次*
