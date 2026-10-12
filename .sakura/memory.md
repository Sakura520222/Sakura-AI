# 项目记忆

累计反思 33 次

## 核心审查原则

- **"无评论"≠"无问题"**：空结果可能源于工具故障；结论须附验证依据（搜索命令、CI 链接）
- **高分警惕确认偏误**：高分仍须负向用例验证；已有 major 未决项时，增量干净亦须保持阻断
- **审查策略动态升级**：≥30 commits / >500 行、或增量 ≥3 轮 / 累计 >1000 行切 full；改动依赖声明即便 quick 亦核验锁文件与子包一致性
- **Fail-Closed**：宁可误报不可漏报；文档不可作验证依据；数据源/配置扰动超限时严格实施 Fail-Closed
- **报告结构化**：摘要→关键风险→建议；阻断项在摘要阶段立即可见

## 依赖、CI 与工作流安全

- **依赖三要素与锁文件阻断**：改动 `pyproject.toml`/`requirements.txt` 须同 PR 纳入 `uv.lock` 并对齐子项目（`sandboxer`/`updater`）依赖下限；CI 必跑 `uv sync --locked`。依赖升级第 1 顺位：核查 lockfile 是否在 diff 且 CI `--locked` 是否通过
- **精准锁升级与 Bot 审查防线**：升级优先 `uv lock --upgrade-package <pkg>` 防无关依赖漂移；Dependabot PR 默认易漏 `uv.lock` 致冒烟测试 exit 1，切忌因 patch/纯数据包麻痹 Approve；指引须兼顾本地提交与 Bot/工作流机制
- **Dependabot 追锁与降级静默排查**：自动补锁须用 `pull_request_target` 校验 `user.login == 'dependabot[bot]'` 与最小权限；警惕补锁流水线（如 `uv-lock-sync.yml`）因缺 PAT/Secret 转入“仅告警跳过”引发降级静默失效

## 跨平台抽象与文件系统安全

- **Windows 路径安全准入与规范化**：涉及 `GetFinalPathNameByHandleW` 或设备级规范化校验时，基准根路径（Root/Workspace）必须统一显式 `root.resolve()`；警惕 8.3 短路径（如 `RUNNER~1`）、Junction 挂载点与驱动器映射导致前缀对比静默失效（`ELOOP` 拒绝导致静默丢结果）
- **白名单例外收敛与跨平台锁生命周期**：放开底层校验（如 `allow_cache_redirect`）须收敛在单一受控入口（如锁锚点）且保持严格硬链/reparse point 校验；平台外置资源（如 LocalAppData 下 `.lock` 进程锁、管道）须设生命周期清理防线
- **多模块子套件 Fixture 隔离**：拥有独立 `pyproject.toml` 的子包（`updater/`, `sandboxer/`）严禁隐式依赖父级根目录 `conftest.py`，必须在子包内自包含测试 fixture，防 pytest 根目录割裂引发收集失败

## 架构隔离与业务设计

- **核心配置层隔离 (Core Config Isolation)**：`backend/core/config.py` 禁止顶层静态导入重型依赖或可选 SDK；动态选项须函数惰性加载，防导入副作用致系统瘫痪
- **端点多语言与准入零信任**：同一 API 端点内所有异常分支统一用户语言；计费准入上下文由服务端强行覆盖，禁止信任客户端付款主体字段
- **配置清理 5 要素**：改动配置同步 `config.py`、`DYNAMIC_CONFIG_GROUPS`、WebUI 模板、翻译字典及 `.deploy/*.env.example`
- **UI Clamp Removal 防线**：移除截断须核验容器弹性高度（`min-h-*`）、全域一致性与 `whitespace-pre-wrap`

## 异步并发与 Agent Harness

- **异步取消排空观测性**：`asyncio.shield` 循环排空任务时，若底层异常被捕获，保留诊断日志后再 break 并重抛 `CancelledError`，严禁吞没异常
- **状态恢复与废弃切断一致性**：历史检查点恢复反序列化须与前置 Validator 同级防御（统一 `.get()`）；废弃旧持久化/工具时，防范历史未决任务硬访问引发空指针
- **并发事务与所有权锁**：跨多段裁决管线设事务级防重/乐观锁；引用计数递增须在 acquire 前完成，资源清理严格为 `users == 0`

## 语法、解析器与 AI 协议治理

- **已确认误报：无括号多异常捕获**：运行时为 Python 3.14+；PEP 758 下 `except TypeError, ValueError:` 是合法语法，严禁误判阻断
- **多段 AI 流水线解析与容错收敛**：严禁裸调 `json.loads`，统一使用带 Markdown 剥离与 recovery 引导的解析函数；多角色管线防跨阶段 Schema 降级与解析异常；协议适配层严格区分 `0` 与 `None` 边界
- **AI 故障兜底与类型防线**：模型调用全盘失败时显式提示“治理服务暂时不可用”，严禁静默生成空报告；拒绝 `bool` 伪装 `int`（用 `type(x) is not int`）；行号参数物理钳制

## 增量审查与协作规范

- **增量+全局双模**：增量审查须与全局架构意图 cross-check，防局部改动破坏全局降级契约；重构/破坏性变更规范打标
- **配置校验优先于早退 (Guard Before Early-Exit)**：配置合法性与安全边界检测置于任何逻辑早退之前
