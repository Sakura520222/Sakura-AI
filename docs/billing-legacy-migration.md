# Billing 2.0：旧权益、支付与退款

本变更保留已购买权益的来源，并把请求次数与 Credits 收费分开。部署后，次数只控制请求准入；即使使用历史一次性次数，启用 Credits 收费后仍需要有可用 Credits。没有默认的“旧 1 次 = 1 Credit”兑换规则。

## 套餐与来源

- `Plan.credit_grant` 使用 Decimal，单位是人类可读的 Credits，最多六位小数。模型的默认值是零，不代表已确认任何正式价格或兑换率。
- `rate_limits` 是 JSON 对象。支持 `pr_daily/pr_weekly/pr_monthly`、`issue_daily/issue_weekly/issue_monthly`、`agent_daily/agent_weekly/agent_monthly` 和 `repo_scan_daily`；值为非负整数，表示来源提供的限流增量。`concurrency_limit` 是可选的正整数。
- 原有 `*_daily_add/*_weekly_add/*_monthly_add` 被折入同一来源的周期限流增量。`*_quota_bonus` 保存到 `billing_legacy_entitlements` 的独立剩余额度，先使用周期限额，周期限额不足时才消耗一次性剩余次数。
- 日/周/月重置仅重置计数，不再充值一次性剩余次数，也不充值 Credits。购买、兑换、后台发放都经由同一个 `_fulfill_order`。
- 创建订单和兑换码时保存购买快照。后来修改套餐、价格、币种或奖励，不改变已创建订单、已生成兑换码及已生效历史来源。
- 每个确认的付费订单只发放一次 Credits。一个订阅订单对应一个付费周期；同套餐续费的周期排在已有付费周期之后，不叠加同套餐限流。不同套餐来源可以叠加。Credits 在付费确认时发放，订阅到期只使该来源的限流和剩余旧次数失效，不让已买 Credits 过期。
- 已有支付通道创建的是单次订单，代码不把每日重置或登录当成订阅续费。任何新的续费必须拥有独立、已确认的付费订单。
- 人工发放的外部接口必须携带稳定的请求幂等键。`Order.grant_idempotency_key` 的数据库唯一约束保证重试返回原订单；同一键不能换用户、套餐或操作者。

`billing_legacy_entitlement_events` 为追加审计记录。消费、撤销和到期都保留原来源。原始用户基础配额不再被新订单修改；退款只撤销相应订单来源。

## 历史修复

历史一次性错误扩容不能从当前 `Plan` 推算。订单缺少原始快照时标记为 `needs_historical_evidence`，不会统一清零，也不会追收历史 AI Usage。

默认先执行只读检查：

```bash
uv run python scripts/migrate_legacy_billing.py --batch-size 100 --after-id 0
uv run python scripts/migrate_legacy_billing.py --manifest reviewed-billing-sources.json
```

清单需要逐笔人工核实的订单归属、当时购买快照、真实剩余次数、可证明的错误每日增量和证据描述。示例仅展示结构，不是正式兑换参数：

```json
[
  {
    "order_id": 123,
    "user_id": 456,
    "snapshot": {
      "id": 7,
      "name": "Archived purchased plan",
      "plan_type": "one_time",
      "pr_quota_bonus": 10,
      "credit_grant": "0"
    },
    "remaining": {"pr": 6},
    "inflated_daily": {"pr": 10},
    "evidence": "Archived invoice and reviewed entitlement consumption records"
  }
]
```

订阅来源清单必须额外提供可靠的 RFC 3339 `expires_at`。如经业务确认选择转换，显式填写 `conversion_credits`，并将被转换的旧剩余次数设为零；工具不会自选兑换率，也不会同时保留可消费旧次数与其转换后的收费余额。

历史快照带有非零 `*_daily_add/*_weekly_add/*_monthly_add` 时，清单还必须提供
`applied_periodic`，逐项注明经审计已写入用户基础额度的周期增量。键为
`pr_daily/pr_weekly/pr_monthly`、`issue_daily/issue_weekly/issue_monthly` 和
`agent_daily/agent_weekly/agent_monthly`；每个非零历史增量都要显式列出，值是
不超过原来源增量的非负整数。可靠证据证明从未应用时填写 `0`；无法确认则
停止该来源修复，不用当前套餐或基础额度反推。示例（仅表示审核清单格式）：

```json
{
  "applied_periodic": {
    "pr_daily": 5,
    "pr_weekly": 50,
    "pr_monthly": 100,
    "issue_daily": 0,
    "issue_weekly": 0,
    "issue_monthly": 0,
    "agent_daily": 0,
    "agent_weekly": 0,
    "agent_monthly": 0
  }
}
```

修复从对应基础日/周/月额度扣除该清单证明已应用的部分，并将原购买的周期
限流完整保留在来源中；每日错误 bonus 按 `inflated_daily` 单独扣除。dry-run
和正式审计均输出九个额度的 `base_before/base_after` 及 `applied_periodic`，
任一合计扣除超过现有基础额度时拒绝。来源到期后只撤销该来源的增量，基础
额度与其他有效来源保持独立；重复运行同一订单不会再次扣减。

旧兑换码缺少当时的购买快照时也不能使用今日套餐兜底。先恢复经审阅的旧兑换码来源：

```bash
uv run python scripts/migrate_legacy_billing.py --redeem-snapshots reviewed-code-snapshots.json
uv run python scripts/migrate_legacy_billing.py --redeem-snapshots reviewed-code-snapshots.json --apply --actor-id 1
```

清单每项需要 `code_id`、完整的 `snapshot` 和 `evidence`；快照至少包含当时的套餐 `id/name/plan_type/price_cents/currency/credit_grant`、已确认订阅时长及权益字段。恢复不会扣除兑换次数，也不会自动使用当前套餐价格。已生成兑换码的报价保持不可变；停用或修改当前套餐不能改变原码报价，后台不允许硬删除仍有兑换码的套餐。若要将旧码权益按新模式转换，应在审阅快照中明确记录已批准的 Credits 与原权益依据，不能从示例推算兑换率。

只有授权的、仍有效的超级管理员可执行修复：

```bash
uv run python scripts/migrate_legacy_billing.py --manifest reviewed-billing-sources.json --apply --actor-id 1 --batch-size 100 --after-id 0
```

每笔修复单独提交；输出包含前后值、来源键和下一批游标。失败条目保留为 `failed`，不阻止其他独立条目完成；用相同清单重跑时返回 `already_migrated`。不要把 `next_after_id` 越过失败条目当成失败已修复，应单独重跑失败来源。dry-run 不写订单快照、剩余额度、基础配额或审计表。

对已经拆分到来源表、尚未转换的旧购买权益，可以用独立的已审阅来源清单发放经批准的 Credits：

```bash
uv run python scripts/migrate_legacy_billing.py --source-conversions reviewed-source-conversions.json
uv run python scripts/migrate_legacy_billing.py --source-conversions reviewed-source-conversions.json --apply --actor-id 1
```

每项需要 `entitlement_id`、经业务批准的十进制字符串 `conversion_credits` 和 `evidence`。先查看 dry-run；不要填入测试中的 Credits 数值作为正式规则。仅有可信购买订单快照或可靠历史订阅快照的、尚未依法到期的来源可以转换。正式提交同时写入 `migration` Ledger、记录来源的转换交易、将被转换一次性剩余次数设为零并追加转换事件；周期限流仍由原来源控制。新收费启用后，尚未配置 Credits 的旧收费次数字段套餐不能继续出售。相同来源同一批准金额重跑不会重复充值，不同金额重用原键会报冲突。

在转换完成之前，保持 Credits 收费关闭，或使用项目的旧购买权益就绪校验阻止不安全切换。不得在启用后让有已购权益但尚无对应 Credits 的用户突然无法使用权益，也不得以无限免费旁路保护旧用户。

已存在订阅的 `applied_*` 是独立的历史来源快照。续费前会将可靠快照从基础配额中分离，保留至原有到期时间；无法还原历史错误无限使用后的精确剩余量时，保守保留原购一次性数量，不追收历史消费。缺失快照或与当前基础额度冲突时拒绝自动修改，要求审计；不使用当前套餐内容兜底。

## 支付验证与资金精度

支付确认必须携带可信通道产生的整数最小货币单位金额与币种，并与订单的实际 checkout 快照一致。金额缺失或币种不匹配不再跳过验证。兑换与人工发放不需要伪造外部支付证据。

跨币种结算需要配置经确认的十进制 `exchange_rate_<FROM>_<TO>`；不使用内置参考汇率、不以 1:1 假装转换成功。转换采用 Decimal 和 `ROUND_HALF_EVEN`，实际 checkout 金额/币种随订单保存，退款沿用该快照。

TRON 金额使用 USDT 的整数原子单位和 Decimal 精确比较。只查询已确认转账，核对实际 USDT 合约、收款地址和订单创建时间；不再以 ±若干原子单位的误差匹配其他订单。`payment_receipts` 的通道/交易唯一约束防止一笔链上交易或支付事件为多个订单发放权益。

每笔新 TRON 发票还持久保存钱包、币种及精确金额的 `invoice_identity` 唯一指纹，永不因取消、到期或付款释放。哈希微量后缀发生碰撞时，在发票公布前生成另一个订单标识；六位 USDT 原子精度在一个 cent 范围内仅有 10,000 个候选，范围耗尽时明确拒绝该通道的新发票，不复用历史金额。切换前检查旧发票：

```bash
uv run python scripts/audit_tron_invoices.py
uv run python scripts/audit_tron_invoices.py --apply --actor-id 1
```

只恢复唯一且可可靠解析的旧通道快照指纹；同钱包同金额的历史碰撞全部标记为 `ambiguous`，不猜测付款人，自动确认保持阻断并要求通道对账。缺失旧指纹的订单不能直接自动发放 Credits。


## 退款、未知窗口与恢复

余额与 Credits 来源由 Wallet/Ledger 负责，退款不能撤销另一笔购买的 Credits。来源已消费或被其他执行预留时，先拒绝本次退款，保留原账务，管理员可依据政策另行审核。

全额退款撤销相应订单来源。部分退款默认由 `payment_partial_refund_policy=reject` 关闭；确认政策后可设为 `proportional_unused_credits`，仅允许纯 Credits 套餐。部分退款需要独立、稳定的审核请求键，按累计退款金额比例计算累计应收回微 Credits，向下取整，再减去已收回金额；最终全额退款补齐剩余微 Credits。包含旧次数、限流增量或并发权益的套餐不做未经确认的比例撤销。

外部退款采用明确的阶段：

1. 事务内持久化 `payment_refund_attempts` 的请求和 Credits 来源预留，并提交。
2. 在没有订单/钱包行锁的窗口请求通道退款。支持的通道传递稳定退款幂等键（Stripe、Alipay）。
3. 确認通道成功后，先持久化 `upstream_succeeded` 和通道退款编号并提交。
4. 独立短事务释放来源预留、追加账务冲正、更新累计退款与来源状态，并追加审计事件。

如果请求已发出而结果未知，记录保持 `pending` 或 `unknown`，Credits 预留保持隔离。重试不会再次发送外部退款。崩溃后若已记录 `upstream_succeeded`，直接恢复本地结算，不再请求通道。这不是跨外部 API 的无条件 exactly-once；不支持通用退款查询的通道需要管理员核对。

检查待恢复记录：

```bash
uv run python scripts/reconcile_payment_refunds.py
```

在通道后台确认真实结果后，用审计证据恢复：

```bash
uv run python scripts/reconcile_payment_refunds.py --apply --attempt-id 123 --actor-id 1 --outcome refunded --provider-refund-id provider-refund-id --evidence "Verified provider refund receipt"
uv run python scripts/reconcile_payment_refunds.py --apply --attempt-id 124 --actor-id 1 --outcome not_refunded --evidence "Verified provider has no refund for this request"
```

已确认退款将恢复本地冲正；确认没有退款只释放原有 Credits 预留，不生成新的余额。不要把尚未查到退款当成已确认未退款，不要删除未知请求后重试。证据不得包含令牌、私钥或完整付款凭证敏感内容。

## 切换与回滚

先完成加法 schema 迁移、审计旧来源、确认正式价格/汇率/Credits 兑换和失败收费政策，再启用 Credits 收费。切换期间单一版本负责发放与收费；旧版本会继续把一次性奖励加到每日额度，因此不能让旧、新 Worker 同时处理发放、退款或配额消费。

回滚可以关闭新业务收费入口并保留未结算/未知请求待恢复，但必须保留全部 Wallet、Ledger、来源及退款审计数据。不要用删除账务流水、清空钱包、重新启用旧错误发放逻辑来回滚。

有账务、购买订单或权益来源的账号移除采用停用，保留原用户 ID 及全部财务关联，避免删除用户导致账务不可重建。
