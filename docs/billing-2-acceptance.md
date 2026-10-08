# Issue #621：本地实现与验收证据

验证日期：2026-10-08。当前工作区 `develop`，基线
`5e4a1032fcc6eaca1d66a4a55f5925246083b5ca`。补丁未提交、推送或部署。
没有修改生产余额、发起真实 AI 付费请求或真实付款/退款。

## 六阶段实现

| 阶段 | 实现位置 | 本地证据 | 未完成的环境验收 |
| --- | --- | --- | --- |
| 0：一次性权益 | payment_service、legacy_entitlement_service、legacy_billing_migration_service；独立来源、剩余次数与追加事件 | test_billing_entitlements：购买/兑换/管理员发放、跨周期重置、消费顺序、多订阅、退款/到期、审核迁移 | 历史生产数据未执行迁移，属于本任务授权边界 |
| 1：Usage Attribution | billing_context、ai_usage_service、实际 Provider/压缩/Embedding/Rerank 出口、四类 Worker、队列持久载体 | test_billing_usage_attribution、test_billing_auxiliary_errors、test_billing_worker_entrypoints：并发身份、Fallback、恢复、新执行、HTTP失败、流式缺失 | 无真实付费 Provider 调用，接口边界模拟 |
| 2：Pricing | billing_pricing、不可变 BillingPriceProfile/BillingUsageCharge；Decimal 和整数比例 | test_billing_pricing、test_billing_settlement：缓存/reasoning语义、未知字段、原生单位、版本冻结、累计舍入、FX尾数 | 正式价格/汇率/markup/Credits价值待运营配置，收费默认关闭 |
| 3：Wallet/Ledger | billing_models、billing_service、billing_schema、billing_reconciliation_service、billing_maintenance | test_billing_wallet/concurrency/settlement/reconciliation：真实SQL、独立连接竞争、回滚、来源/债务、预留、冲正和崩溃恢复 | 隔离MySQL/PostgreSQL的行锁/触发器权限未实测 |
| 4：套餐及迁移 | 购买/兑换快照、统一来源发放、支付/退款inbox、通道原生单位、审计/恢复CLI | test_billing_entitlements、test_billing_payment_events、网关测试：重复/并发、乱序、未知窗口、部分/外部退款、源转换、永久发票指纹 | 原生MySQL/PostgreSQL升级验收未运行；未知历史和转换金额须审核 |
| 5：账单/管理/提醒 | billing API、现有Jinja界面、credits/admin_pricing、双语资源 | test_billing_credits_api：真实HTTP/SQL、鉴权、CSRF、分页/筛选、脱敏、阈值去重、用户隐藏订单 | 浏览器交互见下；无生产部署验证 |

## 需求到自动化场景

所有 `tests/test_billing_*.py` 使用实际服务；涉及持久化时执行真实 SQLAlchemy
事务。外部 AI/支付、鉴权身份与业务外部依赖仅在接口边界模拟，不 mock 钱包结果。

| 验收场景 | 测试入口及结果判据 |
| --- | --- |
| 购买后跨日/周/月重置 | test_billing_entitlements：daily上限保持基础值、一次性remaining只消耗不生成 |
| 支付/兑换/周期重复与并发 | test_billing_entitlements、test_billing_payment_events：唯一订单来源、grant/receipt/refund引用、一次财务效果 |
| 多用户PR/Issue/Agent | test_billing_usage_attribution、test_billing_worker_entrypoints：异步上下文隔离、队列付款人不继承上次执行 |
| 主调用/压缩/RAG/Fallback | test_billing_usage_attribution：顶层feature保持、actual_call_id每请求不同、实际模型价格版本 |
| 缓存/reasoning协议差别 | test_billing_pricing、test_billing_usage_attribution：包含与独立维度分别计算，null与0区别 |
| 无Usage/无价格/流式取消/超时 | test_billing_usage_attribution、test_billing_reconciliation、test_billing_auxiliary_errors：unknown/pending，不伪造零；已报告失败成本保留 |
| 恢复与用户重跑 | test_billing_settlement、test_billing_worker_entrypoints：恢复沿用原ID累计delta；人工retry新ID及可信付款人 |
| 并发消费与退款 | test_billing_concurrency：独立连接条件更新、原来源保留、余额可重建 |
| 写入中断/重复/崩溃 | test_billing_wallet、test_billing_usage_attribution、test_billing_reconciliation、test_billing_payment_events：原子回滚、started未知证据、过期执行回收、本地重放无外部请求 |
| 已有失败Usage/重复退款 | test_billing_auxiliary_errors、test_billing_wallet、test_billing_entitlements：政策快照、不可超退、来源债务/已偿债资金恢复 |
| 改价和微小多调用 | test_billing_pricing、test_billing_settlement：历史快照可复算、累计进位、整数比例及货币half-even精度 |
| 旧用户/多订阅/异常历史 | test_billing_entitlements：无证据拒绝猜测、dry-run零写入、applied_periodic来源拆分、到期无遗留、显式conversion |
| 用户账单/管理权限 | test_billing_credits_api：自身账单、隐藏未授权repo链接、super_admin、CSRF及审计 |
| Credits不足/次数限制/提醒 | test_billing_wallet、test_billing_configuration、test_billing_credits_api、test_billing_entitlements：不同错误代码、准入不误耗次数、跨阈值去重 |
| 完整购买到费用链 | test_billing_end_to_end：管理套餐→兑换→Credits→可信业务Worker→实际UnifiedAIClient/Usage→定价→账单；失败/来源冲正分支 |

## 浏览器证据

隔离SQLite站点验证过用户余额、消费筛选与业务链接、低余额阈值提交及提示、
管理员价格发布。另在管理员待核对页面实际提交重放：缺证据仍显示pending；
提交经过审核的订单映射和证据后事件从待核对列表移除，其他未知事件保留。
浏览器动作只针对临时测试站点，该站点已停止。截图步骤被中断，没有交付截图。

## 可重复命令

使用项目现有 `.venv`（Python 3.14.7）。只为测试使用 `/tmp` uv缓存，未改变
requirements.txt、pyproject.toml或uv.lock。

```bash
UV_CACHE_DIR=/tmp/sakura-billing-uv-cache uv run --no-sync python -m pytest -q -rs
UV_CACHE_DIR=/tmp/sakura-billing-uv-cache uv run --no-sync python run_ruff.py --check
git diff --check
UV_CACHE_DIR=/tmp/sakura-billing-uv-cache uv run --no-sync python -m compileall -q backend scripts
```

原生异步 SQLite schema/CLI smoke 需要可选测试驱动，不安装到项目环境：

```bash
UV_CACHE_DIR=/tmp/sakura-billing-uv-cache uv pip install --python .venv/bin/python --target /tmp/sakura-621-test-deps aiosqlite
PYTHONPATH=/tmp/sakura-621-test-deps UV_CACHE_DIR=/tmp/sakura-billing-uv-cache uv run --no-sync python scripts/verify_billing_sqlite.py
```

该脚本只创建可自动清理的临时数据库，执行 fresh/repeated 建表和迁移、默认配置
补插、所有财务不可变触发器、唯一约束、真实 AsyncSession 原子回滚、钱包对账，
再执行实际维护/历史迁移/支付inbox CLI 的 dry-run。所有数据库连接均使用
脚本生成的临时URL，CLI的部署URL读取入口被替换。

## 上一轮运行结果

以下结果来自本轮 MySQL 1419 启动修复之前，保留作为上一轮 Billing 2.0 实现的
验证记录，不能用来证明本轮新增错误处理或 MySQL 预安装流程已经通过。

| 命令/检查 | 结果 |
| --- | --- |
| `python -m pytest -q -rs` | 4858 passed，17 skipped，88.04s；最终0失败 |
| Billing核心、迁移及贯通链聚合回归 | 103 passed |
| 最终辅助失败/队列/扫描/精度/时间白名单回归 | 31 passed |
| 网关、回调、inbox、管理API/WebUI回归 | 137 passed |
| `python run_ruff.py --check` | All checks passed |
| `git diff --check` | 通过；仅既有CRLF文件换行提示 |
| `python -m compileall -q backend scripts` | 通过 |
| `scripts/verify_billing_sqlite.py` | fresh/repeat异步迁移、触发器、唯一约束、原子回滚和CLI dry-run通过 |
| 五个维护/迁移/恢复CLI `--help` | 全部退出0，未连接部署数据库 |
| 单独补跑 `tests/test_legacy_placeholder_atomicity.py`（临时aiosqlite） | 3 passed，弥补标准套件的可选驱动skip |

17个skip来自既有条件：Docker隔离12项、systemd/root3项、跨属主root权限1项、
缺aiosqlite1项。后者已单独补跑通过，其他16项没有改测试或放宽gate。没有
新增Billing skip。新增缺陷回归先RED再GREEN；实现过程中的旧mock契约及时间
白名单位置失败已经修复，没有以删测试或放宽断言取得通过。未建立修改前的
全套HEAD基线，因此不把中途失败声称为“既有失败”。

## 本轮 MySQL 1419 启动诊断

用户提供的启动日志显示：`ensure_billing_schema` 在创建
`billing_transactions_no_update` 时收到 MySQL 1419，导致应用启动失败。
本轮只读核查观察到服务器为 MySQL 8.4.11，`log_bin=1`、
`log_bin_trust_function_creators=0`，应用账号没有 `SUPER` 或全局 `ALL` 权限，
可见的账本保护触发器为 0 / 24。这是实际观察到的原生数据库权限阻塞；没有
据此宣称 MySQL 迁移、锁等待或账本不可变性验收通过。

修复增加明确的 `BillingSchemaPermissionError`
（`billing_schema_privilege_required`），保持缺少数据库保护时阻止启动；
已有触发器须验证表、事件、BEFORE / ROW 属性及拒绝主体，不能只检查名称。
新增离线 `scripts/export_billing_mysql_guards.py` 和
[管理员预安装 SQL](billing-mysql-guards.sql)，提供 24 条
`CREATE TRIGGER IF NOT EXISTS`，不连接数据库、不提权、不删除触发器或修改
全局变量。此语法的版本要求由
[MySQL 8.0.29 发布说明](https://dev.mysql.com/doc/relnotes/mysql/8.0/en/news-8-0-29.html)
核实；部署步骤、元数据权限及持久 DEFINER 要求见
[MySQL 预安装说明](billing-2.md#mysql-账本触发器预安装)。

本轮未执行运行数据库触发器安装、授权或全局参数变更，未验证部署应用恢复
启动。DBA 预安装后还须重新启动并检查结果；原生 MySQL / PostgreSQL 的迁移、
锁和不可变性验收仍待完成。

本轮实际验证：

| 检查 | 结果 |
| --- | --- |
| 新MySQL权限/错误定义/离线导出回归 | 初始9 failed、2 passed；修复后11 passed |
| MySQL回归与真实SQLite账本聚合 | 18 passed |
| Setup、数据库迁移、模型公开接口回归 | 20 passed |
| 完整 `python -m pytest -q -rs` | 4869 passed、17既有条件skip、87.61s、0失败 |
| `python run_ruff.py --check`、`git diff --check`、相关文件compileall | 通过 |
| `scripts/verify_billing_sqlite.py` | 重复异步迁移、不可变触发器、事务和CLI dry-run通过 |
| 管理员SQL与离线导出器一致性 | 24个固定guard完全匹配，无连接/提权/全局变量修改 |

MySQL错误路径用原始OperationalError模拟驱动边界；真实服务器仅进行版本、
权限和触发器元数据只读查询。没有把模拟权限测试或SQLite通过当成管理员安装
成功。另一次回归命令曾引用不存在的测试文件而未运行测试，已更正文件名并
补跑上述20项，未遗漏该组检查。

## 授权后的 MySQL 保护安装

用户随后明确授权安装。2026-10-08，在当前配置的
`127.0.0.1:3306 / sakura_ai` 安装上述24个保护触发器。安装前比较应用连接与
管理员连接的server UUID及数据库，确认同一目标且12张表均已存在；SQL与离线
导出器逐字一致，SHA256为
`a8f69c4965ac8049f9d0ca2585cf30545d2b8db44100ef6e20b2574ee1668ee9`。
使用既有MySQL容器内管理员凭据，密码未进入命令参数、工具输出或仓库。

安装后的实际应用账号只读验证：

- 24 / 24触发器可见，表、UPDATE/DELETE、BEFORE/ROW及拒绝主体全部通过。
- 实际 `ensure_billing_schema` 返回 `False`，无需新增DDL；检查期间拦截任何
  非SELECT/SHOW/DESCRIBE语句，未触发拦截。
- DEFINER为持久的 `root@localhost`；应用账号未获得SUPER或全局ALL权限。
- `log_bin=1`、`log_bin_trust_function_creators=0`，与安装前相同。
- 执行范围仅为保护触发器DDL，没有余额DML、授权或全局参数变更。

这验证并解除了已观察到的1419触发器权限阻塞，不等于完整应用生命周期或
多Worker计费验收。未启动业务后台任务；原生MySQL的完整迁移/并发锁验证及
PostgreSQL验收仍待完成。

## 配置保存与定价 WebUI 修复

2026-10-08 用户反馈保存失败没有字段原因、提示语言不跟随个人设置，以及模型
与原套餐价格入口缺失。本轮只修改工作区，用户原有暂存补丁的二进制diff SHA256
在修复前后均为 `bdb67b702618c2c22565757ba91e02ea900bd15c307b9960be18bd96a5576ae7`。

真实MySQL只读复现发现 `missing_price`：价格表为空，需要11组实际模型/调用
报价；500 Credits预留和3600秒时限有效。没有自动填入或发布生产商业参数。

- 配置/系统保存返回结构化字段、原因、明细、修复链接，保持AJAX/旧表单契约。
- 个人语言优先于全局默认，批量保存沿用同一语言，Bearer API使用可信用户ID。
- 保留失败输入、聚焦错误、展开错误分区；自定义标签及冲突错误映射正确行。
- 模型定价与套餐价格入口对超级管理员常显，关闭支付也能先完成配置。
- 模型表单分Provider原始价格和用户换算，精确支持每Token/千Token/百万Token，
  保留高级JSON与版本载入，只发布新版本。
- 套餐配置守卫仅放行明确管理路径及方法；权限/CSRF仍有效，购买、订单、退款、
  兑换码生成在支付关闭时继续拒绝，配置价格不产生财务流水。

隔离SQLite浏览器实际验证了中文及英文保存失败、报价明细、收费开关聚焦与
保留值、模型表单发布成功、模型原始价格与换算控件、关闭支付后的套餐页面与
创建窗口。临时预览与运行数据库隔离，没有调用AI或真实支付接口。

最终 `uv run --no-sync python -m pytest -q -rs`：4971 passed、17既有条件skip，
101.55s，0失败；`run_ruff.py --check`、`git diff --check`通过。112个新增翻译键
在中英文中完全一致。新测试与真实SQL场景见 `test_config_save_feedback`、
`test_section_config_validation_feedback`、`test_system_config_feedback`、
`test_webui_config_save_feedback`、`test_billing_pricing_editor`、
`test_billing_pricing_navigation` 和 `test_billing_admin_plan_guard`。

## 已配置账号定价与模型发现

2026-10-08 用户要求定价直接使用已配置 AI 账号，模型列表自动获取。本轮新
补丁保持未暂存；用户原有暂存区二进制 diff SHA256 前后均为
`7bca9d1c16e42e6e04de1d7630d5f8e1ba273b6d49adfa5f5b24f870d16aeca9`。

| 行为 | 实现与可重复证据 |
| --- | --- |
| 已配置账号选择、模型自动发现与刷新 | billing_account_pricing_service、admin_pricing、billing_pricing_accounts.js；test_billing_configured_accounts/test_billing_pricing_account_picker：保存模型、自动发现、失败保留、跨账号响应取消、手动绕过缓存 |
| Provider/账号身份可信且无凭据泄漏 | WebUI 与 API 从账号解析 Provider；真实 HTTP/SQL 测试覆盖伪造 Provider、无账号、停用账号、非超级管理员、脱敏及账号审计 |
| 相同 Provider/模型的账号独立计价 | BillingPriceProfile.account_id/scope_key；test_billing_account_pricing_core：严格范围、无旧通价兜底、冻结历史与增量迁移 |
| 主调用、压缩、流式与 Fallback 实际账号 | ProviderUsageMeter 持久化实际 candidate.account_id；test_billing_account_usage：辅助/失败/Fallback、并发归属、平台承担与独立 Embedding |
| 人工核对不串账号 | billing_reconciliation_service；test_billing_account_reconciliation：跨账号报价拒绝、追加 Usage、幂等重放、旧未绑定记录 |
| 独立辅助模型继续可配置 | source_scope embedding/rerank 从实际配置解析并锁定模型/调用类型；真实 HTTP/SQL 与浏览器验证不借用 AI 账号 |

实际运行结果：

- `uv run --no-sync python -m pytest -q -rs`：5004 passed、17 既有条件 skip，
  94.15s，0 失败。skip 为 Docker 12、root sticky 1、可选 aiosqlite 1、
  updater systemd 3，本轮没有新增 skip。
- 六个账号定价/计量/核对/界面测试文件：49 passed，4.26s。
- `uv run --no-sync python run_ruff.py --check`、`git diff --check` 通过。
- `scripts/verify_billing_sqlite.py` 使用临时安装的 aiosqlite：新建/重复异步
  schema、全部不可变触发器、事务回滚、钱包对账和 CLI dry-run 通过。
- 25 个新增翻译键中英文一致。发现异常不向用户回显上游错误或凭据。

隔离 SQLite 浏览器验证选择 A/B 账号后自动发现各自模型、切换清除旧账号模型、
新发现模型发布报价、历史载入恢复账号与模型、辅助模型来源锁定、手动刷新。
账号及价格全为临时测试数据，上游边界模拟 list_models；没有请求真实 AI
Completion、支付或退款。临时服务和页面已关闭。

账号身份列/索引迁移已在隔离 SQLite 验证，未在运行 MySQL/PostgreSQL 执行，
也未验证真实账号模型发现。升级前保持收费关闭；重启完成增量迁移，为实际
账号发布明确报价后再启用。旧 Provider 报价保留原身份/版本，不自动猜测账号
或用于新账号调用，恢复后新请求同样需要该账号的报价。

## 币种选择与统一模型调用报价

2026-10-08 用户要求币种输入改为选择、普通与流式模型调用合并。所有新增改动
保持未暂存；用户已暂存区的二进制 diff SHA256 仍为
`032dc258076cd333f787793286fb64f77793ab9c5bf0b84907cc1d9bef041936`。

| 需求 | 实现与自动化证据 |
| --- | --- |
| 金额币种统一选择 | currency_units.supported_currencies/normalize_currency 与 currency_select；模型成本/结算、套餐创建/编辑、支付/退款核对，以及默认支付/Stripe/Paddle/支付宝配置共用目录 |
| 防止表单/JSON/API 绕过 | test_billing_currency_selectors、test_billing_plan_currency_selection：拒绝 ZZZ，支持 ISO/USDT，套餐失败不插入或修改旧金额，大小写规范化 |
| 普通与流式共用一份报价 | billing_price_identity、billing_service/configuration/reconciliation；test_billing_unified_chat_tariff：同操作计量、实际 call_kind 保留、共用 chat 报价、旧 API 别名及账号隔离 |
| 历史报价与账单保护 | 同上：旧固定流式版本仍用原价、旧仅流式报价不自动用于新请求、旧未知币种进入 pending_pricing，不丢 Usage、不伪零、不改历史金额 |
| USDT 原生列长度与重复迁移 | billing_models 的两列 String(10)、billing_schema 幂等扩宽；test_billing_currency_schema：MySQL/PostgreSQL SQL 编译/反射定义、冲突预检查、重复运行、SQLite 数据和触发器保留 |
| 旧合法/未知币种回显 | test_billing_currency_history：合法小写只规范化显示；未知值在首空值占位选项显示并要求重新选择；部分编辑省略币种保留旧值和金额 |

最终 `uv run --no-sync python -m pytest -q -rs`：5058 passed、17 既有环境条件
skip，100.53s，0 失败；没有新增 skip。跳过仍为 Docker 12、root sticky 1、
可选 aiosqlite 1、updater systemd 3。
五个新增专项文件共 54 passed，4.43s。Ruff、`git diff --check` 通过，新增两个
翻译键中英文一致。SQLite 异步 schema 重跑、全部不可变保护、事务回滚、钱包
审计及 CLI dry-run 通过。原生 MySQL/PostgreSQL 扩宽只验证编译及模拟分支，
没有连接或修改运行库；启动迁移需要对应 ALTER 权限。

浏览器使用两轮完全隔离的 SQLite 实例验证：统一模型调用菜单只有 chat，成本/
结算为下拉、USDT 报价发布及载入、JPY 套餐创建/编辑保持最小单位、全局四币种
字段为 select。历史 usd 回显 USD 而原行仍 usd；未知 ZZZ 显示原字串，原生
validity.valueMissing=true/valid=false，点击更新不提交，选择支持币种后恢复
有效性，取消后原行和金额保持不变。第一轮追加空值禁用选项的原生校验缺陷被
实际浏览器发现并修正为首占位选项，不以 Node 模拟结果代替浏览器有效性。
预览服务和页面已关闭，没有真实模型调用、付款或退款。

此次金额币种目录不替换 NOWPayments 的 usdttrc20 等收款资产/网络代码；该
字段继续使用原协议配置。本轮没有新增虚构的网关网络支持名单。

## 服务容量与每用户执行上限

2026-10-08 用户要求修复 Agent 并发未接入、PR/Issue 并发仅限单进程，以及页面
两层限制混淆。本轮新补丁仍未暂存；原暂存区二进制 diff SHA256 前后均为
`297c9244a2c263022cc34ad19ddd7839e12d31cb67884fca3a5b5538ef8692bf`。

| 需求 | 实现与验证 |
| --- | --- |
| Agent 服务并发生效 | worker 的初始/恢复/自动审查续轮/人类 follow-up 均接入 service_execution_slot；test_worker_service_capacity 18 项覆盖入口、等待取消、故障收敛与第一次财务终态 |
| PR/Issue 多实例共享 | ServiceExecutionGate + Lease 短事务写锁；test_service_execution_capacity：五个 spawn 进程共用 SQLite 两名额，取消/异常释放、动态降容量、嵌套 Task 隔离、过期 fencing |
| 取消和配置更新正确 | test_service_execution_lifecycle 12 项：重复取消覆盖整段 SQL cleanup、已提交未交付结果回收、AppConfig 新 timing 跨实例生效、旧租约保持期限及独立心跳 |
| 两层文案与口径明确 | PR/Issue/Agent 为服务执行并发，套餐为每用户业务执行上限；test_service_concurrency_configuration 21 项：范围校验、双语及实际 SQL outcome=null 统计、跨功能与终态反例 |
| 排队与恢复不绕用户上限 | admit_billing_operation 在等待前登记；resume_operation 重检查并发、不重算日配额；test_billing_execution_admission 6 项，包括拒绝恢复后任务仍可重试且旧财务结果保持 |
| 活执行不被恢复误终结 | 独立 ServiceExecutionOwnership、恢复前 current read、批次排除活 owner、退出和恢复的交接 TTL；test_service_billing_liveness 6 项实际 SQL 与 MySQL FOR UPDATE 编译证据 |
| 两层限制贯通 | test_service_capacity_billing_integration：同用户一运行/一排队占满套餐名额，第三功能拒绝，另一用户 PR 可执行；取消释放，未调用 AI 不产生消费 |

最终本轮全部新增专项 77 passed，7.25s。完整
`uv run --no-sync python -m pytest -q -rs`：5135 passed、17 既有环境条件 skip，
103.36s，0 失败；没有新增 skip。Ruff、`git diff --check` 通过，7 个新增翻译
键中英文一致。中途环境切换导致一个新增测试文件末尾出现 NUL 填充，确认有效
正文和 13 个完整测试均保留后只去掉无效填充，重新运行全部检查；没有删除或
放宽测试断言。

额外使用临时 aiosqlite 0.22.1 执行真实 AsyncSession：8 个任务共享 2 个名额、
真实异步 SQL 续约、活账务恢复跳过、资源清空均通过。异步 SQLite 新建/重复
schema、全部不可变保护、事务回滚、钱包审计与 CLI dry-run 同样通过。临时
测试驱动没有修改项目依赖清单。默认环境跳过的可选 SQLite 用例另行以临时
驱动运行 `tests/test_legacy_placeholder_atomicity.py`：3 passed，0.60s。
原生 MySQL/PostgreSQL 协调逻辑未执行生产实测，
不能把 SQLite 多进程结果称为生产数据库验收。

隔离 SQLite 浏览器检查了三类服务配置的共享范围说明，以及套餐上限创建/
编辑保存与回显。临时服务和页面已关闭，没有真实模型/支付/退款。
部署需停止或排空旧 Worker，再统一切换并创建三张运行时表；各节点时钟同步。
外部 API 已发出或进程暂停时存在未知在途窗口，租约不承诺上游无瞬时重叠。

## 用户套餐卡片文案纠正

2026-10-08 用户反馈未填写描述却出现系统内部说明。根因是上一轮在
billing/index.html 的并发权益列表中复用了管理员字段帮助
`billing.concurrency_limit_help`，与数据库 plan.description 无关。
本轮去掉该帮助，用户卡片使用单独的双语「同时任务上限」产品文案；后台
创建/编辑仍保留完整帮助，实际填写的描述仍显示，空描述不自动补内容。
没有修改任何运行数据、套餐价格、限额或计费规则。

旧暂存模板通过独立测试进程的 Jinja loader 重现 8 项断言失败，没有把工作区
模板回滚或改动暂存区。当前真实 HTTP/SQL 回归覆盖中文/英文、用户/超级管理员、
空/已填写描述及管理端说明保留：8 项通过，相关回归 57 passed。
完整 `uv run --no-sync python -m pytest -q -rs`：5143 passed、17 既有条件 skip，
115.80s；Ruff、`git diff --check` 通过。浏览器隔离 SQLite 验证了 Plus 无描述
卡片不含内部文字、另一个套餐的自定义描述仍显示，预览和页面已关闭。

用户原暂存区二进制 diff SHA256 保持
`95b35635a351bffc2aac5e5f890cdb57920f4e2fa0ed8e6a84723e9642db2617`；
新增修复未暂存、未提交、未推送。无需数据迁移，重启应用加载新双语资源。

## PR #660 两轮审查修复（2026-10-08）

工作基线为 `7875ed03552a1257d1d20247330c43ca430f175b`，开始时工作区干净。
外部审查正文用于定位问题，邮件回复/反应/退订链接未作为操作授权。以下均为
本地代码与隔离测试；没有暂存、提交、推送、部署或改动实际用户余额。

| 审查项 | 实现 | 可重复证据 |
| --- | --- | --- |
| 1 开启后部分 AI 路由写入绕过定价 | billing_configuration_service + account_store + role_config：有效开关、拟保存快照、数据库配置行锁 | test_review_billing_route_changes：辅助模型、压缩、账号变更、角色 fallback、先有价格后保存、旧 ORM identity |
| 2 旧管理员发放缺键 422 | v1/WebUI billing：可选 body/头、生成返回 UUID、显式冲突拒绝 | test_review_billing_api_compat：独立两次发放和返回键重放，真实 Ledger/余额 |
| 3 已结束 delivery 重投再次入队 | webhook_execution_service + webhook_execution_models：feature/delivery 唯一回执、正文指纹、历史终态检查 | test_webhook_billing_admission：opened/reopened、并发、skip、不同正文/feature、活动旧执行 |
| 4 自动管理员被当付款者 | webhook + Agent candidate：平台 payer，保留可信 trigger_user_id 和语言身份 | test_webhook_billing_admission：管理员零钱包不拒绝、不扣个人钱包；Agent carrier 与付款者一致 |
| 5 无法清除套餐并发上限 | PaymentService + API/WebUI：显式 null/空白清除，省略不变 | test_review_billing_api_compat + test_billing_payment_lifecycle_review |
| 6 隐藏待付订单付款后不可见 | PaymentService 履约同事务恢复可见并追加日志 | test_billing_payment_lifecycle_review：有效重复回调一次恢复，错金额不恢复 |
| 7 退款键 schema/service 上限不一致 | RefundRequest 最大 160 | test_review_billing_api_compat：160 接受、161 拒绝、可选保留 |
| 8 人工核对 input-only 使用 chat 语义 | billing_reconciliation_service 传实际 call_kind | test_review_billing_reconciliation_meters：Embedding/Rerank 缺失缓存保持 null、准确结算、重复核对一次 |
| 9 Alipay 任意币种造成金额标签错配 | config + API + PaymentService + gateway：国内 page.pay 仅 CNY | test_review_billing_api_compat、test_review_billing_route_changes、test_billing_payment_lifecycle_review：拒绝非 CNY 且不发送网关请求 |
| 10 历史未知币种拖垮整页/API | safe_format_minor_amount + billing_money：原整数/code，格式化 null、单位未知 | test_review_billing_api_compat：真实用户/管理 HTTP/API 页面及原行保留 |
| 11 部分退款后申请仍请求全额 | PaymentService：申请/首次审批当前剩余金额，已存在 attempt 不改身份 | test_billing_payment_lifecycle_review：剩余退款、审批前金额变化、unknown 不重发、剩余零拒绝 |
| 12 full-review 入场失败前清除成功结果 | webhook_execution_service + webhook：先钱包/并发入场后外部清理 | test_webhook_billing_admission：余额/并发拒绝保留原 PRReview 与评论；已知关闭释放，未知交接保留 |
| 13 并发拒绝却消费次数 | TelegramService：Billing/Quota 保存点及同事务提交，Agent 外层统一 carrier 与入场 | test_quota_billing_atomic_admission + test_agent_webui_atomic_admission：PR/Issue/Agent 周期/一次性未损耗、余额拒绝、创建/提交失败、非调度状态、重放一账 |
| 14 Paddle 异步终态留下旧 pending | PaymentEventService：原生 adjustment/transaction 关联，追加 superseded 审计 | test_billing_paddle_refund_lifecycle：批准/拒绝、乱序、证据冲突、锁行跳过后 replay，原证据保留 |
| 15 关闭购买阻断历史退款 | require_payment_enabled + sidebar/index：历史/退款精确路径放行，购买继续关闭 | test_review_disabled_payments_and_ipn：用户归属、管理员、CSRF、真实 HTTP、入口及购买表单 |
| 16 NOWPayments 缺验签配置 ACK200 | gateway 类型异常 + webhook：配置 503、签名 400、已验签非目标 200 | test_review_disabled_payments_and_ipn：真实 HMAC 及 HTTP，无未验签 Inbox 写入 |

自审补充：BillingService._operation 在旧身份快照遇到当前新 operation 时
返回可重试 billing_conflict，禁止 operation→wallet 的反向锁获取；可选缺行
探测使用当前锁读。register_operation 刷新已加载 ORM 终态。
test_billing_operation_identity_snapshot 验证拒绝不改余额/预留、恢复和终态刷新。
回执核对的调用/Worker 检查使用当前锁读，用户不能核对或伪装未调用。
test_webhook_execution_reconciliation 覆盖 dry-run 不写、权限、三类核对、重复
核对、已调用/活动 Worker 拒绝及同事务管理员审计。

本轮新增唯一表 webhook_execution_receipts 随已有 create_all/迁移入口创建，
scripts/verify_billing_sqlite.py 验证首次/重复初始化、唯一键、所有财务不可变
触发器、钱包回滚/重建及包含新 CLI 的 dry-run。CLI 操作与恢复边界见
[配置与恢复文档](billing-2.md)。原生 MySQL/PostgreSQL RR/锁竞争本轮未实测；
Paddle 锁查询仅有真实服务调用 spy、原生 SQL 编译和跳过锁行边界模拟证据。

最终验证（本轮新增 102 项自动化回归）：

| 实际命令/检查 | 结果 |
| --- | --- |
| `UV_CACHE_DIR=/tmp/sakura-billing-uv-cache uv run --no-sync python -m pytest -q -rs` | 5245 passed、17 既有条件 skip，115.91s |
| `uv run --no-sync python -m pytest updater/tests -q` | 414 passed、3 systemd 条件 skip |
| `uv run --no-sync python -m pytest sandboxer/tests -q` | 131 passed、12 Docker 条件 skip |
| `PYTHONPATH=/tmp/sakura-621-test-deps uv run --no-sync python -m pytest tests/test_legacy_placeholder_atomicity.py -q` | 用已隔离的 aiosqlite 补跑：3 passed；没有修改依赖清单 |
| `PYTHONPATH=/tmp/sakura-621-test-deps uv run --no-sync python scripts/verify_billing_sqlite.py` | 首次/重复 schema、不可变触发器、回滚、钱包重建、全部 CLI dry-run PASS |
| `/tmp/sakura-pr660-ruff/bin/ruff check .`（0.16.10）及 `uv run --no-sync python run_ruff.py --check` | 通过 |
| Git HEAD archive 叠加完整本地补丁，以已跟踪 Git mode 和新文件 100644 执行相同 Ruff，及变更的 52 个 Python 文件 format check | 通过，防止工作区权限掩盖 CI 模式错误 |
| `git diff --check`、恢复 CLI `--help` | 通过 |

第一次全量的 19 个新增失败已修复：2 个为代码增行后的时间原语白名单位置
漂移（时区语义未改）；17 个为纯字段测试的记录型 mock 未提供新事务接口。
先返回纯字段错误再访问账务校验；该单元测试只隔离账务服务边界，保留原
响应/字段/保存断言，实际账务激活与写入仍由真实 SQL/HTTP 测试验证。
没有删除测试、放宽断言或增加 skip。

实际 Chrome 在临时 SQLite 站点验证中/英文：关闭购买仍显示订单、用户退款
及管理审核入口；购买/兑换按钮为零；ZZZ 历史金额保留原整数、审核页可打开。
未点击批准、拒绝、付款或退款动作。截图为 /tmp/sakura-disabled-history-zh.jpg、
history-en.jpg、refunds-zh.jpg、refunds-en.jpg（后三个同 sakura-disabled- 前缀）；
服务和标签均已关闭。暂存区仍为空，HEAD 保持 7875ed03。

## 原有上线边界

六阶段本地代码已接通，MySQL账本保护已按授权安装并实测验证，其余原生数据库
验收仍待完成，因此不能把本地测试称为Issue的全部部署验收。配置正式商业
参数、审核历史源及待核对事件后再启用。
停止旧发放Worker再切换；购买Credits不随订阅到期清空；回滚不能删除流水或
重新启用旧bonus加法。命令、失败政策与清单见 [Billing 2.0](billing-2.md) 和
[旧权益迁移](billing-legacy-migration.md)。
