# Sakura-AI Agent 重构最佳实践研究

> 供 Agent Team 重构和 Luna-Worker 实施使用的研究结论。
>
> 研究日期：2026-08-20（Asia/Shanghai）
>
> 本文只新增研究文档，不改变运行时代码、数据库或 WebUI。文中明确区分：
>
> - **来源事实**：来自官方文档或项目维护者的一手 GitHub 源码，可直接复核。
> - **Sakura 推论**：结合 Sakura-AI 当前需求得到的设计建议，不声称是外部项目的强制规范。

## 结论摘要

| 主题 | 建议的目标行为 | 主要依据 |
|---|---|---|
| Prompt 角色 | 固定系统提示词只承载 Agent 的稳定身份、工具边界和安全约束；初始任务和管理员指导都作为 `user` 输入；管理员指导进入队列，并在下一次模型调用前注入。 | [OpenAI Responses API 的 `instructions` 与 `input` 角色定义](https://platform.openai.com/docs/api-reference/responses/create)、[OpenAI Agents SDK 的 Agent 配置](https://openai.github.io/openai-agents-python/ref/agent/)、[RunState 暂存新输入](https://github.com/openai/openai-agents-python/blob/main/docs/results.md) |
| Agent 形态 | Agent Team 只保留一个实现型 Agent profile；PR 审查是独立的 review workflow，不再作为实现任务中的第二个“专家”。 | [GitHub Copilot coding agent 完成后请求独立 code review](https://docs.github.com/en/copilot/how-tos/copilot-on-github/use-copilot-agents/overview)、[GitHub Copilot code review](https://docs.github.com/en/copilot/concepts/agents/code-review) |
| 任务寿命 | 去除产品级墙钟 `task_timeout`；运行默认可持续到完成或显式取消。仍保留取消、模型/工具调用故障处理、资源/步骤预算和并发限制等安全控制。 | [LangGraph interrupt 等待外部输入](https://docs.langchain.com/oss/python/langgraph/interrupts)、[Agents SDK 的 `max_turns=None` 与取消](https://openai.github.io/openai-agents-python/running_agents/)、[Agents SDK usage](https://openai.github.io/openai-agents-python/usage/) |
| Skills | 采用目录化 `SKILL.md` manifest；启动时只发现元数据，匹配后才加载正文，正文引用的资源再按需读取；安装/更新先验证，权限默认收紧，版本和来源放入 Sakura 命名空间元数据。 | [Agent Skills specification](https://github.com/agentskills/agentskills/blob/main/docs/specification.mdx)、[Agent Skills client implementation guide](https://github.com/agentskills/agentskills/blob/main/docs/client-implementation/adding-skills-support.mdx)、[GitHub Copilot Skills 安全说明](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-cloud-agent/add-skills) |
| 实时 UI | 用有序、可恢复的语义事件时间线表达“回合、消息、工具、技能、等待/取消/完成”；默认折叠低价值细节，详情再展开。 | [OpenAI Agents SDK semantic stream events](https://github.com/openai/openai-agents-python/blob/main/docs/streaming.md)、[LangSmith Messages/Turns/Details 视图](https://docs.langchain.com/langsmith/view-traces)、[LangGraph 多投影事件流](https://docs.langchain.com/oss/python/langgraph/event-streaming) |

## 1. Prompt 角色边界：系统契约与动态人类输入分离

### 1.1 来源事实

1. OpenAI Responses API 将 `instructions` 定义为插入上下文的 system/developer 指令；`input` 可以是带角色的消息列表。`system`/`developer` 消息在指令层级上优先于 `user` 消息；当使用 `previous_response_id` 时，上一轮的 `instructions` 不会自动延续。详见 [Responses API `create` 参考](https://platform.openai.com/docs/api-reference/responses/create)。
2. OpenAI Agents SDK 把 `Agent.instructions` 定义为该 Agent 被调用时使用的 system prompt，并允许用动态回调生成它。也就是说，“每轮动态生成的 instructions”仍然处于 system 层，而不是普通用户输入层。详见 [Agent API reference](https://openai.github.io/openai-agents-python/ref/agent/)。
3. Agents SDK 的 Runner 将一个字符串输入当作 user message；暂停或取消后到达的新输入可以用 `RunState.add_input()` 暂存，恢复时会在下一次模型请求前接纳，并以可持久化的 `InputItem` 保留顺序。`RunConfig.session_input_callback` 也专门用于合并会话历史和新一轮 user input。详见 [Results: Add input before resuming](https://github.com/openai/openai-agents-python/blob/main/docs/results.md)。

### 1.2 Sakura 推论与目标消息模型

“管理员指导”是运行期间新增的人类意图，不应修改稳定的 Agent 身份、权限或安全边界。因此它不能拼接进 system prompt，也不应伪装成 developer prompt。它应该是有审计记录的、来源明确的 `user` 消息，并在下一次模型调用前由运行时注入。

建议的逻辑上下文如下（省略工具结果）：

```text
system    固定 Agent 身份、工作协议、工具/工作区边界、安全规则
user      初始任务 prompt（创建任务时写入一次）
assistant 运行中的模型消息
tool      工具调用及结果
...
user      [管理员指导 #g-123] 仅在下一次模型调用前接纳的指导
assistant 继续执行
```

实现上应有一个独立的 `guidance_queue`，而不是在拼接 prompt 时读取一个可变的“管理员指导”字符串。建议每条队列项至少持有：

```text
guidance_id, task_id, content, author_id, created_at, sequence,
status(pending|admitted|consumed|cancelled), admitted_turn_id,
created_event_id, consumed_event_id
```

`author_id` 只用于授权和审计；它不代表模型层级。管理员身份不能借由把文本放入高优先级 prompt 来扩大 Agent 的工具权限。权限检查、可执行操作审批和危险工具确认必须在运行时/工具层完成。

### 1.3 下一次模型调用的安全接纳协议

以下是针对 Sakura 的实现推论，依据上述“暂存输入 + 可恢复运行状态”模式：

1. 提交指导时只写入持久队列并发布 `guidance_queued` 事件；若当前回合正在生成，不要修改已经发送给提供商的请求。
2. 在模型调用 admission point 取得任务锁，按 `sequence` 读取所有 `pending` 项，将它们转换为 user-role input item，写入同一份会话/RunState，然后一次性标记为 `admitted`。
3. 只有在输入已持久化并且该次模型请求可以看见它之后，才标记为 `consumed`；如果在 admission 前失败，保持 `pending`，不能静默丢失。
4. 通过 `(task_id, guidance_id)` 唯一约束或等价幂等键，保证重试不会重复插入同一指导。持久化的状态和 SSE 事件都使用稳定 ID；前端按 `event_id`/序号去重。
5. 当前运行已经 terminal、没有下一次模型调用，或者正在取消时，指导应显示为 `rejected`/`cancelled`/`queued-for-new-run` 中一种明确状态，不能显示成已经消费。
6. 暂停后恢复必须使用同一个 task/run 状态标识；LangGraph 的一手实现也要求用同一个 `thread_id` 找回 checkpoint，外部输入通过 `Command(resume=...)` 恢复。[LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) 和 [persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 提供了这一状态模型的直接参考。

## 2. Agent profile 与 PR review 的边界

### 2.1 来源事实

1. OpenAI Agents SDK 同时支持普通单 Agent、handoff 和 agents-as-tools；它把 handoff 定义为把对话交给专业 Agent，把 agents-as-tools 定义为由一个 Agent 保持对话控制、调用另一个 Agent 完成有界子任务。[Agent orchestration](https://openai.github.io/openai-agents-python/multi_agent/) 和 [handoffs](https://openai.github.io/openai-agents-python/handoffs/) 明确说明了这两种不同的编排语义。
2. GitHub 的 coding-agent 教程把流程拆成“实现任务完成并创建 PR”与“在 PR 上请求 Copilot code review”；教程要求用户阅读 review 结果并像审查普通贡献者一样复核。[Get started with Copilot agents](https://docs.github.com/en/copilot/how-tos/copilot-on-github/use-copilot-agents/overview)。
3. GitHub 的 code-review 文档把 review 描述为针对 PR 的反馈能力，并明确提醒 review 可能出错，必须由人验证。[About GitHub Copilot code review](https://docs.github.com/en/copilot/concepts/agents/code-review)。

### 2.2 Sakura 推论

当前需求并不需要“全栈专家”和“审查专家”同时存在于同一个 Agent Team 任务。建议：

- `implementation` 是 Agent Team 唯一可执行 profile，拥有当前任务所需的受控文件、shell、测试和 Skills 工具。
- PR review 保留为独立入口/工作流，使用只读 review prompt、差异/基线上下文和独立输出协议；它不是 `implementation` profile 的一个可切换“专家身份”。
- 需要修复 review 意见时，再由 review workflow 产生一个明确的实现任务，交回 `implementation` profile；不要让 review Agent 在同一条审查链里隐式修改代码。
- Agent Team 页面移除专家卡片、专家切换器和没有实际编排意义的“全栈/审查”标签；页面只显示任务类型、实现 Agent 状态以及是否存在独立 review 结果。

这使实现任务的身份、工具白名单和上下文保持稳定，也避免模型在两个近似 profile 之间浪费一次 handoff。若未来出现真正有独立权限或独立生命周期的专业工作，才使用 handoff/agents-as-tools，而不是为了 UI 分类创建第二个专家。

## 3. 任务寿命：无产品级墙钟上限，但保留可控停止面

### 3.1 来源事实

1. LangGraph 的动态 interrupt 会持久化当前图状态，并等待外部输入；官方文档明确描述为可一直等待，使用相同的 thread ID 和 `Command` 恢复。[Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)。
2. OpenAI Agents SDK 的 `Runner` 以 `max_turns` 控制 Agent-loop 回合数，并明确支持 `max_turns=None` 关闭该回合限制；它还支持 `result.cancel()` 立即停止或在当前回合结束后停止。[Running agents](https://openai.github.io/openai-agents-python/running_agents/) 和 [streaming](https://github.com/openai/openai-agents-python/blob/main/docs/streaming.md)。
3. Agents SDK 会跟踪每次运行的 requests、input/output/total tokens，可用于监控费用和执行限制；本地工具还可以配置并发上限。[Usage](https://openai.github.io/openai-agents-python/usage/) 和 [Run config](https://openai.github.io/openai-agents-python/ref/run_config/)。SDK 仍区分模型调用超时和 Agent-loop 的回合限制，`ModelTimeoutError` 不是任务级墙钟截止时间。[Running agents exceptions](https://openai.github.io/openai-agents-python/running_agents/)。

### 3.2 Sakura 推论

删除 `task_timeout_seconds`、任务超时设置页/接口和“超时失败”终态；任务在 worker 存活、依赖可用且未被取消时可以持续执行。这里的“不加限制”应解释为**不再用一个产品级墙钟 deadline 终止整个任务**，而不是允许失控的网络请求或无限资源消耗。

保留以下彼此独立的控制面：

- **显式取消**：`cancel requested -> cancelling -> cancelled`，支持立即取消和当前模型/工具回合完成后取消；取消后继续排空事件流并落库终态。
- **传输/调用故障控制**：模型 HTTP、Redis、GitHub 等单次调用仍需自己的连接/读取/重试策略，以便断开时能恢复或报错；它们不应被展示为任务超时。
- **资源/步骤预算**：请求数、token、工具并发、单个工具输出大小和可选的步骤上限用于成本与循环保护。默认可以不启用产品级步骤截止，但必须存在可观测和管理员可配置的保护面。
- **进程级故障恢复**：worker 重启、模型失败或机器掉电后，从持久运行状态恢复，而不是因旧墙钟已过直接标记失败。

建议状态机不再包含 `timed_out`：

```text
queued -> running -> waiting_guidance/ waiting_approval -> running
                         |                              |
                         +-> cancelling -> cancelled   +-> succeeded/failed
```

`waiting_guidance` 表示正在等待下一条管理员指导或明确的外部输入；它不是失败，也不是超时。

## 4. 可扩展 Skills 系统

### 4.1 来源事实

| 能力 | 来源事实 |
|---|---|
| Manifest | Agent Skills 规范要求每个 Skill 是一个目录，至少包含 `SKILL.md`；YAML frontmatter 的 `name` 和 `description` 必填，`license`、`compatibility`、`metadata`、`allowed-tools` 可选。[Specification](https://github.com/agentskills/agentskills/blob/main/docs/specification.mdx) |
| Progressive disclosure | 规范建议启动时只加载 name/description，匹配后加载完整正文，再按需加载 `scripts/`、`references/`、`assets/`；正文建议小于 500 行/5,000 tokens。[Specification: progressive disclosure](https://github.com/agentskills/agentskills/blob/main/docs/specification.mdx) |
| Discovery | 参考客户端按目录扫描 `skill-name/SKILL.md`，至少保存 name、description、location；也允许在激活时再读正文以节省内存并获取最新内容。[Adding skills support](https://github.com/agentskills/agentskills/blob/main/docs/client-implementation/adding-skills-support.mdx) |
| Validation | 规范提供 `skills-ref validate ./my-skill` 作为参考校验入口；`skills-ref` README 同时说明它是 reference library，不是生产库。[Specification](https://github.com/agentskills/agentskills/blob/main/docs/specification.mdx)、[skills-ref README](https://github.com/agentskills/agentskills/blob/main/skills-ref/README.md) |
| 权限 | GitHub 文档说明 `allowed-tools` 可以预批准工具，但明确警告预批准 `shell`/`bash` 会移除确认，并可能使攻击者控制的 Skill 或 prompt injection 执行任意命令；不确定时应省略该字段。[Adding agent skills for GitHub Copilot](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-cloud-agent/add-skills) |
| 生态发现 | GitHub Copilot 支持项目级 `.github/skills`、`.agents/skills`、`.claude/skills` 和个人目录，并用优先级解决同名 Skill；Skill 内容按需注入而非全部预加载。[About agent skills](https://docs.github.com/en/copilot/concepts/agents/about-agent-skills)、[CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference) |
| 内容质量 | Agent Skills 官方最佳实践建议从真实任务中提炼 Skill，控制正文上下文成本，拆分大 Skill，并用可重复的验证脚本/检查清单形成“执行-验证-修复-再验证”循环。[Best practices for skill creators](https://github.com/agentskills/agentskills/blob/main/docs/skill-creation/best-practices.mdx) |

### 4.2 Sakura 建议的 manifest 扩展

保持标准字段可互操作；Sakura 特有信息放在 `metadata.sakura`，不要把运行时状态或权限决策写进 Markdown 正文：

```yaml
---
name: sakura-example
description: A short description and when to activate this skill.
license: Apache-2.0
compatibility: Requires Python 3.14 and the repository test tools.
allowed-tools: "Read"
metadata:
  sakura:
    schema_version: 1
    version: 1.2.0
    min_agent_version: 0.0.0
    source: github:Sakura520222/example-skill
    revision: "<commit-or-content-digest>"
    permissions:
      tools: ["Read"]
      network: false
---

# Instructions

Keep the frequently needed workflow here; put long references in `references/`.
```

这是一种 Sakura 推论：规范本身只规定必需/可选字段，并没有跨客户端统一的 Skill version 语义。因而 Sakura 应把 `schema_version`（manifest 结构版本）和 `version`（Skill 内容版本）分开；同时保存来源、revision/digest 和安装者，以便审计、回滚、同名冲突处理和更新提示。

### 4.3 发现、验证、激活、更新的生命周期

建议实现为以下可观察的流水线：

```text
discover metadata -> validate manifest -> show catalog
                  -> activate on match -> read SKILL.md
                  -> read referenced resource on demand
                  -> execute through normal tool/policy boundary
                  -> record source/version/digest/usage
```

具体约束：

1. **发现**：只读取 frontmatter，生成 `name/description/location/source/version/validation_status`；不把所有正文塞进 system prompt。无有效 Skill 时不要注册空的 Skill 工具或空列表。
2. **校验**：安装和更新阶段解析 YAML、校验必填字段/字符集/长度、目录名与 name、相对引用、资源大小、符号链接/路径穿越和 digest；脚本不在安装阶段执行。必填字段缺失时跳过；非关键兼容问题可警告并显示诊断，这与参考客户端的 lenient validation 建议一致。[Client implementation guide](https://github.com/agentskills/agentskills/blob/main/docs/client-implementation/adding-skills-support.mdx)
3. **权限**：`allowed-tools` 只能作为候选声明，不能绕过 Sakura 的工具白名单、工作区边界和审批；默认不自动批准 shell/network/写操作。技能包更新若扩大权限，必须让管理员重新确认并产生审计事件。
4. **激活**：以 `activate_skill(name, task_id)` 或受控文件读取方式返回正文；正文中的 `references/`、`scripts/`、`assets/` 仍按需读取。Skill 是说明/流程扩展，不应自行获得超出 Agent Team 工具策略的能力。
5. **更新**：下载到临时版本目录，完整校验后原子切换；保留 active revision 和上一版本，失败时回滚。名称冲突时按明确的 project/user/bundled 优先级解析，并在页面显示实际来源。
6. **质量评估**：为关键 Skill 保存最小输入/预期行为测试，要求执行验证脚本或 checklist；把触发描述当作可评估字段，避免含糊的“适用于所有任务”。

### 4.4 agent-skills 页面重构建议

这是基于上述来源事实的 UI 推论：页面以紧凑表格为主，而不是把完整正文和所有资源平铺出来。每行显示：启用状态、名称、短描述、来源、版本/revision、验证状态、权限摘要、最近使用时间；点击行打开详情抽屉，展示 manifest、诊断、引用资源和审计记录。安装/更新流程拆成“选择来源 -> 预览 manifest/权限 -> 验证 -> 启用”，正文在详情中按需读取。

## 5. Agent Team 实时流与高密度 UI

### 5.1 来源事实

1. OpenAI Agents SDK 同时提供原始 token/response events 和更高层的 `RunItemStreamEvent`。后者用固定语义事件表示完整生成的消息、工具调用、工具结果、技能搜索和 handoff，适合按“消息生成/工具运行”而非每个 token 更新界面；流结束后才代表持久化、审批和 compaction 等后处理完成。[SDK streaming 源码](https://github.com/openai/openai-agents-python/blob/main/docs/streaming.md)。
2. SDK 的流可以在工具审批处暂停，转成可恢复状态后继续；`cancel()` 支持立即取消或当前回合后取消，因此 UI 需要区分 waiting approval、cancelling 和 terminal，而不能仅凭“最后一个 token”判断完成。[Streaming](https://github.com/openai/openai-agents-python/blob/main/docs/streaming.md)、[Results](https://github.com/openai/openai-agents-python/blob/main/docs/results.md)。
3. LangSmith 把线程作为主要导航单位，并提供 Messages（看完整轨迹）、Turns（每回合可折叠摘要）和 Details（单次运行的输入、输出、耗时、token、错误和 metadata）三层视图；工具并行调用可聚合为一行再展开。[View traces](https://docs.langchain.com/langsmith/view-traces)。
4. LangGraph 的事件流可以同时输出 state、messages、subgraphs，并提供严格到达顺序的 interleave；这说明 UI 可消费多个投影而不把所有底层数据塞进同一文本流。[Event streaming](https://docs.langchain.com/oss/python/langgraph/event-streaming)。

### 5.2 Sakura 推论：统一事件契约

后端应先把 provider/worker 事件归一化，再由 SSE/WebSocket 输出；建议最少字段如下：

```text
event_id, task_id, run_id, turn_id, sequence, occurred_at,
kind, status, actor, summary, payload_ref, parent_event_id
```

建议的 `kind` 集合：

```text
task_created, task_started, turn_started, assistant_message,
tool_called, tool_output, skill_discovered, skill_activated,
guidance_queued, guidance_admitted, approval_required,
waiting_guidance, cancel_requested, task_cancelled,
task_succeeded, task_failed
```

不要把任意 log line 当作 UI 事件。日志仍进入诊断通道；事件流只发送能改变任务状态或解释 Agent 行为的事实。事件必须单调有序且可用 `event_id` 去重，以支持断线重连和页面刷新。

### 5.3 Sakura 推论：agent-team 页面

推荐一个紧凑的三段布局：

```text
┌──────────────┬────────────────────────────────────┐
│ 任务列表       │ 当前任务头部：状态 / 回合 / 取消        │
│ 状态筛选       ├────────────────────────────────────┤
│ 标题 + 状态    │ 按 turn 分组的语义时间线                 │
│ 最近活动       │ 消息、工具、Skill、等待、审批、结果       │
└──────────────┴────────────────────────────────────┘
                 底部固定：管理员指导输入 + queued 数量
```

- 左栏只保留标题、任务类型、状态、最近活动时间和未读数；移除重复的专家卡片、长描述、空统计卡。
- 主时间线默认显示每个回合的摘要、助手可见消息、工具名/耗时/结果摘要、Skill 激活和状态变化；token delta 只用于当前正在生成的消息，完成后合并成一条。
- 工具结果、低价值日志和推理细节默认折叠；点击后在详情抽屉查看原始 payload、输入输出、耗时、token、错误和事件 ID。不要默认展示模型隐藏推理文本。
- 管理员指导使用明确的 user-message 气泡和 `queued/admitted/consumed` 标签；提交后可以继续查看，但不会假装已经改变当前正在进行的模型请求。
- 顶部状态必须有 `running / waiting_guidance / waiting_approval / cancelling / cancelled / succeeded / failed`，不再出现 `timed_out`。
- 取消按钮在运行中可见；等待审批/指导时显示恢复入口；取消后禁用输入或明确标记该指导将进入下一次新运行。
- 详情与时间线必须保持当前任务上下文，不要跳转到另一个“监控页面”才看得到对话。

## 6. 给 Luna-Worker 的实现验收清单

下面都是 Sakura 推论，用于把研究转成可验证的实现合同：

### Prompt 与队列

- 单元测试断言：system/developer prompt 不包含管理员指导正文；初始任务是 user item；指导按创建顺序作为下一次模型调用的 user item 进入。
- 两次提交同一 `guidance_id` 不会产生重复消息；模型调用失败、worker 重启和 SSE 重连不会丢失或重复消费指导。
- 指导有权限、审计、状态和明确的 terminal 行为；它不能通过 prompt 角色绕过工具授权。
- 暂停/恢复保留同一 task/run 会话和事件序号。

### Agent 与任务寿命

- Agent Team 只公开一个 implementation profile；专家选择器和全栈/审查重复配置消失。
- PR review 走独立入口，默认只读，有自己的结果/状态/权限边界；修复 review 意见时创建实现任务。
- 数据库、配置、API、前端不再提供产品级 `task_timeout`/`timed_out`；长任务在没有显式取消时不会因墙钟时间自动失败。
- `cancel` 有立即/回合后两种路径并可观测；资源/步骤/并发保护和单次传输超时不被误标为任务超时。

### Skills

- Skill 目录和 `SKILL.md` frontmatter 可被解析；必填字段、name 约束、资源路径和权限变化有验证诊断。
- 发现阶段只返回元数据；激活阶段才返回正文；引用资源和脚本按需加载。
- Sakura 版本、schema、来源 revision/digest 和实际权限有结构化字段；更新可验证、原子切换和回滚。
- shell/network/写操作默认不自动批准；Skills 不能扩大 Agent Team 既有工具权限。
- 关键 Skill 有验证脚本/测试样例，页面能显示验证失败原因和最近使用情况。

### 实时 UI

- SSE/WebSocket 事件包含稳定 ID、任务/回合 ID和序号；刷新、断线重连、并发工具和重复事件在前端可正确去重/排序。
- 主视图按回合和语义事件聚合，详情抽屉承载原始 payload；页面不把日志行和 token delta 堆成一面滚动文本。
- guidance、approval、cancel、success、failure 都有独立视觉状态；`waiting_guidance` 不显示为失败或超时。
- agent-skills 页面可在紧凑列表中完成发现、验证、启停、更新和权限预览。

## 7. 研究边界与注意事项

- 外部来源展示的是可迁移的运行时和 UI 模式，不是 Sakura-AI 的可直接复制实现；数据库事务、SSE 生命周期、模型适配器和现有安全边界仍需按仓库代码验证。
- Agent Skills specification 的 `skills-ref` 是参考实现，不能替代 Sakura 的生产验证和权限系统；尤其不能因为 manifest 声明了 `allowed-tools` 就绕过现有工具白名单。
- “无任务超时”不等于“无任何网络/资源保护”。应把墙钟任务 deadline、单次调用 timeout、取消信号和资源预算拆成不同字段/状态，并分别测试。
- GitHub Copilot code review 的反馈也需要人工验证；Sakura 的独立 PR review workflow 不应自动将低置信反馈变成代码修改。
- 本轮按 `agent-reach` 技能要求先尝试了 `agent-reach doctor --json`，但当前环境没有安装该命令；随后使用技能中规定的 GitHub CLI/网页只读备用路径，并仅采用官方文档或项目官方仓库作为来源。未执行外部写操作。

