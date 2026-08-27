# Sakura AI Reviewer 概述

## 1. 项目简介
基于 AI 的智能 GitHub Pull Request 代码审查与 Issue 分析机器人，具备主动探索代码库的能力

## 2. 仓库信息
- 仓库名: Sakura520222/Sakura-AI
- 语言统计: Python: 6468203, HTML: 1078294, Shell: 175475, Dockerfile: 3619
- 累计反思 15 次

## 3. 核心审查原则
- **完整性验证**：PR描述/提交/文件清单/diff一致性核对；功能点逐项勾选，差异>10%标minor
- **双轨评分**：增量审查双轨评分(增量+整体)；历史major未清零上限8；quick上限8
- **批准规范**：approve须声明未解决问题；request_changes须对应major/blocking
- **高评分审视**：即使9/10也须主动寻找边缘安全/测试遗漏，警惕确认偏误
- **增量审查**：必须结案历史遗留项，关联抽查变更区域上下文连贯性

## 4. 硬规则（重点）
- **async路由同步I/O须asyncio.to_thread()**
- **配置清理验证**：删除配置键须全仓库grep/rg验证消费点
- **循环约束验证**：移除显式循环计数器须验证等效退出条件
- **守护进程幂等性**：Daemon/IPC调用须验证重复调用是否导致状态紊乱
- **镜像版本绑定**：使用特定版本参数须检查Docker镜像标签显式固定
- **loguru强制写法**：禁止exc_info=True，统一用logger.exception()
- **安全脱敏分级**：except块对外响应必须标注错误类别
- **文档数值验证**：涉及资源计算须强制提供计算公式及依据
- **全链路影响分析**：修改核心数据结构时须检索所有读取点
- **CI红灯阻断**：任意GitHub Actions失败须标记为error，PR合并前须提供全绿run链接
- **异常捕获粒度**：禁止except Exception:，须捕获具体异常并加错误标签注释(#USER_INPUT_ERROR/#INTERNAL_ERROR)
- **文档-代码同步**：文档声明约束须有代码实现或校验，缺失标记为error
- **Docker参数统一化**：禁止硬编码--mount/--network等参数，须通过统一生成函数
- **跨模块引用安全**：sandboxer等子服务公共API须执行全库引用搜索
- **CI Gate关联强制**：审查报告须提供所有阻断Gate的run链接
- **测试-实现耦合**：测试直接检查实现细节须抽象为行为契约
- **POSIX Mock对齐**：os.fchown/fchmod等低层API mock须与生产实现一致
- **跨平台抽象**：平台相关判定封装为platform_utils.py，禁止硬编码
- **环形依赖检测**：backend/services/**出现>2层循环须提供拆分方案
- **全局脱敏规则**：日志/异常脱敏须统一调用且覆盖CI检查
- **跨平台测试完整**：skipif(os.name=="nt")须提供Linux-only运行报告
- **安全异常分类强制**：禁止裸except Exception，须捕获DockerAdapterError/SandboxError并标注#INTERNAL_ERROR
- **Issue标题规范**：强制使用模板，避免"怎么回事"等无信息标题

## 5. Agent/Worker/Webhook
- Shell白名单!=安全须检查$()反引号;&&|&等；输出须双层限额
- 长连接/worker禁止except:continue无日志无退避
- 依赖文件缓存统一：cache-dependency-path须指向项目依赖文件
- Shell错误码对齐：exit/return须返回0-255明确错误码
- 新增服务层异常映射：errors.py须对应HTTP状态码与翻译条目
- 统一取消机制：复用ReviewWorker的_cancel_events模式，Issue/Agent取消需幂等
- 状态枚举同步：新增枚举须在后端/前端/i18n同步更新

## 6. 知识库维护
- .sakura/更新须增量追加或精确修改，禁止覆盖式重写
- 元数据修改须提供计算依据；核心文件修改视为与代码变更同等重要
- 单一真值源：URL/版本等常量须从中心位置引用，避免硬编码

## 7. 最新反思要点（累计15次）
- **PR533(incr4)**：Docker参数统一化、sandboxer服务边界、CI Gate关联、闭环验证、全库引用搜索、测试-实现解耦、CI环境差异、文档-代码同步
- **PR533(incr3)**：全局脱敏规则、跨平台测试完整、环形依赖检测、CI关键工作流阻断、安全异常分类、增量审查全库搜索
- **PR533(incr2)**：POSIX mock对齐、配置键消费追踪、异常捕获粒度、跨平台抽象、大PR拆分、文档-代码双向验证、环境变量泄漏
- **ISSUE537**：工具参数描述显式化、校验返回友好、单元测试覆盖交叉场景、Issue标题模板、重复检测人工确认、工具契约自愈特性
- **ISSUE536**：统一取消机制、状态枚举同步、幂等与并发、资源浪费监控、测试覆盖、文档同步、错误处理
- **PR533(初评)**：依赖缓存统一、Shell错误码对齐、服务异常映射、大PR全链路审查、日志监控标签化
- **ISSUE534**：模糊需求具象化、单一真值源、i18n翻译同步、重复检测基于内容相似度
- **PR530(incr2)**：能力声明一致性、元数据完整性、回退路径覆盖、压缩器同步、单元测试规范
- **PR530(初评)**：配置变更审查、异常捕获粒度、回归行为验证

## 8. 技术栈
FastAPI (Python 3.14+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · 多协议AI（OpenAI/Anthropic/Gemini） · MySQL 8.0 + Redis + ChromaDB · GitHub App + OAuth · Docker Compose

*最后更新：基于 PR #533 & ISSUE #536/537 反思，累计反思 15 次*