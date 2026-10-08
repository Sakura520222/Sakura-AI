# Billing 2.0：配置、迁移与对账

本实现不提供正式商业价格。首次部署保持 `billing_enabled=false`，先审核历史
权益、配置正式价格/汇率/倍率/Credits 价值和开账余额，再显式启用。
`payment_enabled` 只控制支付入口；钱包、账单、管理员发放和定价可独立使用。
关闭购买时，已有订单历史、用户退款申请及管理员退款审核/处理仍可访问；
侧栏保留相应入口，概览隐藏购买和兑换表单。权限及 CSRF 检查保持生效。

超级管理员侧栏及全局配置的付费配置卡片常显「模型定价」和「套餐与价格」。
模型定价在 `/billing/admin/pricing` 选择已配置 AI 账号，编辑 Provider 原始单价
及用户结算换算；
套餐购买金额、发放 Credits 和限流权益在 `/billing/admin/plans` 编辑。配置
入口不以开启支付或收费为前提，购买与支付入口仍由 `payment_enabled` 控制。

模型报价支持逐字段表单及高级 JSON。Token 单价可按每 Token、每千或每百万
输入，服务器用 Decimal 统一到每百万；请求/文档/搜索单位使用实际接口计量。
币种、汇率、倍率与 Credits 价值均需明确填写，不预填商业参数。历史版本可
载入编辑，但发布只新增版本，不能覆盖历史账单引用的报价。

金额币种使用统一支持目录的下拉选择：Provider 成本币种、结算币种、套餐创建/
编辑、付款/退款核对，以及默认支付、Stripe、Paddle 的配置币种均复用
`currency_units.py`，USD/CNY 排在前面，包含其他受支持 ISO 币种及 USDT。
表单、高级 JSON、API 和套餐服务拒绝目录外的新币种；已保存历史值不会自动
改写，异常币种只显示为不可选的历史值，需要管理员明确重新选择。套餐价格
继续使用各币种的整数最小单位，不因更换控件转换金额。NOWPayments 的
`usdttrc20` 等接收资产/网络标识保持原协议语义，不用 ISO 币种替代。

当前支付宝接入为国内 `page.pay`，付款币种仅允许 CNY；表单、Settings、
全局配置 API、结账服务及网关均拒绝其他币种，不会把 USD/JPY 等标签与人民币
金额混用。历史异常订单须核对实际网关币种后处理，不能自动换标签或猜汇率。
历史未知币种的账单保留原整数及币种代码，并显示单位未知；API 的格式化金额
为 null、`currency_supported=false`。这只保证读取可用，不代表允许继续付款。

USDT 有四个字符；费用表 `billing_usage_charges` 的 provider_currency 和
settlement_currency 从 VARCHAR(3) 扩为 VARCHAR(10)。正常启动迁移检查并幂等
扩宽 MySQL/MariaDB/PostgreSQL 的旧列，保留空值、默认值、注释及已有字符集/
排序规则，不修改财务行、不移除保护触发器。SQLite 无需重建表。迁移账号仍需
ALTER 权限；本轮已验证 SQLite 及原生 DDL 编译/反射分支，未在运行数据库执行。

账号来自现有「AI 配置」，不展示通用厂商目录。选择账号后立即展示该账号
已保存的模型及默认模型，再用账号的服务端凭据自动获取模型列表；失败时保留
该账号保存的模型并提示重试。自动发现结果短暂缓存，手动「重新获取模型列表」
绕过缓存。发现只请求模型元数据，不发送测试 Completion，不产生 AI Usage。
独立 Embedding/Rerank 仍使用全局配置中的各自 Provider/模型，页面锁定该来源，
不将其冒认作某个 AI 账号。API Key 和 API Base 不传到定价页面或模型列表响应。

启用收费时，保存反馈会列出缺少的账号/Provider/模型/调用类型报价，标记收费开关
并链接模型定价页；预留精度、回收时限等无效值会定位各自字段。配置页、批量
保存、系统配置及报价保存按当前用户个人语言返回反馈。部分保存失败保留输入，
明确区分已成功保存的分区；错误响应不包含原始输入、Prompt或凭据。
收费已开启时，部分更新压缩、Embedding/Rerank 路由以及 AI 账号或角色绑定也
重新校验报价。拟保存的账号/绑定先投影解析，验证与保存之间持有数据库配置
行锁，缺价拒绝提交；未使用的账号、保留已定价路由的改名仍可保存。

## 金额与价格

一个 Credit = 1,000,000 个整数微 Credits。余额、流水和预留用 BigInteger；
价格及计算用 Decimal，拒绝 float、NaN、负价格和不明确的单位。
`credit_grant` 最多六位小数。测试价格均为测试样本，不能启用到生产。

`/billing/admin/pricing` 发布不可变的 `account_id + provider_id + model_id + call_kind`
价格版本；Provider 从已保存账号解析，同 Provider/模型的不同账号可独立定价。
「模型调用」一份报价同时覆盖流式及非流式调用，定价类别统一为 `chat`。
原始 Usage/请求尝试仍保存实际 `chat` 或 `chat_stream`，计价快照同时保存
实际调用类型和定价类别。压缩、Embedding、Rerank 继续保留独立定价类别。
独立辅助模型和历史未绑定报价使用 `account_id=null` 的 Provider 范围。
版本号继续使用 Provider/模型/调用类型的全局递增审计序列，以兼容原唯一约束；
不同账号版本号可能不连续，查价仍严格按账号隔离。
配置必须提供 `currency`、`settlement_currency`、`fx_rate`、`markup`、
`credits_per_currency_unit`：

| unit | 价格与单位 |
| --- | --- |
| tokens | input_price/output_price，每百万 Token；独立 cached_input_price/cache_creation_price/reasoning_price 需要实际对应计量 |
| requests/documents/search_units | unit_price，每个单位；meter 必须匹配实际接口或权威完成时测得的计量 |

原始 Usage、Provider 成本、结算金额与应付 Credits 分层保存。每条费用有 Usage
引用、价格版本、语义/计量快照、换算参数及舍入规则。调用前冻结价格，管理员
改价不影响历史。Fallback 按实际命中的账号、Provider、模型定价，不使用
TokenTracker 展示估算。调用尝试、原始 Usage、计价快照和人工对账均保留
account_id；人工核对不能用其他账号的报价替换该调用。

旧 API 提交 `call_kind=chat_stream` 会发布新的统一 `chat` 版本。旧流式报价、
已固定报价的调用和历史账单保持原版本及原金额，不原地修改。仅有旧流式报价
时不会替管理员推断普通调用应收哪种价格；请载入旧版本审核后发布统一模型
报价，新请求才能使用。启用收费检查只要求统一模型报价，不再分别要求流式
报价。尚未结算的历史异常币种/配置进入待核对定价状态，不凭未知币种算精确费。
非零价格/汇率支持最多 36 位有效数字、36 位小数；超过支持精度明确拒绝，
计算使用独立的高精度 Decimal 上下文。货币与来源的比例退款用整数除余计算，
保留累计尾数，不能依赖进程默认 Decimal 精度。
操作内累计未舍入费用后向上舍入至微 Credit，增量结算收取累计应付与已结算
的差额，不对每个辅助调用分别进位。

模型确实不支持的维度可在版本化配置中明确指定 cache_read_supported /
cache_creation_supported / reasoning_supported=false，并保留缺失计量为 null；
不能为绕过待核对而声明不真实的能力。未知缓存或独立 Gemini thoughts 不当作
零。只有明确不支持，或相同的包含式费率证明分量不影响金额，才可在缺少分量
时计算已知费用。返回了正用量但声明不支持时仍进入待核对。

缺失字段与零不同；没有必要 Usage/价格不等于免费。OpenAI 缓存读取和推理
通常属于父计数子集；Anthropic input 排除缓存读取/创建；Gemini thoughts 与
候选输出分开；DeepSeek cache miss 是普通未缓存输入。Embedding/Rerank 按
实际接口单位处理，不生成虚构输出 Token。取消时的流式中间快照保留为不完整
证据，不能作为最终精确费用。

协议依据：[OpenAI](https://developers.openai.com/api/docs/guides/reasoning)、
[Anthropic](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)、
[Gemini UsageMetadata](https://ai.google.dev/api/generate-content#UsageMetadata)、
[DeepSeek](https://api-docs.deepseek.com/api/create-chat-completion/)。

## 执行、身份与政策

服务端 BillingContext 包含可信 payer、operation、feature 和业务 source。
队列/持久化业务行显式保存标识，恢复与重投递沿用，用户新执行/新续问获得新
标识；自动 Agent 续轮显式恢复累计执行。扫描的人工 retry 使用新 operation
和本次服务端验证的触发者；可信 `process_scan(..., resume=True)` 恢复原标识，
待核对执行先解决未知证据。PR 完整审查后排队的增量执行使用队列自身付款人，
不继承之前手动/管理员审查的付款人；旧队列没有可信付款证据时平台承担。
每个真实上游请求都有独立 UUID，
不能用 logical call 合并真实重试/Fallback。主调用、摘要、压缩、Embedding、
Rerank 均继承顶层 feature。平台任务、共享索引、无绑定身份和管理员系统任务
记录平台承担原因；语言偏好用户不自动成为付款人。上下文退出后复原。
恢复按 operation 的冻结收费策略判断：`charging_enabled=false` 或明确平台付款
的执行，可在缺价/未知 Usage 时继续 Agent 自动审查轮次或可信 Repo Scan 恢复。
原 Usage、待定价/待核对原因仍保留，既有用户并发检查仍生效。后来开启全局
收费不改变旧执行；后来关闭全局收费也不能解除旧收费执行的金融核对要求。
自动事件中的已验证管理员仍保留触发者/语言身份，payer 明确为平台；可信
trigger_user_id 写入业务来源。用户钱包/套餐并发入场与周期或一次性次数消费在
同事务提交，余额或并发拒绝不消耗次数，次数拒绝回滚预留。Agent 任务载体与
入场一起保存，创建失败不留下孤儿预留；未知队列交接保留执行待核对。

PR/Issue 的已验签 delivery 使用 `webhook_execution_receipts` 记忆响应，以
feature+delivery 唯一键及签名正文指纹防止并发重投递再次入场/入队。已完成的
旧 Billing operation 同样去重；活动旧执行或外部副作用未知返回 503，不能凭
缺少 Usage 推断请求未发生。`/full-review` 先入场，再清理旧结果。
所有实例应统一升级并排空旧 handler；旧版本不认识回执，混跑无法提供同样保护。
新表随项目初始化/自动迁移创建，不改写余额或历史流水；回滚保留回执和审计。

回执恢复默认 dry-run，仅列出待核对项。应用核对需要活跃超级管理员身份、
明确证据及原因，不发送 GitHub 写入、AI 请求或队列消息：

```bash
uv run python scripts/reconcile_webhook_executions.py --limit 100 --offset 0
uv run python scripts/reconcile_webhook_executions.py --apply --receipt-id RECEIPT --actor-id ADMIN --resolution terminal --evidence "Verified durable terminal outcome" --reason "Recover lost acknowledgement"
```

terminal 要求已有持久化终态；accepted 是操作者确认已交接；cancelled_unstarted
要求确认未交接，拒绝存在调用或活动 Worker 的执行，并释放无调用预留。重复
核对不追加第二次审计；新的用户执行使用新 delivery。发生 `billing_conflict`
时回滚并用新事务重试，不在旧快照里反转钱包/operation 的锁顺序。

### Agent delivery 与 PR 增量恢复

Agent `/agent`（Issue/PR）将“任务已保存”和“已经交接”分开。仅保存或已入场但
未派发的重投递恢复同一 operation，次数与预留不会重复。首次响应为 202 queued；
后台协程创建不当作实际 Worker 证明。交接窗口未知的重投递返回 503，只有
原 execution 的持久 Worker 所有权或终态才可去重确认。Worker 入口在读取任务
付款者前检查回执令牌；取消或手动重跑后的旧协程不能执行新的载体。

Agent 没有启动自动重排队循环。未知交接使用已有回执核对入口；Worker 已声明
processing 时不能伪装为“未调度”。进程崩溃且没有活动 Worker 的过期 operation
先按现有账务恢复入口处理，再以 terminal 核对回执并同步原 queued 任务终态；
已换新 operation 的任务不被覆盖。不重投未知请求，不删除原审计或流水。

PR synchronize 增量保留入场时的付款者、operation 和来源；不同 operation 独立
串行执行，正在审查的旧 operation 不再吸收其他付款者的增量。所有调用仍累计
在其原 operation 内舍入。成功、失败、取消、PR 关闭均收敛对应队列和预留，
一个终态残留不会阻塞后续增量。

pr_review_incremental_queue 增加 nullable `dispatch_token`、`dispatch_expires_at`；
派发使用数据库条件声明和既有 service_execution_lease_seconds。Worker 在进入
账务/AI 作用域前校验令牌，旧派发不能结束新派发的账单。初始化/自动迁移追加
两列并保留旧行；应排空旧 Worker 后统一升级，回滚保留列、队列与财务记录。

增量恢复默认 dry-run，不调用 AI；应用审核清单仅将已过期、未进入 Worker、
没有实际调用或活动所有权的派发恢复为可调度状态。running/未知请求需先核对
账务，不能凭当前无 Worker 自动重发：

普通账务 recover 对仍有可信 pending/dispatching 载体、没有实际调用的增量
返回 queued_dispatch_recovery，保留原入场和预留，不把等待上一轮审查的有效
工作当成孤儿取消。该项交由增量恢复或已验证 PR 关闭的取消入口处理。

```bash
uv run python scripts/billing_maintenance.py recover-increments --batch-size 100
uv run python scripts/billing_maintenance.py recover-increments --apply --actor-id ADMIN --manifest reviewed-increments.json
```

清单每项包含 queue_id、evidence、reason。超级管理员可通过
`POST /api/v1/queue/increments/{item_id}/resume` 请求恢复，body 为 evidence/reason；
来源、原付款者及 operation 均从数据库重建，复核 GitHub App 的仓库访问和 PR
状态后调度现有 Worker。用户不能指定付款者，未配置入口不能绕过授权。

| app_config 参数 | 默认及语义 |
| --- | --- |
| billing_enabled | false；新操作保存收费开关快照 |
| billing_charge_failed_operations | false；失败/取消保留 Provider 成本，默认不新增用户扣费 |
| billing_charge_failed_calls | false；成功任务里的失败真实请求默认平台承担，保留其成本 |
| billing_initial_reserve_credits | "0"；入场预留可配置，收费启用后可用余额须为正 |
| billing_reservation_ttl_seconds | 3600；调用更新有效期，恢复入口处理过期执行 |
| payment_partial_refund_policy | reject；可选 proportional_unused_credits，只用于 Credits-only 套餐 |
| service_execution_lease_seconds | 300；共享执行名额与 Worker 所有权的租约时长，30–3600 秒；独立于任务总时长和 AI 调用次数 |
| service_execution_poll_seconds | 0.25；等待共享服务名额的检查间隔，0.05–60 秒 |

次数及并发限流独立于收费，重复执行的限流入场也有数据库幂等事件。套餐并发
采用有效来源的最高值；repo_scan_daily 按应用
日历日检查。旧一次性权益单独保存余额/消费来源，周期重置不生成新权益。

### 服务执行容量与每用户执行上限

| 限制 | 范围与到达上限后的行为 |
| --- | --- |
| max_concurrent_reviews | 所有共享同一数据库的 API/Worker 实例合计 PR 执行容量，默认 5，超出等待 |
| max_concurrent_issues | 同样范围的 Issue 执行容量，默认 5，超出等待 |
| agent_team_max_concurrent | 同样范围的 Agent 执行容量，默认 1；初始执行、恢复、自动审查续轮、人类 follow-up 共用名额 |
| Plan.concurrency_limit | 每用户 PR/Issue/Agent/Repo Scan 已登记、未记录终态结果的业务执行总上限，到达上限拒绝新登记 |

用户执行先登记再等待服务名额，所以排队也计入套餐上限。统计依据是
BillingOperation.outcome IS NULL；已经记录 completed/failed/cancelled 的业务
不再占用用户名额，即使账单仍待定价/待核对。恢复已经结束的执行重新检查用户
上限，沿用原 operation_id，不重算同次执行的每日限额。用户上限为 3、Agent
服务容量为 1、PR 容量尚有余量时，可同时执行 1 个 Agent 和 2 个 PR；登记
3 个 Agent 时最多运行 1 个，另外 2 个等待，用户的 3 个入场名额已占满。

用户套餐卡片仅展示价格、Credits、同时任务上限、期限及实际填写的套餐描述。
描述为空不插入占位说明；关于登记、终态和账务恢复的详细帮助保留在管理端
创建/编辑表单，不作为套餐介绍向购买用户展示。

共享服务容量通过 service_execution_gates 行写锁和 service_execution_leases
在短事务中申请/续约/释放，SQLite 也依靠数据库写入序列化，业务与 AI 请求期间
不持有事务或行锁。没有数据库不能降级为每进程私有名额。容量降低时已运行
任务正常结束，新任务等待合计数量低于新上限；修改容量、TTL、轮询的新值从
app_config 读取，已取得的租约保持自身时长，心跳按其时长的三分之一续约。

service_execution_ownerships 独立记录 Worker 的排队与运行所有权，不占服务
执行槽。恢复入口排除有效所有权，以当前锁定读重查，防止长排队/工具任务的
账务预留超时后被误判失联。结束时在独立短事务中保护账务结算的交接有效期，
不改余额、金额或历史流水。续约失效停止对应 Worker 并报告失败；取消期间
等待全部心跳/SQL 清理完成后释放，重复取消不会跳过释放。进程崩溃后不能续期
旧 token，过期名额可重用，旧 Worker 不得释放新持有者的名额。

各节点必须保持时钟同步。租约控制的是应用 Worker 执行容量；进程暂停、网络
分区或外部 API 已发出的未知窗口不能保证上游请求随 Worker 一起立刻停止，
真实 Usage 仍须对账，不宣称外部 exactly-once 或绝对无瞬时在途重叠。
数据库连接与事务失败必须处理其可观察错误；没有新增 Agent 固定轮数/调用数。

部署前停止或排空旧 Worker，再统一切换 API 与 Worker，不能混跑旧进程内
信号量和新共享池。正常模型注册/迁移会创建上述三个运行时表和索引，需要
CREATE/ALTER 权限；本轮未在运行库执行。回滚同样先停止新入场和全部新 Worker，
保留财务流水与业务标识，再整组切换执行协调方式。

钱包变更与不可变流水在同一数据库事务内。行锁、条件更新和唯一约束保护多
Worker，不靠进程锁。服务只 flush，调用方 commit。预留/释放不属于正式余额
变更，有独立追加事件。有效预留从可用余额中扣除；退款引用原交易，不删除流水。
数据库触发器与 ORM 禁止改写/删除财务、价格、计价和相关审计事件。

每次真实请求前持久化 started，结果与 Usage 同事务保存。写账失败阻断发送或
使业务失败，辅助模块不能吞掉财务异常后返回伪成功。外部请求未知窗口进入
pending_reconciliation，不声称跨外部 API 无条件 exactly-once。状态包括
not_started/running/usage_known/pending_pricing/pending_reconciliation/settled/
failed/cancelled；冲正通过引用原交易的追加流水表现。

预留不是费用上界。无法证明上界时，单个在途请求可超支，真实费用完整入账为
债务并阻止后续发送。充值按来源归还债务，剩余才成为可退款余额；消费退款
恢复原来源与债务资金。不得截断 Usage、凭空加钱或新增 Agent 固定调用/轮次
预算。已知费用可单独入账，同时保留待核对部分标记。

同一操作的自动续轮保留此前成功阶段的已结算金额；后续失败默认不新增该阶段
费用，不自动退掉此前成功阶段。已核对的失败/取消执行可按原 ID 从检查点恢复；
恢复后成功则按该操作实际可收费 Usage 累计结算。用户明确重跑使用新 ID。

## 迁移、切换与回滚

1. 停止旧 API/Worker 后部署同一版本，保持收费关闭；不混跑旧 bonus 写入或
   次数收费。沿用 init_db/migrate_schema_async 自动建表、补列及必要约束、索引、
   append-only 触发器。迁移角色需要 CREATE/ALTER/TRIGGER 权限，PostgreSQL
   还需函数权限；MySQL 开启 binlog 时还受服务器的触发器创建权限策略约束，
   见下方预安装步骤。缺权限不能跳过财务约束。
2. 按 [旧权益迁移](billing-legacy-migration.md) dry-run 审计并审核源快照；未知
   历史仅标记待核对，不从当前 Plan 猜购买内容、不归零、不追讨历史消费。
   可按审核清单显式 conversion_credits 转换可靠来源；不存在默认“1 次 = 1
   Credit”。保留限流来源，转换一次性余额及入账必须同事务、可重跑。
3. 为每个实际主/压缩/Fallback AI 账号配置模型报价，并配置独立辅助/外部 RAG
   模型价格及换算；正式商业参数待
   配置。收费启用校验拒绝未审计的已购旧权益和缺价格路由；后来新增的未知
   路由仍在发请求前拒绝。旧期初 Usage 不追溯收费。
4. 在隔离库验证购买/兑换 → Credits → AI → Usage → 定价 → 结算 → 账单，
   以及失败/退款。购买 Credits 不随订阅到期清空。订阅按确认支付周期发放，
   每日重置不能当每日充值。其他有效订阅、基础限额和其他购买来源不被撤销。

回滚可停止新入场/关闭新操作收费，但已开始操作仍按其政策快照恢复结算。
保留账本及新版本维护能力，不恢复错误 bonus 写入，不删除流水或改历史金额。
数据库整体恢复须对照外部支付、退款及 Usage 对账。

账号定价升级新增 Usage/调用尝试/报价的 account_id、报价 scope_key 与唯一索引，
沿用正常启动迁移。旧报价和旧调用保留未绑定身份及冻结版本，不从当前账号配置
猜测历史归属；旧 Provider 报价不能作为新账号调用的自动兜底。升级前保持收费
关闭，重启 API/Worker 完成增量迁移并为实际账号发布正式报价后再启用。已经
开始的旧执行仍按自身冻结报价核对/结算，恢复后新请求同样需要账号报价。
升级不修改既有流水或价格金额，不移除
不可变触发器；新增迁移尚未在部署数据库执行，不能把隔离 SQLite 验证当成
生产升级成功。

### MySQL 账本触发器预安装

MySQL 开启 binlog、`log_bin_trust_function_creators=0` 时，普通应用账号即使有
表级 `TRIGGER` 权限，创建触发器仍可能收到 1419。缺少创建权限时，迁移抛出
`BillingSchemaPermissionError`，错误代码 `billing_schema_privilege_required`，
阻止启动；不能以收费尚未启用或已有 ORM 防修改监听为由跳过数据库保护。
ORM 监听不能拦截原始 SQL 或批量更新。
[MySQL 官方说明](https://dev.mysql.com/doc/refman/8.4/en/stored-programs-logging.html)
解释了 binlog 下的额外创建权限要求。

仓库提供 [预安装 SQL](billing-mysql-guards.sql)，覆盖 12 张不可变表的 UPDATE /
DELETE，共 24 个触发器。导出脚本仅生成 SQL，不读取部署连接或连接数据库：

```bash
uv run python scripts/export_billing_mysql_guards.py > /tmp/sakura-billing-mysql-guards.sql
```

SQL 使用 `CREATE TRIGGER IF NOT EXISTS`，适用于 MySQL 8.0.29 及后续版本，
包括 8.4；这个语法从 [MySQL 8.0.29](https://dev.mysql.com/doc/relnotes/mysql/8.0/en/news-8-0-29.html)
引入。它不创建业务表，也不包含 DROP、授权或全局参数修改。安装前停止应用，
由 DBA 确认目标数据库及 12 张表已存在，审阅 SQL 后使用具有当前服务器所需
创建权限的管理员账号执行；密码交互输入，不写在命令中：

```bash
mysql --user=YOUR_MIGRATION_ADMIN --password --database=YOUR_SAKURA_DATABASE < docs/billing-mysql-guards.sql
```

DDL 可能逐条提交；中途失败须核对已安装部分，解决权限或对象冲突后重跑。
`IF NOT EXISTS` 不会修复错误的同名触发器。重启时应用验证已有触发器的表、
事件、BEFORE / ROW 属性及拒绝改写的主体；定义冲突仍阻止启动，不能仅按
名称存在判定保护已生效。正确预安装后，应用不再创建这些已有触发器。

应用账号仍须有对应表的 `TRIGGER` 权限，才能读取
`information_schema.TRIGGERS`；缺少权限会使保护不可见，不能把空查询结果当成
安装成功。[元数据权限说明](https://dev.mysql.com/doc/refman/8.4/en/information-schema-triggers-table.html)
适用于启动验证。SQL 未指定 `DEFINER`，默认使用安装账号；该账号必须持续存在
并保有所需触发器执行权限，不能在安装后删除临时迁移账号。
[DEFINER 说明](https://dev.mysql.com/doc/refman/8.4/en/create-trigger.html)
列出了创建及执行时的权限检查。

生产数据库安装、账号权限及全局参数变更需要单独授权。本任务未执行这些操作，
也不建议给应用账号 `SUPER` 或永久放宽 `log_bin_trust_function_creators`。
预安装之后仍需核对启动结果并在隔离 MySQL 上验证原始 UPDATE / DELETE 被拒绝；
预安装指导及离线导出通过不能当成部署数据库验收通过。

## 恢复与对账入口

```bash
uv run python scripts/billing_maintenance.py audit --batch-size 100
uv run python scripts/billing_maintenance.py recover --batch-size 100
uv run python scripts/billing_maintenance.py recover --apply --actor-id <super-admin-id>
uv run python scripts/billing_maintenance.py resolve-calls --manifest reviewed-usage.json --actor-id <super-admin-id>
uv run python scripts/billing_maintenance.py resolve-calls --manifest reviewed-usage.json --actor-id <super-admin-id> --apply
```

默认只读/dry-run，URL 来自现有配置且不打印。核对余额 = 正式流水之和；
预留 = 有效执行预留 + 未确认退款隔离预留；可用余额 = 余额 - 预留。
recover --apply 前确认执行已失联，不对仍在进行的长工具任务主动回收。
Usage 核对清单是 JSON 数组，需 call_id/event_key/reason/usage/outcome，可提供
审核 billing_units；无价格的原调用必须显式指定审核 price_profile_id。核对
追加新 Usage 和审计，保留旧未知证据。已冻结费用不能原地改价改量，应追加
引用原交易的冲正/调整。重复相同证据没有第二次财务效果。

尚未形成费用的异常定价可通过核对清单显式指定审核版本，核对事件保留原冻结
版本和审核版本；不会自动使用最新价格。存在未定价的可收费结果时停止该操作
继续发出请求，避免缺价格造成持续免费执行。

静态 TRON 收款采用永久唯一的币种/精确金额/地址指纹，取消或过期不释放金额
身份。使用 scripts/audit_tron_invoices.py dry-run 审核旧发票：重复或不明确来源
不能猜付款人。旧兑换码没有购买快照时必须通过 --redeem-snapshots 审核恢复，
不能把当前 Plan 当旧权益。具体清单及命令见旧权益迁移说明。

外部退款先持久化意图和来源隔离预留，释放锁后联系网关，确认后追加账务和
撤销来源。未知结果不得自动重发；使用 scripts/reconcile_payment_refunds.py
与网关证据确认成功或未发生。部分退款按累计货币金额计算累计微 Credits，
只退差额，最后一笔收尽余数；不可退其他来源或已经消费/偿债的资金。
禁用或缺凭据的网关先进行本地配置解析；确认未能构造客户端时不创建未知退款
意图或隔离 Credits，修复配置后可沿用原键重试。既有 pending/unknown 或成功
尝试优先使用其持久状态，不因配置修复重新发送退款；真正发送后的超时/取消
仍进入待核对并保留隔离预留。

已验签的入站支付及退款回调先进入持久化 inbox，再在独立保存点应用本地财务
效果。未知订单、金额、币种或通道的归一化失败保留 `wire_evidence` 和
`amount_cents=null`，状态为 `pending_reconciliation`；保存证据失败返回 503，
不能返回已完成。退款回调只核对并入账，不重新请求退款网关。通道事件与退款
引用分别由数据库唯一约束去重，乱序到达与重复回调没有第二次资金效果。

在 `/billing/admin/pricing` 查看分页待核对列表；重放保留未解决原因，核对需要
超级管理员、证据与必要的订单/金额/币种。原始 `received` 和人工 `reviewed`
审计不可变，原回调再次投递与原始证据比较，不能覆盖已审核证据。CLI 同样
只处理已保存证据，不发送外部付款或退款：

```bash
uv run python scripts/reconcile_payment_events.py --limit 100 --offset 0
uv run python scripts/reconcile_payment_events.py --apply --event-id 123 --actor-id 1 --order-id 456 --evidence "Verified provider receipt"
```

必要时提供 `--checkout-amount-cents/--checkout-currency` 或
`--refund-reference-id/--refund-amount-cents/--refund-currency`，金额为该币种最小
单位，不能猜测或填写未知值。返回 pending 时仍需补充证据。外部已实际发生的
退款可能涉及已消费来源，此时追加来源债务，其他来源不被直接撤销；随后冲正
原消费会先消除该来源债务，已用于偿债的资金恢复其实际来源。

启用支付通道前必须配置其验签密钥或公钥。NOWPayments IPN、Paddle Webhook
缺少或仅有空白密钥时拒绝处理财务事件；支付宝缺少公钥也不能接受支付确认。
金额解析失败只在验签成功后持久化为待核对，不能跳过验签补录任意请求。
NOWPayments 验签配置缺失返回 503 `retry_required`，补齐配置后可重新投递；
缺失/无效签名返回 400 `verification_failed`，不会保存未验签财务证据。
只有已经验签的非目标事件才能返回 200 `ignored`。
Paddle adjustment.created 的 pending_approval 与后续 approved/rejected 共用
原生 adjustment/transaction 身份；终态核对成功后关闭匹配的旧待核对投影，
追加 superseded 关联审计，保留原 received/reviewed 证据。金额、币种、订单
或来源矛盾继续待核对。锁竞争中被跳过的行由现有 payment-events replay 入口
在锁释放后关闭，不再次请求退款或要求猜测原始证据。

用户订单的删除操作改为隐藏列表记录，保留购买快照、支付日志、退款流水和
永久发票指纹。已隐藏订单仍参与回调、退款和审计；不能借清理页面释放发票身份。

若隐藏的待支付订单随后完成验签、金额核对及权益发放，同事务恢复其用户可见性
并追加支付审计；无效回调不恢复。部分退款后的新申请只请求尚未退款的金额，
审批前重新核对剩余金额，已存在的财务尝试保持原身份，未知结果不再次请求网关。

管理员发放接受 body `idempotency_key` 或 `Idempotency-Key` 请求头，两处同时
指定须相同。旧客户端只传 user_id/plan_id 仍可使用，服务器为该次独立请求生成
UUID 并返回（WebUI 返回同名响应头）；重试请沿用该键。旧无键请求无法证明是
重试还是第二次发放，不保证跨独立无键请求去重。退款键最多 160 字符。
套餐更新省略 concurrency_limit 保持原值，显式 null 或表单空白清除该套餐上限。

用户接口只读自身钱包/流水/Usage/执行，关联链接受现有权限检查。管理价格、
发放、调整需要超级管理员，记录操作者/原因/幂等键和同事务审计。账单不泄漏
Prompt、响应、凭据、内部价格快照或无权访问仓库；低余额阈值 0 禁用提醒，
跨阈值追加一次提醒，充值后重新启用下一次提醒。

## 验证边界

tests/test_billing_* 使用真实 SQL 持久化及独立连接并发，覆盖钱包重建、来源/
债务、定价精度、辅助调用/Fallback/流式未知、支付/兑换/迁移/退款、权限和
实际 WebUI/API。外部 AI/支付仅在边界模拟。浏览器使用隔离 SQLite 测试站点
验证余额、筛选、来源链接、阈值提交、提示和价格发布。
部署前还需在隔离 MySQL/PostgreSQL 运行迁移、触发器安装与锁等待验收，
SQLite 并发证据不能当生产数据库实测证据。

完整本地场景映射、命令和验证边界见 [验收证据](billing-2-acceptance.md)。
