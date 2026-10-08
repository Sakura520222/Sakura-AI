# Agent Harness 2.0 运行行为

`/agent` 在配置和凭据就绪后默认无人值守。它可持续读取、搜索、修改、执行测试和创建只读子 Agent，直到明确完成、用户取消或遇到真实故障。模型轮次、累计工具调用及任务步数没有新增硬预算，不会因次数达到阈值强制总结或切换模型。重复相同操作仅触发自主策略自检，详见[仓库规则与 Skills](AGENT_REPOSITORY_CONTEXT.md)。

## 完成、并发与取消

普通 assistant 文本不是成功事件。只有运行时认可的 `finish_task` 才能正常完成；必需的 `before_finish` 检查失败时，模型收到失败证据并可继续修正。其他工具的返回值不能伪造终态。取消、不可恢复错误和无法安全继续的 blocked 状态保留各自原因；blocked 不表示等待人工授权。

同批安全读取并行执行，结果按模型调用顺序持久化。文件修改、Shell、Skill 状态变更和完成检查独占工作区；命令 Hook 和它所验证的工具处于同一个屏障中。进程内使用写入优先的读写屏障；进程间对共享工作区目录使用内核 advisory lock，读取共享、写入独占。锁等待不设任务预算，取消释放等待状态，进程退出释放锁；不在仓库内创建可替换的锁文件。

进程间锁要求各 worker 访问同一支持 `flock` 的文件系统目录。它不提供跨机器独立副本协调，也不等于任务/session 的分布式所有权租约。数据库行锁和子会话唯一约束不能被描述为完整的跨 worker 所有权保证。

POSIX 锁能力不会成为整个后端的导入依赖。Windows 等缺少 `fcntl` 或安全目录打开能力的宿主仍可导入/启动普通后端；若实际执行 Harness 工作区操作，则明确返回 `workspace_lock_unavailable`，不会默默退回仅进程内互斥。这不代表已实现 Windows 原生 Harness 工作区执行。

取消会通知并等待并行工具、命令进程和子 Agent 清理，再释放工作区屏障。崩溃恢复不会盲目重放可能已经执行的写入；工具和 Hook 的 admitted/completed 副作用账本用于识别不确定操作。已有任务恢复入口用于故障处置，正常工作不需要点击继续。

## 只读子 Agent

主 Agent 使用 `spawn_agent(task=...)` 创建独立的 durable session，通过 `wait_agent(agent_id=...)` 取回结构化结果，通过 `cancel_agent(agent_id=...)` 取消。任务和父会话身份由数据库验证，不能等待或取消另一任务的子会话。关闭父会话会取消并等待尚未完成的后代。

`agent_team_subagent_concurrency` 默认 `4`，表示同时运行的子 Agent 数；其余任务排队，完成后释放运行位置。它不是累计调用次数上限。子 Agent 使用现有 `agent_team` 模型配置，没有根据角色或累计次数切换便宜模型。

子 Agent 只能使用运行时允许的读取、目录、搜索、diff、项目识别、Web 和经部署配置标记为只读的 MCP 工具。不能写文件、运行 Shell、修改 Git、激活 Skill 或递归创建 Agent。只读范围和继承的 Skill 限制随父子映射一起持久化；直接恢复子会话也无法移除限制。已创建子 Agent 的等待、取消和清理不会因撤销新建权限而失效。

搜索和 Git 检查使用 `READ_ONLY` 执行域。Sandbox 挂载工作区/Git 元数据为只读且禁止网络；本地源码模式要求 Linux x86_64/arm64、Landlock ABI ≥ 3 和 libseccomp，通过系统边界拒绝文件修改和网络。后端不支持只读域时明确失败，不回退到可写执行。macOS 本地进程的只读执行域尚不支持；应使用具备 `read_only` 能力的 Sandbox。升级时需同时部署支持该 profile 的 sandboxd/runner，旧 daemon 不会被当作只读后端使用。

`check_changes` 还要求所选后端的 Git 支持 `--no-lazy-fetch`（上游 Git **2.45+**），以禁止检查过程中隐式获取缺失对象。旧版本在读取配置的前置检查中返回非终止 `GIT_READ_ONLY_UNSUPPORTED` 错误；不会继续执行 status/diff，也不会去掉保护选项重试。可以升级该后端 Git 或选用兼容的 Sandbox runner，其他工具仍可继续使用。该最低依赖要求同样适用于源码部署的系统 Git。

内容搜索先在选定 runner 枚举文本候选，再通过与普通读取一致的描述符边界重读并生成匹配。符号链接、硬链接和特殊文件不参与结果；返回内容/统计不采用未验证的 grep 输出。关键词是固定字符串，换行分隔的关键词作为备选；忽略大小写使用统一的 Unicode casefold。候选输出不完整时标记截断，取消会等当前读取退出，不留下继续扫描的线程。runner 失败时不会改走宿主搜索。

## 配置与边界

[插件管理](AGENT_PLUGINS.md)统一提供现有 Skills 管理入口及 MCP/Hooks 的部署配置。默认 permission profile 是 `autonomous`；常规工作没有新增逐工具批准。

| Profile | 行为 |
|---|---|
| `read_only` | 仅只读工具与被允许的外部读取；禁止写入及 Shell |
| `workspace_write` | 工作区读写和断网 Shell；禁止外部工具/发布/依赖出网 |
| `autonomous` | 普通自主开发、子 Agent、已配置 MCP、现有依赖和发布流程；继续服从部署网络设置 |
| `full_access` | 管理员配置的最大 capability 集合；仍不关闭 Sandbox、路径或秘密边界 |

`workspace_write` 的断网要求在执行器中强制收窄，即使部署网络设置是 `full_access` 也不会获得出网。本地源码 runner 无法兑现该网络隔离时明确拒绝，使用 Sandbox 可执行断网 Shell。主会话的默认 `autonomous` 保持已有网络设置语义：默认 `web_tools` 下普通 Shell 断网，依赖命令按 #604 获取执行级临时出网；#627 继续处理 bootstrap 重试和降级诊断。没有第二套网络授权系统。

能力和插件策略在真实调用时重新读取。模型/仓库不能选择 Docker 网络、挂载、宿主命名空间、环境秘密或关闭 Sandbox。MCP 的只读分类来自部署配置，远端服务自己的 hint 不能扩权；应只将可信、确实只读的远端操作标为只读。

Hook 的独立参数 `{workspace}` 由所选执行后端展开：本地是实际工作区路径，Sandbox 是容器中的 `/workspace`。仓库内容不能安装命令 Hook。Hook 依赖已有 runner 的单次执行/进程清理设置，没有新增任务累计预算。

## 持久化、压缩与用量

数据库新增 `agent_team_subagents`（父子会话与历史范围）和 `agent_team_usage`（provider usage receipt）表，沿用现有模型建表流程；不重写已有历史列或推算旧任务用量。插件设置在 `app_config` 中，现有配置备份导入/导出尚不包含插件专用设置；迁移部署时应独立保存插件配置及部署凭据。

主/子 Agent 和摘要请求的实际 provider usage 在模型返回后、执行它选定的工具前持久化，并与任务统计增量在同一事务提交。重复 receipt 不重复计数；这里记录用量和估算费用，不新增扣款。provider 已产生回复但本进程在首次持久化前崩溃的极小窗口无法凭本地 receipt 重建，也不虚构 token 数。

每次实际压缩记录压缩前后 token **估计**和保留证据标识，保留当前任务、真实用户指导、最近完整工具批次和未解决错误。审计不存放秘密、原始工具参数或完整正文，也不成为新的模型/用户消息。usage 持久化失败不会被压缩 fallback 吞掉后继续请求模型。

本地和模拟验证的确切边界见[验收记录](AGENT_HARNESS_2_ACCEPTANCE.md)。真实 provider 完成率、生产 MySQL 竞争、多主机执行和实际 Docker 部署不能由 SQLite、HTTP fixture 或生成的容器命令推断。

## English summary

The unattended Agent has no new model-round, cumulative tool-call or task-step budgets. Only an accepted `finish_task` completes normally; required pre-finish hooks can return actionable failures. Repeated identical work prompts autonomous strategy review without terminating the task or changing models.

Safe reads run concurrently. Mutations and completion validation are exclusive. An event-loop barrier and advisory lock on the shared workspace directory coordinate local processes without repository lock files or time budgets. This requires shared filesystem `flock` semantics and is not distributed task/session ownership. Cancellation drains child tasks/processes before releasing exclusion; uncertain historical mutations require reconciliation instead of blind replay.

Backend import/startup does not require the optional POSIX locking module. Hosts without `fcntl` or safe directory-open support can start the general backend, but actual Harness workspace operations fail explicitly with `workspace_lock_unavailable`; they never silently drop process exclusion. Native Windows Harness workspace execution is not implemented by this import compatibility fix.

Subagents have isolated durable sessions, immutable readonly/Skill ceilings, scoped wait/cancel/results and parent cleanup. `agent_team_subagent_concurrency` defaults to four live children with queued work; there is no lifetime call cap. Main and child sessions retain the configured `agent_team` model. Readonly search/diff uses readonly, offline Sandbox mounts or Linux Landlock ABI ≥ 3 plus libseccomp. Unsupported backends fail explicitly; macOS local readonly execution is not implemented. Deploy compatible sandboxd/runner versions together.

`check_changes` requires Git with `--no-lazy-fetch` support (upstream **2.45+**) in the selected backend, including source-local system Git. Older binaries receive a nonterminal `GIT_READ_ONLY_UNSUPPORTED` prerequisite error before status/diff. The runtime never retries without the protection; other tools remain available.

Content search requires the selected runner to enumerate query-independent text candidates, then builds matches only from descriptor-admitted regular single-link files. Links/special files and raw grep contents never become results. Newline-separated literal patterns are alternatives; case-insensitive matching uses Unicode casefold. Incomplete enumeration reports truncation. Cancellation drains the current read and stops later candidates; runner failure never falls back to host search.

Profiles intersect existing execution boundaries. `workspace_write` enforces offline Shell even if the deployment network setting is broader; a local runner unable to isolate networking rejects that profile. Default `autonomous` preserves existing configured networking and reuses #604/#627. No runtime approval queue is introduced. Trusted command Hooks alone can execute; `{workspace}` expands into the selected backend's namespace.

The additive subagent/usage tables preserve session identity and idempotent usage receipts. Provider usage settles before model-selected effects; no new balance charging is introduced. A crash before the first receipt commit remains an accounting boundary. Compaction audit records token estimates and retained task/tool/error evidence without promoting untrusted content. Plugin settings live in `app_config` but are not yet covered by generic configuration backup/import. See the linked plugin guide and acceptance record for setup and observed validation limits.
