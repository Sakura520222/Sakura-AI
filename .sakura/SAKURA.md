# Sakura AI Reviewer 概述

## 1. 项目简介
基于 AI 的智能 GitHub Pull Request 代码审查与 Issue 分析机器人，具备主动探索代码库的能力

## 2. 仓库信息
- 仓库名: Sakura520222/Sakura-AI
- 语言统计: Python: 8277389, HTML: 1112221, Shell: 324499, Dockerfile: 3061
- 累计反思 15 次

## 3. 核心审查原则
- 完整性验证：PR描述/提交/文件/diff一致核对；功能逐项勾选，差异>10%标minor
- 双轨评分：增量+整体；历史major未清零上限8；quick上限8
- 批准规范：approve须声明未解决问题；request_changes须对应major/blocking
- 高分审视：10/10≠无风险，须主动找安全/测试遗漏，警惕确认偏误
- 无评论须附验证依据：rg命令/CI链接等审查痕迹
- 增量审查须结案历史项并触发全库引用搜索；增量≠全局，环境变量/全局副作用须跨文件审查
- 审查报告结构化：摘要→关键风险→修复建议，阻断项须在摘要阶段可见，勿留到正文补充

## 4. 硬规则（重点）
- CI失败标error，合并前须全绿run链接；workflow最小permissions；CI须全库py_compile/import-all，语法阻断设merge-gate
- async路由同步I/O用asyncio.to_thread()
- 配置增删改须全库rg验证消费点并列命令与结果；env注入统一配置模块，禁止业务代码setdefault
- 守护进程/补偿扫描幂等：唯一约束/Redis锁防并发重复触发
- 镜像版本绑定：固定tag或digest并校验；start.sh等脚本须fail-closed标签一致性断言
- loguru统一logger.exception()；except块标注#USER_INPUT_ERROR/#INTERNAL_ERROR并统一sanitize
- 全链路影响分析：schema修改检索所有读取点；迁移脚本幂等可回滚
- 禁止except Exception:；捕获具体异常；多异常统一except (A,B) as e:，禁止逗号式
- Shell一等公民：fail-closed、ERR_前缀统一错误码、日志脱敏、幂等与回退路径
- 跨平台抽象/环形依赖检测/安全改动强制major或error；POSIX-only API须skipif或运行时特性检测
- 三镜像部署原子更新：capability协商、版本漂移检测、manifest一致性、幂等回滚
- 模块级全局导入有单点故障风险，关键依赖改延迟导入或容错包装；常量集中constants.py

## 5. Agent/Worker/Webhook
- Shell白名单≠安全：检查$()反引号;&&|&；输出双层限额
- worker禁止无日志无退避的except:continue
- 统一取消机制：复用_cancel_events，取消幂等
- 状态枚举后端/前端/i18n同步；errors.py映射HTTP码与翻译条目

## 6. 知识库维护
- .sakura/增量追加或精确修改，禁止覆盖式重写
- 单一真值源：URL/版本常量集中；监控关键状态
- 配置集中backend/core/config.py，新配置env+文件双入口并CI校验

## 7. 依赖管理规范（PR573-577沉淀）
- 锁文件同步是必检项：改pyproject.toml/requirements.txt须同步uv.lock；CI加uv lock --check，发布链路统一uv sync --locked，防"CI过、发布败"
- 双清单镜像：pyproject↔requirements逐行一致，CI脚本自动校验，不一致标error
- 未使用依赖检测：升级前全库import搜索，0命中标suggestion（移除或注明保留意图并写文档）
- 无导入点≠可删除：搜索须扩展至CI脚本、Dockerfile、alembic.ini、migrations/等非代码消费点
- 升级须附官方Release Notes与Breaking Changes；有调用点须兼容性测试；安全类升级提升major/error
- Python 3.14 wheel可用性提前验证（cp314）；超大锁文件用分块/摘要读取

## 8. 最新反思要点（累计15次）
- PR572 incr2：9处Python2逗号式except致全包不可导入仍残留；阻断缺陷先于功能审查，request_changes须跟踪修复闭环；POSIX-only测试补skipif；文档审查列必检
- PR573：alembic死依赖——非代码消费点检查、保留意图文档化、CI实装install+测试验证
- PR574：pyyaml升级补CI run链接证据；docs内旧版本约束须同步检查
- PR576：watchfiles单点消费封装良好；锁文件问题须前置到审查摘要，勿留主要评论
- PR577：python-slugify零调用冗余依赖；锁文件漂移风险分级应更保守
- 历史要点：ISSUE571 通知链路清理全库审计+灰度；ISSUE570 三镜像原子更新；PR567 incr4 全局env全库引用审查；ISSUE569 高风险结构化清单；ISSUE568 幂等+分页退避；PR539 外部CLI契约测试

## 9. 技术栈
FastAPI (Python 3.14+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · 多协议AI（OpenAI/Anthropic/Gemini） · MySQL 8.0 + Redis + ChromaDB · GitHub App + OAuth · Docker Compose

*最后更新：基于 PR #572-#577 反思，累计反思 15 次*
