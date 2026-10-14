# Billing 2.0：配置、迁移与对账

本实现不提供正式商业价格。首次部署保持 `billing_enabled=false`，先审核历史
权益、配置正式价格/汇率/倍率/Credits 价值和开账余额，再显式启用。
`payment_enabled` 只控制支付入口；钱包、账单、管理员发放和定价可独立使用。
关闭购买时，已有订单历史、用户退款申请及管理员退款审核/处理仍可访问；
侧栏保留相应入口，概览隐藏购买和兑换表单。权限及 CSRF 检查保持生效。
原有虚拟币账单的 GET `/billing/crypto-payment/{order_no}` 和
`/billing/crypto-status` 同样保留，TRON 通过原订单轮询确认转账并幂等履约。
这些入口校验订单所有者，不创建新订单或重新生成支付金额；已履约订单直接
返回完成，支付页只启动一份轮询，完成后停止轮询。单独停用 TRON 网关或
修改收款地址前，应先处理其未完成账单；关闭销售开关不要求停用到账查询。

管理端移除用户与 Telegram 使用同一账务保留服务，按受保护的用户 ID 操作。
有钱包（包括零余额）、流水、执行、订单、订阅或旧权益来源的用户会被停用，
余额、历史、来源及原外键保留；无相关记录的用户仍可删除。结果和确认提示
按操作者语言显示，审计记录区分停用保留与删除。

Bearer、query token 和 Cookie 的 API 鉴权每次都读取持久用户状态及当前角色。
保留用户被停用后，已有签名 Token 在新请求中返回 401；降权后的旧管理员
Token 使用当前角色，管理接口返回 403。缺失用户同样拒绝，数据库读取失败不
回退到 JWT 中的旧权限。Cookie 续期仍仅适用于 Cookie，原 MFA 门槛保持生效。
此检查发生在请求入场时，不声称撤销已经入场的请求或断开现有 SSE 连接。

退款无订单号时，先限定原支付通道，再按 ID 分批读取旧 Text 元数据，在
应用层解析合法 JSON 对象及精确支付来源；不向未校验文本应用 SQL JSON
运算或强制转换。每批 200 条是内存读取批次，不是订单总数限制；一次查找
固定起始最大 ID。损坏的无关历史行原样保留，选中行损坏或多订单匹配则进入
待核对，不猜测所有者。选中后锁行并复核来源，重复退款仍使用原退款唯一键。

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

Paddle 付款币种使用独立的协议能力目录，从 SDK 明确声明的 CurrencyCode
成员取得并与本项目已知最小货币单位取交集。不能仅构造 CurrencyCode 验证，
因为 SDK 允许未知字符串。已核对 [Paddle GET /currencies 官方目录](https://developer.paddle.com/api-reference/currencies/list-currencies/)，
当前 33 种包含 USD/CNY/EUR/JPY；USDT、AED 不属于此通道。下拉、Settings、
配置 API 与网关发起请求前统一拒绝不支持的值。AI 成本/结算及其他通道仍使用
自己的原目录；已保存异常配置或历史订单不自动换币或改金额，请明确重新选择。

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

API/WebUI 的 Issue 重新分析先完成既有权限、关闭状态与历史来源检查，再在
返回 task_id 前提交钱包/每用户并发入场及预留。余额不足返回 402
insufficient_credits，并发超限返回 429 concurrency_limit；失败不排队、不改原
分析状态或消费 Credits。管理员保留触发者与语言身份，付款者显式为平台。
成功携带同一服务端 operation 到 Worker，不重新预留；用户明确重发的新请求
获得新 operation。入场事务同时保存新的 PENDING IssueAnalysis（独立版本）及
`origin=issue_reanalysis` 的执行回执，提交前固定 task_id、付款者和分析行。
回执只保存来源标识及输入摘要，正文继续保存在既有分析表，不复制到对账回执。
Worker 先以数据库条件更新领取回执，验证令牌、付款者、来源及分析输入，再
复用已保存的分析行；重复交接不创建第二份结果或重新占用预留。等待执行容量
时取消、早期失败以及正常完成都会收敛载体、账务与回执。已知 runtime gate
未提交会终结无调用执行并释放预留；
交接结果未知返回 503，保留入场待恢复，不用缺 Usage 推断未发生。该不确定性
记录为交接确认状态，不伪造金融 Usage 或阻止后来实际启动的原 Worker。
GET `/api/v1/issues/tasks/{task_id}` 返回持久分析 ID、分析状态及交接状态；
只有原触发者及当前管理员可访问。POST 同一路径的 `/retry` 仅恢复明确尚未
进入交接窗口的 retryable 回执，沿用原 task_id/operation/付款者。结果未知或
已被 Worker 领取时拒绝再次发送，不能用没有 Usage 推断没有执行。没有拿到
响应中的 task_id 时，管理员仍可用既有回执 CLI 分页找到来源及载体。
恢复沿用现有过期执行/对账入口，不通过删除流水回滚；确认未开始可用
cancelled_unstarted 取消并释放预留，processing 必须先核对执行终态。令牌
轮换阻止迟到 Worker，原分析与财务证据保留。复用现有表，无需结构迁移；
应排空并统一升级旧 Issue Worker，旧版本不识别本轮交接令牌。

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

手动 Agent 的 queued 创建、候选提交及重试复用已有执行回执表，将任务、
限流消费、Billing admission 和 `origin=webui` 回执同事务保存。尚未开始
交接的回执为 retryable；后台注册失败仍可从任务页重试，沿用原 operation、
付款者和次数消费。进入交接窗口前以 SQL 条件更新领取一次，再复用 Agent
Worker 的回执/token 校验；结果未知保持 pending_reconciliation，重试返回
503 和按用户语言显示的提示，不产生新执行或重复消费。已失败执行的主动
重跑仍分配新 operation。旧 queued 入场无回执时，仅为已有执行生成待核对
回执，不推断未发送；原生 GitHub delivery 保留其原回执及重投递身份。
所有入口应统一升级并排空旧 handler，无回执的旧 Worker 不具备新 token 校验。

上述手动回执也可使用下方回执 dry-run/核对 CLI。确认未交接后才能应用
cancelled_unstarted；已有调用或活动 Worker 拒绝此操作。它释放原预留并
取消载体，轮换 token 阻止迟到的当前版本 Worker；核对不发起 AI 或队列请求。

`billing_charge_failed_calls=false` 的冻结执行策略将明确失败/取消调用由平台
承担。这些调用的原始 Usage、未知用量状态和 Provider 成本待核对原因继续
保留；续轮单独检查用户应付部分，所有应付 Usage 已冻结计价且没有未确认
请求时才能恢复。未写入失败回执的 started 请求、成功调用未知 Usage、
缺价，以及冻结失败收费策略开启的未知失败调用仍阻断，不能因当前全局
开关变化自动免除。续轮保持原 operation，累计结算差值不重复扣取已结算费用。

重排 HTTP 成功并不代表调用结果有效：缺失或畸形结果在计量作用域内判为
失败，实际 Usage 和 Provider 成本继续保留，结算遵守执行准入时冻结的
失败调用收费政策。严格模式还要求返回请求的完整数量；非严格模式继续
接受有效的部分结果和空结果，只有真正失败时才退回原文档。

管理员发放的订单插入使用数据库唯一键和保存点。并发相同发放键的失败方
回滚保存点后锁定读取已提交的订单，核对用户、套餐及操作者，返回同一财务
效果；不同意图仍拒绝，不将其他完整性错误当作幂等成功。无需额外数据库列。

PR/Issue 的已验签 delivery 使用 `webhook_execution_receipts` 记忆响应，以
feature+delivery 唯一键及签名正文指纹防止并发重投递再次入场/入队。已完成的
旧 Billing operation 同样去重；活动旧执行或外部副作用未知返回 503，不能凭
缺少 Usage 推断请求未发生。`/full-review` 先入场，再清理旧结果；
`/analyze` 同样在配额入场和 Issue 派发前领取 `issue_analysis` 回执。
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

### 自动 PR / Issue 的持久交接

自动审查、自动分析和评论命令复用现有回执 JSON，保存版本化的私有
`_dispatch` 载体：服务器确定的输入、付款者、operation 和任务标识。载体和
财务入场同事务提交后才创建进程内任务；HTTP 重放和对账 CLI 不返回私有输入。
无需新增表或列，不改写历史账本。创建 Worker 时清除 handler 的 delivery
ContextVar，避免子任务或增量继承另一投递的领取上下文。

`queued` 表示 Worker 尚未领取。应用启动恢复和同一已验签投递的重送可以按
原载体直接派发；不会再消耗次数、重复清理 GitHub 结果或生成新 operation。
每次派发使用数据库条件更新轮换领取令牌，旧协程和重复 Worker 在业务入口
被拒绝。启动恢复按固定高水位分批读取；单个提交失败保留原输入及安全错误
类型，继续处理其他载体。不会在 CLI 退出前创建随进程一起取消的临时 Worker。

`started` 表示 Worker 已持久领取，并不保证外部请求结果已知。缺少终态或
活动 Worker 所有权时重送返回 503、进入待核对，不自动重发。存在可信的
PR 增量队列也可证明交接；证明需核对仓库、PR、原付款者、operation、feature
和有效队列状态。后续增量清除旧审查的回执令牌，使用其自身队列 fence。
管理员审核确认的交接不被普通重放降级；重复同一确认不追加第二条审计。

关闭 PR、关闭/删除 Issue 时，先通知本机正在运行的任务，再持久取消已证明
尚未执行的载体，轮换令牌并释放预留，最后才确认生命周期事件。持久取消
失败保持重试错误；Issue 已启动的任务仍完成取消清理。账务过期恢复识别可信
未调用的 queued 载体，返回 `queued_dispatch_recovery`，保留其原入场等待派发，
避免恢复脚本先将有效工作取消。已开始或有调用证据的载体须按原对账入口处理。

升级应排空旧 handler/Worker 并统一版本。旧 accepted 回执没有持久载体、终态
或活动所有权时不能据此声称执行成功；保留待核对状态，由可信证据处理。
回滚保留载体、令牌和审计，不让不认识 fence 的旧 Worker 接管未完成任务。

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
多个一次性来源并存时，先消耗最早到期的有效来源，永久来源排在最后；
到期日相同时按来源 ID 排序。

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

Usage 核对必须在执行已有持久终态、且没有有效 queued/running Worker 所有权
后应用；身份核验、事件重放与财务写入仍在事务中完成。Worker 请求尚在返回
时不能先填入替代用量。失联请求的真实结果可能晚于核对到达：它追加带有
原 attempt、审核事件及已选 Usage 引用的成本证据，不覆盖已审核用量或再参与
用户结算。只有验证该引用关系后才排除迟到证据；新的实际请求仍单独收费。
不可变原记录和后续证据均保留。原始 Usage 统计可包含同一请求的多份观测，
不能将其行数或观测合计直接解释为已结算费用或独立请求数。

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
外部退款回调同样核对购买快照：混合 Credits、一次性次数、限流或并发权益
的部分退款进入待核对，不自动应用本地资金效果。仅含 Credits 的部分退款
及整单退款保持现有处理规则；混合权益的比例撤销须先确定审核政策。
禁用或缺凭据的网关先进行本地配置解析；确认未能构造客户端时不创建未知退款
意图或隔离 Credits，修复配置后可沿用原键重试。既有 pending/unknown 或成功
尝试优先使用其持久状态，不因配置修复重新发送退款；真正发送后的超时/取消
仍进入待核对并保留隔离预留。

核对外部成功退款必须提供该退款的原生身份，或使用尝试中已确认的原生身份；
不能只填支付交易号或不带身份确认成功。身份缺失、冲突或复用在审核/入账前
拒绝。追加退款引用让迟到验签回调识别同一退款，避免再次冲销。手动零现金
发放、兑换等纯本地冲正保持原规则。

支付宝退款的 `trade_no` 标识支付，`out_request_no` 区分部分退款请求。
系统在发送前保存实际 wire 请求号，将交易号和请求号组合为稳定退款身份；
超过协议长度的本地幂等键使用有界摘要。退款结果、验签通知和人工核对使用
同一映射。通知中的 `refund_fee` 是累计退款，不能直接作为单次退款再次入账。
历史仅保存交易号的尝试，必须用原意图及请求级确认凭据关联；无法关联的
规范化身份或累计金额保持待核对，不猜测、不自动重写已有流水。核对 CLI
展示原请求证据，支付宝可提供经核实的 `out_request_no`。
协议字段说明见 [支付宝退款 API](https://aipay.alipay.com/docs/vibe-pay/ai-web-app-payment-qianyi/api-list/alipay-trade-refund.html)。

部分退款先按已支付的网关币种、金额及累计已退款金额计算整数累计目标，
采用 half-even 舍入后收取差额。外部退款差额小于一个网关最小单位时返回
`refund_amount_unrepresentable`，在写退款意图、冻结 Credits、构造网关或
发送请求前拒绝；拒绝不占用幂等键，调整为可表示的金额后可再次提交。
API 保留字符串 detail，并以 X-Error-Code 返回该错误码；提示沿用用户语言。
手动零现金权益冲正仍走本地追加流水，不被此网关金额规则阻止。

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

启用支付通道前必须配置其验签密钥或公钥。NOWPayments IPN、Paddle 和 Stripe Webhook
缺少或仅有空白密钥时拒绝处理财务事件；支付宝缺少公钥也不能接受支付确认。
金额解析失败只在验签成功后持久化为待核对，不能跳过验签补录任意请求。
NOWPayments、Paddle、Stripe 验签配置缺失（含空白密钥）返回 503
`retry_required`，补齐配置后可重新投递同一事件；未验签时不保存财务 Inbox、
不改变订单或发放 Credits，验签后的重投递仍按原事件幂等处理。
缺失/无效签名返回 400 `verification_failed`，不会保存未验签财务证据。
只有已经验签的非目标事件才能返回 200 `ignored`。
Stripe SDK 的非验证运行错误保留可重试失败，不归类为未处理事件；验签日志
及 HTTP 错误不回显 secret、签名或原始回调正文。此修复无需数据库迁移或
修改账户余额；保留已收到的财务记录，不能以重投递为由清空旧事件。
Paddle adjustment.created 的 pending_approval 与后续 approved/rejected 共用
原生 adjustment/transaction 身份；终态核对成功后关闭匹配的旧待核对投影，
追加 superseded 关联审计，保留原 received/reviewed 证据。金额、币种、订单
或来源矛盾继续待核对。锁竞争中被跳过的行由现有 payment-events replay 入口
在锁释放后关闭，不再次请求退款或要求猜测原始证据。

Stripe 的 `refund.created` 中 `pending`/`requires_action` 同样按原生退款 ID
与 PaymentIntent 关联；`refund.updated`、`refund.failed`、`charge.refund.updated`
或完整 `charge.refunded` 中的终态证据核对成功后，关闭匹配的旧待核对回执。
支持终态先到及多笔原生退款的累计通知；失败/取消不会产生退款流水，已成功
退款与后续失败证据冲突时继续待核对。原始接收和人工审核证据保持不变，
仅追加 `superseded` 审计。协议见 [Stripe Refunds](https://docs.stripe.com/api/refunds)
及 [事件类型](https://docs.stripe.com/api/events/types)。

同一用户重复兑换多次可用兑换码时，以 MySQL 当前锁定读读取已有兑换记录；
并发唯一键冲突通过保存点撤回失败请求的新订单与已占用次数，再读取并返回
赢家订单，不重复发放 Credits，也不回滚调用方的其他事务变更。

升级后的兑换记录表为空时，先以用户、兑换码及通道读取历史已支付、已完成
或已退款订单，返回原订单并补登记关联。恢复购买快照不允许同一用户再次
领取；补登记不占用兑换次数、不改写历史快照或重新发放 Credits。

Stripe 退款可能先于付款完成回调到达；付款核实并保存 PaymentIntent 后自动
重放同来源的待核对退款。忙碌回执在付款提交后逐条处理，失败后付款回调
重投递也能继续恢复。金额、币种或归属冲突继续待核对，回调不重新发起网关
退款。Stripe/Paddle 外部全额退款与本地退款一样，从其余同套餐权益重算
订阅摘要；最后一个来源撤销后订阅过期，其他已购买周期保留。

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
