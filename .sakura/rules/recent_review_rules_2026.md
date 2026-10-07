# 最新审查硬规则 (2026)

## 一、Dependabot 与自动化 PR 锁文件防御规则

### 1.1 Dependabot PR 的锁文件完整性检查
1. **uv.lock 缺失断言**：针对 Dependabot 或外部机器人发起的依赖升级 PR，优先校验 `uv.lock` 是否在变更列表中。若仅更改 `pyproject.toml` 或 `requirements.txt` 而未更新 `uv.lock`，因 CI 启用了 `uv sync --locked` 校验，必须直接阻断（Request Changes）。
2. **修复指导可操作性**：对于锁文件未同步但依赖版本更新无 Breaking Changes 的 Bot PR，审查意见中应提供确切的操作指令（如“在 PR 中触发自动化 action 或本地运行 `uv lock`”），避免盲目全盘否定。
3. **元组与 Specifier 精确匹配**：`uv sync --locked` 对 `[package.metadata] requires-dist` 执行严格精确匹配。即使锁定的版本号已在目标区间，specifier 不一致仍会导致 CI 构建报红。

---

## 二、前端模板与 UI 展示防线规则

### 2.1 UI 文本渲染与截断处理
1. **截断故障双重验证范式**：遇到“内容显示不全/截断”问题，须建立“数据生成/数据库存储 -> 前端 CSS/模板渲染”双重排查路径。必须前置验证 DB 字段类型及后端服务传参，排除后端被截断后，锁定前端 `line-clamp-*` 类与样式限制。
2. **渲染安全与纯函数提取**：UI 文本格式化必须提取为单一纯函数；更新 DOM 节点统一绑定 `textContent`，涉及动态数据展示严禁使用 `innerHTML`。
3. **正则校验与防截断异常**：对 Commit Hash (如 `^[0-9a-f]{40}$`)、UUID 等特征字符串做截取之前，必须先经过正则匹配；匹配失败时须实施平滑降级（退回原字符串或纯版本号），严禁产生 `undefined` 或空渲染。

---

## 三、日志收敛与第三方库日志桥接规则

### 3.1 日志噪音 (Log Noise) 拦截规则
1. **第三方库心跳日志收敛**：第三方库（如 APScheduler、httpx、sqlalchemy）的默认 INFO 心跳日志必须接入系统的 `logging_bridge` 前缀过滤列表 (`_NOISY_LOGGER_PREFIXES`)，保留 `WARNING` 和 `ERROR` 级别，防止日志污染运维视线。
2. **开关语义一致性**：配置项（如 `*_enabled`）若在 UI 描述为“禁用”，但底层架构保持心跳 Timer 运行以监测多副本 DB 变动，必须同步优化 UI/文档文案并屏蔽过程日志，确保配置语义与运维感知一致。
3. **日志过滤测试要求**：针对 `logging_bridge` 前缀过滤变更，须同步补全正向测试（WARNING/ERROR 正常透传）与负向测试（INFO 心跳被拦截），防止告警丢失。
