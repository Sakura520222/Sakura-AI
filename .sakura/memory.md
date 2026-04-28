# 项目记忆

## 核心审查教训与模式
- 双重处理陷阱：同一格式化函数在调用链重复执行，必须扫描数据全路径的同名调用，标critical
- 服务层职责模糊：worker预格式化，破坏comment_service唯一渲染入口；修复建议禁用上游预渲染
- 历史问题闭环验证：声称修复须列出所有调用位置、冲突消除证据，缺失触发missing-statement
- 增量审查陷阱：隧道视野致配置假开关、数据流断裂；新字段须追踪全生命周期，缺环节标major
- 日志语义与行为一致性：行为变日志必同步，missing标minor+；关键路径AI/外部输入修正须warning，debug属无效可观测
- LLM输出解析：正则须容错空白、标点变体；清洗规则须注释上游模式；脆弱累积可升级技术债
- 语义转换重构：去重→合并等，日志/变量名/注释同步为高优先级
- 日志静默传染：发现一处错误吞没，须扫描同文件同类场景
- 增量审查回归评估：须回答“是否新增未处理异常路径？”，未答评分≤8
- PR描述与diff一致性校验：类型不符标blocker；范围超出要求澄清
- 防御代码隐形流失；“部分修复”比“未修复”更危险；假兜底升级
- 配置假值陷阱：统一is not None；配置默认值变更强制评估攻击面/成本
- 异步取消与超时：run_in_executor无感知取消可能任务泄漏
- API URL拼接：禁止base_url含路径段；必须确认follow_redirects；缺失标suggestion/升级
- 容器/服务就绪轮询须含超时失败退出
- 安全扫描类强制security标签；默认secret/API token经query传输为高危
- 重复检测须输出Issue标题/相似度；已关闭Issue再现打regression
- 零评论高分是陷阱信号，approve也留简评
- 修复局限性归档为技术债Issue
- 版本升级须同步pyproject.toml与CHANGELOG
- 异步懒加载缓存并发安全：无锁保护提示asyncio.Lock

## 代码高危区
- 路径安全、SSRF、容器的host网络、配置双源同步
- 双重格式化调用链：评论body预渲染后又被service格式化
- 解析器职责膨胀：result_parser集去重/合并/清洗易上帝类
- API客户端3xx非透明、缺失重定向
- 版本号多文件硬编码、身份推断下沉风险

## 严重程度判定
- 三问法强制绑定；安全类禁降级附security标签；幽灵变更标blocker
- 纯可读性封顶suggestion；假兜底无异常捕获标major
- 新功能闭环断链标major；LLM解析脆弱模式标suggestion
- 格式化重复调用标critical（非阻塞则major）

## 仓库信息
- 仓库名: Sakura520222/Sakura-AI-Reviewer
- 累计反思次数: 79