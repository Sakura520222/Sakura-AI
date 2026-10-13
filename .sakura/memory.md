# 项目记忆

累计反思 43 次

## 核心审查原则

- **"无评论"≠"无问题"**：空结果可能源于工具故障；结论须附验证依据（搜索命令、CI 链接）；推演结论（如正则复杂度）须配病态输入基准测试作回归锚点
- **高分警惕确认偏误**：高分仍须负向用例验证；已有 major 未决项时，增量干净亦须保持阻断；CI 失败定 Major 须追到行号级证据
- **阻断措辞**："证据待补型阻断"与"缺陷型阻断"须显式区分，防评分被误读为质量恶化
- **审查策略动态升级**：≥30 commits / >500 行、增量 ≥3 轮 / 累计 >1000 行切 full；依赖声明核验锁文件与子包一致性
- **噪声过滤**：排除 chore/merge/reflection 提交，报告显式声明有效变更集与 diff 基线（反向合并 ≥3 次须核验 `main...HEAD` 三点 diff）
- **重复建议升级**：同一建议跨 ≥2 轮未处理，转 tracked issue 或要求作者显式回应，禁车轱辘重述
- **Fail-Closed**：宁可误报不可漏报；文档不可作验证依据；配置扰动超限严格 Fail-Closed
- **报告结构化**：摘要→关键风险→建议；阻断项摘要可见；遗留项摘要逐条列出

## 工作流与 AI 自产物安全

- **新增 workflow 审查**：凡新增 `.github/workflows/*.yml`，须核验 `permissions` 最小权限、Secret 缺失是否静默降级、矩阵 fail-fast，首次绿色运行为合并前置
- **AI 记忆脱敏红线**：`.sakura/` 下反思/记忆文件提交前，须扫描引用的日志片段、配置示例、diff 摘要是否含明文凭据——审查覆盖必须包含 AI 自身产物

## 依赖、CI 与外部输入防线

- **依赖三要素**：依赖声明须同 PR 纳入 `uv.lock` 并对齐子项目下限；CI 必跑 `uv sync --locked`；Dependabot 易漏 `uv.lock`，勿因 patch 麻痹 Approve
- **外部响应值硬钳制**："不可信输入"含外部服务 Response（如 `Retry-After`）；用于调度/重试间隔须 `min(val, 上限)`，默认 ≤1h
- **容量与生命周期**：订阅规则/端点创建 API 强制配额上限，全量载入加 LIMIT 熔断；新增 Outbox/Deliveries 表须补 Retention/Purge 清单
- **凭据文件交接**：原子写（`mkstemp`+`replace`）、最小权限（0600/ACL）、启动轮换、消费即焚

## 架构隔离与配置中心

- **不可变事实源**：恢复默认值必从 `model_fields[key].default` 取源，禁依赖被污染单例；字段级 PATCH 时严格校验优先于早退
- **配置清理 5 要素**：同步 `config.py`、`DYNAMIC_CONFIG_GROUPS`、WebUI 模板、翻译字典、`.deploy/*.env.example`
- **跨平台路径**：`normcase` 仅 Windows 小写化，黑名单过滤须评估 macOS 大小写绕过；子包 fixture 严禁依赖父级 `conftest.py`

## 异步、日志管线与 Provider 契约

- **日志热路径防线**：全局 patcher 严禁单条日志全量扫 Settings；按 Record 生命周期或进程级缓存；热路径正则含 DOTALL/未锚定 `.*?`/反向引用须验最坏耗时并设长度钳制
- **Record 缓存隐含约束**：挂在 record 上的缓存依赖"同步 sink + record 不池化"，引入异步 sink 前须显式声明，否则跨记录泄漏密钥
- **Provider 异常全翻译**：异步 Provider 层禁穿透 `TimeoutError`/`aiohttp.ClientError` 等原生异常，统一映射 Transient/Permanent 错误
- **竞态断言规范**：禁断言事件顺序等 incidental 产物，须断言计数、幂等、终态一致性；ORM 播种经事件管线会派生事件流——先问"播种会发出什么"，单测与 live-DB fixture 语义须对齐

## 语法、模板与 AI 协议治理

- **已确认误报：PEP 758**：`except TypeError, ValueError:` 在 3.14+ 合法，禁误判阻断
- **模板清洗原子性**：剔除空字段并兜底追加时严格校验完整性标识；异常降级须清空累加变量
- **AI 解析兜底**：禁裸调 `json.loads`；区分 `0` 与 `None`，拒 `bool` 伪装 `int`
