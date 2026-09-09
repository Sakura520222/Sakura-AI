# Sakura AI Reviewer 概述

## 1. 项目简介
基于 AI 的智能 GitHub Pull Request 代码审查与 Issue 分析机器人，具备主动探索代码库的能力

## 2. 仓库信息
- 仓库名: Sakura520222/Sakura-AI
- 语言统计: Python: 6471412, HTML: 1078294, Shell: 175475, Dockerfile: 3619
- 累计反思 5 次

## 3. 核心审查原则
- **完整性验证**：PR描述/提交/文件清单/diff一致性核对；功能点逐项勾选，差异>10%标minor
- **双轨评分**：增量审查双轨评分(增量+整体)；历史major未清零上限8；quick上限8
- **批准规范**：approve须声明未解决问题；request_changes须对应major/blocking
- **高评分审视**：10/10≠无风险，高分须主动寻找安全/测试遗漏，警惕确认偏误
- **无评论须附验证依据**：任何"无问题"结论须附rg搜索命令/CI run链接等审查痕迹
- **增量审查**：必须结案历史遗留项，增量后触发全库引用搜索补盲点

## 4. 硬规则（重点）
- **全绿Gate提供链接**：任意CI失败标记error，合并前须提供全绿run链接；workflow须声明最小permissions
- **async路由同步I/O须asyncio.to_thread()**
- **配置清理验证**：删除/重命名配置键、异常类、缓存结构须全仓库rg验证消费点并列出命令与结果
- **循环约束验证**：移除显式循环计数器须验证等效退出条件
- **守护进程幂等性**：Daemon/IPC/补偿扫描须验证重复调用幂等（唯一约束/Redis锁防并发重复触发）
- **镜像版本绑定**：docker pull须显式固定标签或digest(@sha256:)并校验
- **loguru强制写法**：禁止exc_info=True，统一logger.exception()
- **安全脱敏分级**：except块对外响应标注#USER_INPUT_ERROR/#INTERNAL_ERROR，统一sanitize_error调用
- **文档数值验证**：资源计算须提供公式依据；文档声明约束须有代码实现或校验
- **全链路影响分析**：修改核心数据结构/schema须检索所有读取点；迁移脚本须幂等可回滚
- **异常捕获粒度**：禁止except Exception:，须捕获具体异常并加错误标签注释
- **Docker参数统一化**：禁止硬编码--mount/--network等，须通过统一生成函数
- **跨模块引用安全**：sandboxer等子服务公共API须执行全库引用搜索
- **测试-实现耦合**：测试检查实现细节须抽象为行为契约；新增错误分支须fail-closed负向路径测试
- **POSIX Mock对齐**：os.fchown等低层API mock须与生产实现一致
- **跨平台抽象**：平台判定封装platform_utils.py；skipif(nt)须提供Linux-only运行报告
- **环形依赖检测**：backend/services/**出现>2层循环须提供拆分方案
- **安全改动风险分级**：ReDoS防护/Token有效期漂移等安全类改动强制major/error，禁止标minor
- **性能基准**：解析类改动须微基准测试，CI检查耗时阈值防隐藏回退
- **双写一致性**：Redis+内存双写须Lua/pipeline原子清理，防并发竞态残留
- **跨语言安全链审计**：UDS文件权限、容器用户映射、--cap-drop、挂载隔离须逐项检查
- **CI打包校验**：Release产物须校验包装清单与实际文件一致（如sandboxer存在性）
- **Issue标题规范**：强制模板；重复检测须加语义相似度防漏报，孤立标签须登记标签字典
- **外部CLI版本声明**：workflow调用外部二进制须声明最低版本并在CI真实运行验证

## 5. Agent/Worker/Webhook
- Shell白名单!=安全：检查$()反引号;&&|&；输出双层限额
- worker/长连接禁止except:continue无日志无退避
- 统一取消机制：复用_cancel_events模式，Issue/Agent取消需幂等
- 状态枚举后端/前端/i18n同步；errors.py须映射HTTP状态码与翻译条目
- Shell错误码对齐：exit/return须返回0-255明确错误码，错误输出用ERR_前缀结构化

## 6. 知识库维护
- .sakura/更新须增量追加或精确修改，禁止覆盖式重写
- 单一真值源：URL/版本常量集中引用；监控上报关键状态（如上次成功扫描时间）
- 配置集中backend/core/config.py，新配置须env+文件双入口并CI校验默认值

## 7. 最新反思要点（累计5次）
- **PR567全量**：UDS权限/容器用户等跨语言安全链遗漏；workflow权限最小化；迁移脚本幂等；公告状态唯一索引；网络策略互斥校验；全量重审须列新发现与增量对照闭环
- **PR567 incr2**：安全改动强制major分级；expires_at显式字段消除Token配置漂移；前向扫描替代正则防ReDoS；增量审查后须全库搜索；双写原子清理；CI打包清单校验；性能基准
- **PR539**：外部CLI版本声明、负向路径contract测试、全库消费点搜索、错误信息结构化、CI环境兼容性声明
- **ISSUE569**：10/10误批准源于确认偏误，高风险场景（部署链/双写/回退/并发/状态机）提示词结构化清单；反证pass限高风险文件控成本；依赖图增量缓存防大PR卡顿；基于历史PR建回归测试套件
- **ISSUE568**：补偿扫描核心是幂等（DB唯一约束/分布式锁）；分页+指数退避防API限速；停摆须告警；依赖注入入口幂等性须先验证

## 8. 技术栈
FastAPI (Python 3.14+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · 多协议AI（OpenAI/Anthropic/Gemini） · MySQL 8.0 + Redis + ChromaDB · GitHub App + OAuth · Docker Compose

*最后更新：基于 PR #567(全量+incr2)/#539 与 ISSUE #569/568 反思，累计反思 5 次*
