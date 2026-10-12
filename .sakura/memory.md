# 项目记忆

累计反思 38 次

## 核心审查原则

- **"无评论"≠"无问题"**：空结果可能源于工具故障；结论须附验证依据（搜索命令、CI 链接）
- **高分警惕确认偏误**：高分仍须负向用例验证；已有 major 未决项时，增量干净亦须保持阻断
- **审查策略动态升级**：≥30 commits / >500 行、或增量 ≥3 轮 / 累计 >1000 行切 full；改动依赖声明核验锁文件与子包一致性
- **Fail-Closed**：宁可误报不可漏报；文档不可作验证依据；数据源/配置扰动超限严格 Fail-Closed
- **报告结构化**：摘要→关键风险→建议；阻断项在摘要阶段立即可见

## 依赖、CI 与工作流安全

- **依赖三要素与锁文件阻断**：改动依赖声明须同 PR 纳入 `uv.lock` 并对齐子项目依赖下限；CI 必跑 `uv sync --locked`
- **精准升级与 Dependabot 审查**：优先 `uv lock --upgrade-package <pkg>`；Dependabot 易漏 `uv.lock` 致冒烟失败，切忌因 patch 麻痹 Approve
- **追锁流水线排查**：补锁须校验 `dependabot[bot]` 与最小权限；警惕缺 Secret 降级静默失效

## 跨平台抽象与文件系统安全

- **Windows 路径准入**：`GetFinalPathNameByHandleW` 基准根路径必须显式 `root.resolve()`；警惕 8.3 短路径、Junction 致对比失效
- **`os.path.normcase` 跨平台陷阱**：`normcase` 仅在 Windows 下小写化，POSIX 恒等；黑名单过滤切忌假定其通杀大小写不敏感系统，须评估 macOS APFS 别名绕过
- **凭据文件交接**：引导凭据交接须保证原子写（`mkstemp` + `replace`）、最小权限（0600/ACL）、启动轮换与消费即焚
- **子套件 Fixture 隔离**：独立 `pyproject.toml` 子包（`updater/`, `sandboxer/`）严禁依赖父级 `conftest.py`，必须自包含 fixture

## 架构隔离与配置中心演进

- **不可变事实源**：`backend/core/config.py` 禁静态导入重依赖；恢复默认值严禁依赖被污染单例，必从 `model_fields[key].default` 取源
- **字段级 PATCH 与校验**：拆解统一保存为字段 PATCH 时，严格配置校验优先于早退（Guard Before Early-Exit），防隐式布尔覆写与并发写竞争
- **配置清理 5 要素**：改动配置同步 `config.py`、`DYNAMIC_CONFIG_GROUPS`、WebUI 模板、翻译字典及 `.deploy/*.env.example`
- **UI Clamp Removal**：移除截断须核验容器弹性高度（`min-h-*`）、全域一致性与 `whitespace-pre-wrap`

## 异步并发、热路径与日志管线

- **日志拦截热路径防线**：全局脱敏（Loguru patcher）处高频路径，严禁单条日志全量扫 Settings；须按进程缓存或在 Record 生命周期内做 Fail-Closed 隔离缓存
- **脱敏正则 ReDoS 防线**：热路径正则含 `re.DOTALL`、未锚定 `.*?` 及反向引用闭包时，须核验大文本最坏耗时并设长度钳制
- **异步排空与事务锁**：`asyncio.shield` 排空任务捕获异常须保留日志再重抛 `CancelledError`，禁吞没；跨多段管线设防重/乐观锁

## 语法、模板与 AI 协议治理

- **已确认误报：PEP 758**：运行时 Python 3.14+；`except TypeError, ValueError:` 是合法语法，禁误判阻断
- **模板清洗与兜底追加原子性**：动态剔除空字段且兜底追加时，严格校验完整性标识，防半成品脏中间态逃逸；异常降级须清空累加变量
- **AI 解析与协议兜底**：严禁裸调 `json.loads`，统一 Markdown 剥离与 recovery；模型调用失败显式提示“服务不可用”，禁静默空报告；区分 `0` 与 `None`，拒 `bool` 伪装 `int`
