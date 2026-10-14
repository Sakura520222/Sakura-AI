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

## PR #660 后续生命周期审查（基线 a7eeafe925）

2026-10-08，本轮对 4 条 P1 逐条读取真实入口并补 RED/GREEN 回归；邮件页脚
没有作为 GitHub 回复、反应、退订或发布授权。开始实现时 Billing 分支工作区
干净、HEAD 与 a7eeafe925367f51abe9b2046bbbd35d667fede8 一致。

| 审查项 | 本地实现 | 回归及反例 |
| --- | --- | --- |
| 非收费执行因 missing_price 阻止恢复 | billing_service.resume_operation 按冻结收费策略/平台付款者解除金融 gate，保留 Usage 和 pending_reason；限流不放宽 | test_billing_noncharging_resume：9 项，Agent/Scan 持久 Worker 自动续轮与可信恢复、缺失/已知 Usage、开关正反切换、平台成本、用户并发拒绝；初始 6 failed/1 passed |
| Agent 仅保存任务即假确认重放 | billing_admission + webhook + billing_context：入场和交接分阶段，原 operation 配额幂等，持久 CAS，Worker 入口令牌，未知返回 503；现有回执对账同步原任务 | test_agent_delivery_recovery：21 项，Issue/PR 的前后 admission 崩溃、创建竞态、8 线程 SQL CAS、未知窗口、晚取消/手动换付款者、TTL 账务恢复与原载体同步；初始 4 项失败 |
| 网关配置失效先冻结退款 Credits | PaymentService 先构造本地网关，后持久退款 intent/hold，再无数据库锁调用上游；既有未知意图不重发 | test_billing_refund_presend：7 项，禁用/缺 key 重复失败不写意图或 hold、同键恢复、审批、超时/取消未知、两会话退款竞争；初始 3 failed/4 passed |
| synchronize 入场 operation 未执行/终结 | pr_review_incremental_queue + review_worker：按原付款者/op 串行、SQL 派发 token、终态收敛与关闭补偿；recover 服务/CLI/API；maintenance 保留可信排队载体 | test_billing_incremental_operations：28 项真实 SQL/服务/API handler，跨用户、失败/取消/关闭、多行组恢复、旧 token、来源错配、未知调用、权限/审计、长期有效排队及无效载体反例、已知子任务登记失败；首批 5 项均失败 |

没有真实 AI、支付或退款请求。新 Actor 和来源从持久化记录/现有鉴权确定，
恢复 API 不接受客户端付款者；CLI 默认 dry-run，不发送 AI 请求。Agent 没有
启动自动重投循环，未知交接保持可追踪；不承诺跨外部 API 的 exactly-once。
原生 MySQL/PostgreSQL 行锁竞争本轮未实测。

迁移：仅 PR 增量表追加两个 nullable 列 dispatch_token/dispatch_expires_at，
复用自动缺列迁移。scripts/verify_billing_sqlite.py 实际模拟旧表缺列、保留原
队列行，再验证首次/重复迁移、不可变财务触发器、钱包重建及所有 CLI dry-run，
包含 recover-increments。统一切换旧 Worker，回滚保留列和队列/财务审计；
操作步骤见 [Billing 2.0](billing-2.md)。

最终验证：新增 65 项自动化回归；未改依赖清单、未增加 skip、未放宽原断言。

| 实际运行命令/检查 | 结果 |
| --- | --- |
| `UV_CACHE_DIR=/tmp/sakura-billing-uv-cache uv run --no-sync python -m pytest -q -rs` | 最终 5310 passed、17 既有条件 skip，117.05s；前一版本 5309 passed，最后补已知子任务登记失败后重跑 |
| `uv run --no-sync python -m pytest updater/tests -q` | 414 passed、3 systemd 条件 skip |
| `uv run --no-sync python -m pytest sandboxer/tests -q` | 131 passed、12 Docker 条件 skip |
| `PYTHONPATH=/tmp/sakura-621-test-deps uv run --no-sync python -m pytest tests/test_legacy_placeholder_atomicity.py -q` | 已隔离的 aiosqlite 驱动补跑 3 passed；没有依赖变更 |
| `PYTHONPATH=/tmp/sakura-621-test-deps uv run --no-sync python scripts/verify_billing_sqlite.py` | 旧表缺两列/旧队列行保留、首次/重复迁移、所有财务不可变触发器、回滚、钱包重建、全部 CLI dry-run PASS |
| `ruff check .`（隔离 CI 0.16.10）和 `uv run --no-sync python run_ruff.py --check` | 通过 |
| 完整补丁叠加 Git HEAD archive，使用 tracked Git mode 和新文件 100644；同 Ruff check + 20 个变更 Python 文件 format check | 通过，/tmp/sakura-a7ee-ci-patch-nxvtieoi |
| `git diff --check`、`billing_maintenance.py recover-increments --help` | 通过 |

17 条件 skip 为 Docker 12、root/sticky 权限 1、未装默认 aiosqlite 1、systemd 3；
aiosqlite 场景已另外隔离补跑。没有运行 Docker/systemd/root 专属或原生数据库
实测，不把 SQL 编译/SQLite 验证称为 MySQL/PostgreSQL 验收。所有测试外部接口
仅在边界模拟。本轮没有 WebUI 模板变更，不新增未经验证的浏览器结果。
暂存区为空，HEAD 保持 a7eeafe925；未推送、创建工作树或执行生产操作。

## 缺验签配置回调修复（基线 cd4f341523）

2026-10-09，核实 Paddle 与 Stripe 将缺失 secret 转 UNKNOWN，再被 handler
返回 200 ignored，确实会误确认未处理回调。两网关现在复用已有配置/验证
异常：None、空串或空白配置返回 503 retry_required；签名失败或不可解析
JSON/UTF-8 返回 400 verification_failed；已经验签的未处理事件保持 200 ignored。
Stripe SDK 的其他运行异常传播给 handler 的可重试失败，日志不插入异常全文。
Paddle 直接对原始字节验 HMAC；已验签金额/币种归一化仍走原持久 Inbox 路径。

新增 test_billing_webhook_verification_config（14 项）及
test_review_stripe_verification_config（13 项），共 27 项。前者使用真实 ASGI、
本地原生签名和真实 SQLite：缺密钥时订单/流水/Inbox 不变，补齐配置后的
同一回调重复投递只入账一次、余额可重建；覆盖坏签名、缺签名、坏 JSON 与
已验签未处理事件。Stripe 原生 SDK 专项还覆盖过期签名、坏 UTF-8、未调用
construct_event 的配置检查及 SDK 非验证错误。正确测试夹具下 Paddle HTTP
回归修复前 6 failed/8 passed；Stripe 专项修复前 12 failed/1 passed。

旧测试更新为新的严格失败语义，保留“未验签不写财务表”的断言，没有删除
测试或增加 skip。测试订单直接构造本地快照，签名本地生成，没有真实付款、
退款、AI 请求或生产账户修改。无需数据库迁移、依赖清单或 WebUI 模板变更。

| 实际检查 | 结果 |
| --- | --- |
| `uv run --no-sync python -m pytest -q`，两个新文件及 Stripe/Paddle、支付 Inbox、Paddle lifecycle、NOWPayments HTTP 回归共七个文件 | 107 passed，7.13s |
| `UV_CACHE_DIR=/tmp/sakura-billing-uv-cache uv run --no-sync python -m pytest -q -rs` | 5337 passed、17 既有条件 skip，137.48s |
| `/tmp/sakura-cd4f-ruff/bin/ruff check .`（隔离安装 0.16.10）及 `uv run --no-sync python run_ruff.py --check` | 通过；未改项目依赖 |
| Git HEAD archive 叠加本地补丁，tracked Git mode/新文件 100644，Ruff check + 7 个 Python 文件 format check | 通过，/tmp/sakura-cd4f-ci-patch-hc5pjuv2 |
| `git diff --check` | 通过 |

17 个 skip 仍为 Docker 12、root/sticky 权限 1、缺默认 aiosqlite 1、systemd 3；
本轮没有追加这些环境门槛的实测。当前 HEAD 保持 cd4f341523，暂存区为空，
没有提交或推送；不将本地验证称为远端 CI 或生产验收。

## Issue 重新分析入场与 Paddle 币种边界（基线 9adb69fa0a）

2026-10-11，工作基线 9adb69fa0ae5d74c5acd9e28aca5d9b58a83fe60，开始时
Billing 分支工作区干净。按实际代码核实两条 P2，未将邮件页脚作为外部操作授权。

| 审查项 | 实现 | 可重复证据 |
| --- | --- | --- |
| API/WebUI 返回 task_id 后才拒绝收费入场 | issues/reanalysis_admission 共享服务先提交可信 payer/operation、钱包/并发与预留，再排队；402/429 明确拒绝、原分析不变；管理员平台承担；已知未提交释放，未知交接保留且不污染金融 Usage gate | test_review_issue_reanalysis_billing：16 项，真实 ASGI/SQL 拒绝无 task_id/入账、提交前独立会话可见原 operation、身份不可客户端指定、重复登记一份预留、提交失败与写账中断、迟到 Worker、渲染后 JS 执行 |
| Paddle 下拉广告不受支持币种 | paddle_capabilities 从明确 SDK 成员生成协议目录，配置下拉/Settings/API 与网关发起前共用校验；模型成本、结算与其他通道不变 | test_review_paddle_currency_bounds：5 项，拒绝 USDT/AED、保留 USD/CNY 等、配置 API 不写错误值、合法小写规范化、网关不构造 SDK Client；官方 GET /currencies 与 SDK 明确成员均为 33 种 |

初始两组 16 项回归均失败，证明原路径无提前入场/不限制 Paddle。后续补充
3 项写账/配置 API 与 1 项迟到 Worker、1 项真实渲染 JS 回归，共新增 21 项。
第一遍全量出现 16 个旧夹具失败：旧 Issue 状态测试只提供用户名/单表录制
会话。补齐可信测试 ID 并隔离财务服务边界，保留关闭 409、来源、权限、版本
与语义内容断言；没有为过测试放宽生产身份校验、删除断言或新增 skip。

实际浏览器点击发现 Issue 详情旧 showToast 全局不存在，造成 402 后仍无提示。
该页改用 Base 已有 show-toast 事件，消息由服务端/Jinja 按语言生成，不使用
不存在的 JS 翻译键。渲染后的 JS 由 Node VM 执行真实异步请求/事件路径，
并在临时 SQLite 站点实际点击确认：POST 402、英文余额不足事件且页面可见。
Paddle 页面实际下拉含 33 个选项、USD/CNY 可选、USDT/AED 不可选，切换 USD
成功（未保存配置或发起付款）。临时服务及浏览器已关闭；证据在
/tmp/sakura-9adb-browser-evidence。测试外部 Worker/支付仅在边界模拟。

| 实际检查 | 结果 |
| --- | --- |
| 新回归及 Issue 状态/历史来源/关系回归 `uv run --no-sync python -m pytest -q ...` | 152 passed，6.23s |
| `UV_CACHE_DIR=/tmp/sakura-billing-uv-cache uv run --no-sync python -m pytest -q -rs` | 最终 5358 passed、17 既有条件 skip，120.50s；前版 5357 passed 后补详情 toast 修复再重跑 |
| `/tmp/sakura-9adb-ruff/bin/ruff check .`（隔离 0.16.10）及 `uv run --no-sync python run_ruff.py --check` | 通过；没有依赖清单变更 |
| Git HEAD archive 叠加补丁，tracked Git mode/新文件 100644，Ruff check + 12 个 Python 文件 format check | 通过，/tmp/sakura-9adb-ci-patch-u3qito1b |
| `git diff --check` | 通过 |

无需数据库迁移，不自动修改已保存非法 Paddle 值或历史订单金额；管理员需
明确重新选择通道支持币种。Paddle 目录依据及 HTTP/恢复语义见
[Billing 2.0](billing-2.md)。没有原生 MySQL/PostgreSQL、Docker/systemd/root
门槛实测，不把 SQLite 证据称为生产验收。暂存区为空，HEAD 保持 9adb69fa0a；
未提交、推送、创建工作树或修改生产账户。

## 变基后的两轮审查（基线 1692bec1a1）

2026-10-11，在 feature/billing-system-2.0 的干净工作区核对旧审查
5347927f2c 和当前审查 1692bec1a1；实际 HEAD 是
1692bec1a149f2e8b4e7662c20af0c93ef8557ec。以下均为本地补丁，不把邮件
页脚、审查建议或贡献指南当作提交、推送、付款或生产部署授权。

| 审查项 | 实现位置与行为 | 自动化证据 |
| --- | --- | --- |
| 关闭销售阻断 TRON 到账 | webui/deps 精确 GET 路径；billing 状态接口识别 fulfilled；原账单所有者、永久指纹、金额快照与幂等履约保留 | test_review_disabled_payments_and_ipn：真实 ASGI/SQL 关闭销售后打开原账单、他人拒绝、一次入账、无新 invoice；Node 执行渲染后的组件，只有一份轮询且终态停止 |
| 删除用户绕过账务保留 | TelegramService.retire_user 供 API/WebUI/Telegram 共用；按 ID、锁定读取财务/权益来源，有记录停用，无记录删除；审计结果与中英文提示同步 | test_review_billing_user_retention：真实 FK 开启，零钱包、入账、旧订单/权益、执行与限流记录，权限/自身/CSRF、写入中断及语言、未绑定/同名身份反例 |
| Text 上的 JSON 来源查找 | PaymentEventService 按通道及固定高水位 keyset 读取、应用层校验；损坏无关行保留，匹配不明确进入待核对，选中后锁定复核 | test_review_payment_reference_lookup：损坏/非对象 JSON、中文/转义标识、跨批次、其他通道、重复退款、重复映射、选中损坏及锁后来源变化；编译 PostgreSQL/MySQL 查询确认无 Text JSON 运算 |
| 手动 Agent 提交无持久交接 | manual_dispatch + agent_team 路由：任务/入场/回执同事务，retryable 恢复原 op/payer，SQL 领取并复用 Worker token 校验，未知交接 503；复用核对 CLI | test_review_manual_agent_dispatch：创建与 queued 恢复、注册拒绝、未知提交、新次数不重复扣、事务中断、并发领取、管理员帮助不改付款者、核对释放及迟到 Worker、旧载体隔离 |
| 不收费失败尝试阻止续轮 | BillingService._has_payable_pending 按冻结策略和正式计价判断续轮；settlement 的 Provider 待核对标记及原未知 Usage 保留 | test_review_failed_call_continuation：PR/Agent 真实 Meter 失败后 fallback、同 op 再结算不重扣，策略反向切换、收费失败正例和无回执 started 反例；原有 Usage/流式/账号回归保持通过 |
| 并发管理员发放唯一键报错 | PaymentService._grant_replay + 保存点冲突恢复；重读并验证财务意图，不吞其他约束错误 | test_review_admin_grant_race：隔离会话中的真实 SQLite 唯一键/保存点，模拟并发首读遗漏，余额/流水只有一次，用户/套餐/操作者冲突拒绝，外层事务仍可使用 |

第一批回归初始 14 failed/14 passed；浏览器复测发现双初始化轮询和误导的
永久删除说明，新增 3 项红测后修复。第一批最终全量 5964 passed/17 既有
条件 skip。第二批新回归复现待核对 gate、真实唯一键异常及缺回执；收费失败
反例需期待下一次发送和终结传播同一财务拒绝，未放宽生产收费失败策略。

第二批首次全量出现 5 个失败：3 个原有未知/部分 Usage 的待核对断言提醒
保留 Provider 成本信号，因此改为单独判定续轮而保留原结算标记；另 1 个
AsyncMock 保存点夹具修正为同步返回异步上下文，2 个 fromtimestamp 调用
所在的 1 项白名单测试同步精确行号。没有移除断言、增加 skip 或放宽时间
边界。最终运行结果见本节后续验证表。

| 最终实际运行的命令 | 结果 |
| --- | --- |
| `UV_CACHE_DIR=/tmp/sakura-billing-uv-cache uv run --no-sync python -m pytest -q -rs --tb=short` | 最终 5980 passed、17 既有条件 skip，153.66s，无失败或警告；包含主应用/updater/sandboxer；保存点当前读取补强后重新全量运行 |
| `uv run --no-sync python -m pytest -q tests/test_review_admin_grant_race.py tests/test_billing_entitlements.py tests/test_payment_service.py --tb=short` | 91 passed，3.06s；真实唯一键、保存点及服务回归 |
| `uv run --no-sync python -m pytest -q tests/test_review_failed_call_continuation.py tests/test_billing_usage_attribution.py tests/test_billing_account_usage.py tests/test_billing_noncharging_resume.py --tb=short` | 60 passed，5.12s；原未知/部分流式 Usage 断言保留 |
| `/tmp/sakura-9adb-ruff/bin/ruff check .`、`uv run --no-sync python run_ruff.py --check` | 隔离 0.16.10 与 venv 0.16.9 均通过；没有依赖清单或锁文件修改 |
| `/tmp/sakura-9adb-ruff/bin/ruff format --check`（19 个相关 Python 文件）及 `git diff --check` | 通过 |
| Git HEAD archive 叠加 25 个改动文件，恢复 tracked Git mode、新文件 100644 后运行 Ruff | 通过，/tmp/sakura-1692-ci-patch-6n0oyqm3；未将本地检查称为远端 CI |

17 项 skip 为 Docker gate 12、root/sticky gate 1、可选 aiosqlite 1、
systemd gate 3，本轮没有新增 skip。没有运行真实 AI/支付/退款、生产部署，
也未提交、暂存、推送、创建工作树或关闭 Issue。工作区基线 HEAD 保持不变。

实际浏览器在隔离 SQLite 站点确认关闭销售后的 TRON 页面为英文，正常
轮询一次返回 200、Payment Complete、5 TEST Credits 一次入账且轮询停止；
用户移除实际点击确认后保留原行并停用，英文 toast 及确认文案正确。测试网关
在边界模拟，没有真实转账、AI 调用或生产写入。临时服务及浏览器已关闭，
curl 验证 8898 不可达；证据位于 /tmp/sakura-5347-browser-evidence。

本轮没有模型/约束迁移或依赖变更，复用已有执行回执。旧 Worker 应排空并
统一升级；待核对回执需可信证据，不能凭无 Usage 推断未交接。原生
MySQL/PostgreSQL 的本轮并发场景尚未实测，SQL 编译和 SQLite 证据不替代
原生事务验收。配置、恢复与回滚说明见 [Billing 2.0](billing-2.md)。

## 保留身份、退款精度与 Issue 持久交接（基线 d5e41b4972）

2026-10-11，在 feature/billing-system-2.0 的干净工作区核对本轮三项审查，
HEAD 为 d5e41b497225b120a89bd6e40f94fd989f43e1bb。补丁仅修改本地代码、
测试和文档；没有暂存、提交、推送或执行生产动作。

| 审查项 | 状态与实现 | 自动化验收证据 |
| --- | --- | --- |
| 保留身份停用后旧 Token 仍有权限 | 完成。API 共用鉴权边界对 Bearer/query/Cookie 刷新持久身份、活跃状态及角色；Cookie 续期与 MFA 保持原语义，数据库失败不使用旧角色兜底 | test_review_retired_token_auth：9 项，真实签名 JWT/ASGI/SQL 删除金融用户后的所有 Token 运输模式及管理/钱包/直接消费者拒绝、降权与重命名正常反例、缺失身份与 MFA pending；test_webui_session_renewal 原有续期/MFA 回归 |
| 部分退款舍入为零仍冻结并发送 | 完成。PaymentService 用整数累计 half-even 求实际网关差额，非正值在 intent/hold/client 前拒绝，返回 refund_amount_unrepresentable；API 保持字符串 detail，页面、审批提示及持久失败明细按个人语言翻译 | test_review_refund_rounding_presend：11 项，零值/边界重复拒绝后 commit 仍无退款尝试/冻结、可表示多次累计退款及幂等、最小单位全额退款、零现金手动发放冲正、真实 API 和审批页面双语；test_billing_refund_presend 保留真实未知窗口及两会话竞争 |
| Issue reanalysis 入场提交后没有持久载体 | 完成。API/WebUI 同事务保存新的分析行、BillingOperation/预留及现有回执，提前固定 task_id；SQL 领取及 Worker token/source/input fence，复用原分析行；状态及安全恢复接口、已知未开始补偿、未知交接核对复用现有 CLI | test_review_issue_dispatch_carrier：11 项，独立会话提交可见、模拟进程退出、原身份恢复及权限、重复 Worker、processing 不可伪装未执行、核对后迟到 Worker、真实 Worker 早期失败、容量等待取消、启动前关闭/取消释放；原有 reanalysis/legacy state/relations 断言保留 |

首批 Token/退款测试在修复前为 10 failed/3 passed；最初持久交接测试三项
均失败。自审新增的“Worker 首条指令前关闭/取消”两项也先复现红测，再补
无调用补偿。旧 Issue 单表/队列夹具增加持久分析写入接口和服务器 task_id，
财务/交接边界继续由完整 SQL 集成测试验证，没有移除原权限、关闭状态、
关系判定及版本断言。全量中发现的 Agent 错误文案兼容问题恢复原文案；
新增页面夹具按既有 302 合约和完整认证身份修正，没有放宽余额断言或 skip。

Token 修复依照 codex-security:fix-finding 的独立只读调查及候选复核完成；
复核未发现旁路，并验证读取失败拒绝、混合 Token 优先级、实时降权及 MFA
反例。复核后的 API deps 内容哈希保持一致。请求入场后的执行及已打开 SSE
连接不被本轮的新请求检查追溯取消。

实际浏览器在隔离 SQLite 站点操作审批和确认，英文及中文失败提示、
明细均正确；切换持久个人语言后重试仍无网关请求。测试余额保持 5,000,000
微 Credits、reserved=0、attempt_count=0、gateway_calls=0，账本重建一致。
截图与快照在 /tmp/sakura-d5-browser-evidence-fmof1m7l；只挂载账单功能的
测试站点缺少公告/SSE/版本接口，其 404 不作为整站验证。临时服务及浏览器
已关闭，8899 的 curl 为 000，没有真实退款、转账或 AI 调用。

| 最终实际运行的检查 | 结果 |
| --- | --- |
| `UV_CACHE_DIR=/tmp/sakura-billing-uv-cache uv run --no-sync python -m pytest -q -rs --tb=short` | 6011 passed、17 既有条件 skip，177.42s，无失败；包含主应用、updater、sandboxer，启动前取消修复后重新全量运行 |
| `uv run --no-sync python -m pytest tests/test_review_retired_token_auth.py tests/test_review_refund_rounding_presend.py tests/test_review_issue_dispatch_carrier.py tests/test_webui_session_renewal.py tests/test_billing_refund_presend.py tests/test_review_issue_reanalysis_billing.py tests/test_webhook_execution_reconciliation.py tests/test_agent_delivery_recovery.py tests/test_issue_cancellation_races.py tests/test_issue_worker_cancellation.py tests/test_service_billing_liveness.py -q --tb=short`（同一 UV_CACHE_DIR） | 128 passed，9.62s；31 项本轮新增回归全部包含 |
| `/tmp/sakura-9adb-ruff/bin/ruff check .`、`uv run --no-sync python run_ruff.py --check` | 0.16.10 与 venv 0.16.9 均通过 |
| `/tmp/sakura-9adb-ruff/bin/ruff format --check`（本次相关 18 个 Python 文件）、`git diff --check` | 通过 |
| `python3 /tmp/sakura-1692-ci-copy.py` | HEAD archive 叠加本次 23 文件、恢复 tracked Git mode/新文件 100644 后 Ruff 通过；临时副本 /tmp/sakura-1692-ci-patch-yp_xh5ou，不将本地检查称为远端 CI |

17 项 skip 为 Docker gate 12、root/sticky gate 1、可选 aiosqlite 1、systemd
gate 3，本轮没有新增 skip。没有运行真实 AI、支付/退款、生产部署或关闭 Issue。

本轮复用现有表，无结构迁移或依赖变更。排空并统一升级旧 Issue Worker，
避免旧代码绕过领取令牌；已有未知交接继续使用持久证据核对，不能凭无 Usage
重发，回滚保留所有财务与交接记录。原生 MySQL/PostgreSQL 多 Worker 的
本轮新增交接场景尚未实测；SQLite 事务证据不替代原生部署验收。

## 自动投递、退款身份与 Usage 对账（审查 f9fb37fb36）

2026-10-13，先在干净的 feature/billing-system-2.0 工作区 fetch 并变基到
origin/develop 8c7a5e2fb0e7ba661c5d8b08502a327ddcc162b8，变基后 HEAD 为
ddd8d39112c4a31c4436365b62384d38cd004402。唯一冲突保留双方所需的测试导入；
已验证 develop 为 HEAD 祖先。下述新补丁未暂存、提交或推送，没有生产动作。

| 审查项 | 状态与实现位置 | 可重复验收证据 |
| --- | --- | --- |
| 自动 PR/Issue 的成功回执早于持久 Worker | 完成。webhook_review_dispatch 保存私有 queued 载体及原财务入场，启动/重送恢复直接派发；SQL token fence、started 待核对、持久取消及账务过期保护；main 注册并观察恢复任务 | test_review_automatic_delivery_dispatch：22 项，原生提交器第一条 Worker 指令前模拟进程退出、PR/Issue 启动恢复/重放、旧 Worker 及重复 Worker、真实用量与账本、单条失败隔离；test_webhook_execution_reconciliation/test_service_billing_liveness 等保留原断言 |
| /analyze 重投递重复派发 | 完成。handle_issue_analyze_command 在配额和派发前领取 issue_analysis 回执 | 同一新增套件中的真实 ASGI、签名 webhook、SQL 付款者/次数/预留及并发投递证明仅派发一次 |
| 外部退款人工确认没有原生退款身份 | 完成。payment_service 在审核/冻结/入账前核实身份，payment_event_service 绑定相同退款，CLI 显示原 wire 证据；Alipay 区分请求身份及累计通知，历史无确证保持待核对 | test_review_refund_reconciliation_identity/test_review_alipay_refund_identity：缺 ID、重放、迟到回调、不同部分退款、中断恢复、历史 trade-only/NULL 身份、规范化及原始请求号反例；完整退款相关套件见下表 |
| 活动 Worker 的用量被人工替代后双扣 | 完成。billing_reconciliation_service 拒绝非终态及有效 Worker 所有权；ai_usage_service 追加已审核请求的迟到结果证据，billing_service 核实关系后排除重复结算 | test_review_usage_reconciliation_liveness/test_review_usage_reconciliation_late_results：活动 queued/running 所有权、终态无所有权反例、审核后迟到结果、未知/部分原记录、不同实际请求、证据伪造、提交失败重试及账本重建 |

独立复核发现并修复了增量回执重放误降级、旧回执身份继承、数据库等待前
取消信号未发出、queued 载体过期被误收回，以及历史支付宝规范化身份重复
冲销。新增测试均先复现相应失败：增量重放 503、下一增量 body=0/队列 running、
取消事件 unset、重复人工确认两条审计，以及同一历史退款再次冲销 30 分。
真实 SQL/runner 复核确认增量只执行一次、按原 operation 结算，取消成功和
持久写入失败均先通知并等待清理、失败继续抛出，没有后续发布。

变基后的基线全量有 20 个 Issue 模板验证失败。兼容修复保留 develop 的必需
占位符规则，并恢复 Billing 的结构化字段错误和双语提示；未知占位符/格式
错误不泄露用户输入或底层异常。test_issue_template_validation_feedback 新增
44 项反例与有效模板/双语校验，未放宽原有断言。中途全量的两项剩余失败为
支付宝文件位移导致协议时间白名单行号失配、旧 PR 关闭夹具缺少实际数据库；
分别同步精确位置和 SQL 夹具，未新增时间原语或 skip。

| 最终实际运行的检查 | 结果 |
| --- | --- |
| `UV_CACHE_DIR=/tmp/sakura-billing-uv-cache uv run --no-sync python -m pytest -q -rs --tb=short --junitxml=/tmp/sakura-f9-final.xml` | 6241 passed、41 既有环境条件 skip，184.57s，无失败；含主应用、updater、sandboxer |
| `uv run --no-sync python -m pytest -q tests/test_review_automatic_delivery_dispatch.py tests/test_webhook_execution_reconciliation.py tests/test_billing_incremental_operations.py tests/test_issue_worker_cancellation.py tests/test_webhook_billing_admission.py tests/test_webhook_incremental_queue.py tests/test_webhook_auto_review_toggle.py --tb=short`（同一 UV_CACHE_DIR） | 98 passed，23.21s |
| 用量对账十套件：review_usage_reconciliation_late_results/liveness、billing_reconciliation/account_reconciliation、review_billing_reconciliation_meters、billing_unified_chat_tariff/settlement、service_billing_liveness、billing_usage_attribution/account_usage | 124 passed，11.20s |
| `uv run --no-sync python -m pytest tests/test_review_alipay_refund_identity.py tests/test_review_refund_reconciliation_identity.py tests/test_alipay_gateway.py tests/test_billing_entitlements.py tests/test_billing_payment_events.py tests/test_billing_payment_lifecycle_review.py tests/test_billing_paddle_refund_lifecycle.py tests/test_billing_refund_presend.py tests/test_review_refund_rounding_presend.py tests/test_payment_service.py tests/test_billing_credits_api.py tests/test_billing_wallet.py tests/test_billing_settlement.py tests/test_stripe_gateway.py tests/test_paddle_gateway.py -q --tb=short --show-capture=no`（同一 UV_CACHE_DIR） | 301 passed，19.88s；独立复核再次运行两个退款身份文件为 45 passed |
| 时间原语、Issue 模板字段反馈、section_config_service、webui_unified_config | 150 passed，5.15s |
| `uv run --no-sync python -m ruff check .`、`uv run --no-sync python run_ruff.py --check` | venv Ruff 0.17.0 全仓通过 |
| `uv run --no-sync python -m ruff format --check`（全部 27 个相关 Python 文件）、`git diff --check` | 通过 |
| `scripts/reconcile_payment_refunds.py --help`、`scripts/reconcile_webhook_executions.py --help` | 通过；未应用生产清单或发起外部请求 |

41 项 skip 为原生 Windows 24、Docker gate 12、root/sticky gate 1、可选
aiosqlite 1、systemd gate 3；本轮补丁没有新增 skip。Ruff 使用 venv 0.17.0，
没有使用历史临时 Ruff 路径。最终 27 个 Python 文件哈希留存于
/tmp/sakura-f9-final-source-sha256.txt，JUnit 留存于上述路径；都是本地临时证据，
不会写入生产或替代远端 CI。

本次没有新增数据库结构。恢复载体复用现有 JSON 列；必须统一升级并排空旧
Worker，旧回执缺少可信交接证据时保留待核对。CLI 默认只读，只有审核明确
证据后才 apply；自动重新派发仅针对持久 queued 载体，未知 started 请求不
自动重发。退款核对必须关联原生请求身份；历史缺证据不自动转换流水。
完整操作及回滚边界见 [Billing 2.0](billing-2.md)。本轮没有原生 MySQL/
PostgreSQL 多 Worker 验收或远端 CI 完成证据，不将本地结果描述为生产验收。
变基使用 develop 已锁定的依赖；本次修复没有修改依赖清单或锁文件。

## 2026-10-13 PR #660 变基与审查修复

- 已变基到 `develop` 的 `5e379ade`，合并日志追踪与计费/派单装饰器，保留
  异常脱敏、取消传播及原有任务恢复行为。
- Stripe 按原生退款 ID、PaymentIntent、金额、币种和订单收敛非终态回执；
  支持成功/失败/取消、终态先到、累计多退款通知、原生失败及兼容更新事件。
  原始接收/审核证据不被覆盖，追加 superseded 审计；冲突继续待核对。
- 兑换码使用当前锁定读及保存点处理并发唯一键冲突，赢家订单只发放一次；
  失败请求的新订单、占用次数及兑换记录一起撤回，调用方事务保留。
- 计费准入、结算、不可变定价版本、回执持久化及收敛补充结构化日志。
  网关调用绑定具体凭据/验签头作用域，异常副本和截断 URL 先统一脱敏；
  兑换日志及 PaymentLog 使用内部兑换码 ID。控制台和 JSON Lines 实际输出
  覆盖异常堆栈、编码后的 opaque key、作用域退出后重新记录、线程和并发隔离。
- `test_logging_review_regressions` 的扫描替身补齐计费入口所需读取；翻译
  校验显式使用 UTF-8，保持 Windows 测试可执行，没有新增 skip。

新增证据位于 `test_review_stripe_refund_lifecycle`、`test_review_redeem_concurrency`
和 `test_billing_operational_logs`。与既有 Paddle/支付宝/退款身份套件联合运行
为 94 passed；新增三个文件再次验证为 35 passed。日志、配置、计费 Worker、
恢复和扫描超时相关套件为 79 passed。全仓 Ruff、`run_ruff.py --check`、相关
Python 文件格式检查及 `git diff --check` 均通过。

本次不新增数据库结构或配置项，不修改商业价格、不调用真实支付网关。
兑换竞态证据使用真实 SQLite 唯一约束/保存点、受控快照模拟及 MySQL 当前读
SQL 校验；不将其描述为原生 MySQL 多 Worker 部署验收。

远端 Linux 首轮全仓验证发现跨进程容量测试的启动时序问题：原测试用
80 ms 工作窗口推断最大并发，spawn/import 错开时只观察到一个持有者。
后续测试让五个子进程先共同就绪，前两个获准进程保留真实 SQL 租约，直到
父进程确认二者同时运行且没有第三个进入后再释放。保留最大并发恰为 2、
最终归零和子进程正常退出的断言，不修改生产容量实现。容量套件 13 项及
该跨进程测试的独立重复运行均通过。

后续 Windows CI 日志暴露了 Agent 13 轮持久化和 Git 记账的 2 秒测试等待，
以及 60/120 ms 集成租约对慢磁盘/日志 I/O 的依赖。行为测试的挂起保护改为
10 秒；心跳验证等待实际续租确认并检查 SQL 有效期延长，短/长资源仍保留
5:1 比例并分别断言冻结后的心跳周期。Worker 的失效注入在业务阶段进入后
触发，继续核实 failed 财务收尾、精确分析行、取消语义和注册表清理。
生产租约、并发和计费配置保持原值；失效日志补齐受保护的堆栈及结构化
service_feature/service_resource_id。相关六套件为 123 passed、2 既有平台
条件 skip，控制台和序列化日志另验证了失效诊断的来源及关联信息。

## 原有上线边界

六阶段本地代码已接通，MySQL账本保护已按授权安装并实测验证，其余原生数据库
验收仍待完成，因此不能把本地测试称为Issue的全部部署验收。配置正式商业
参数、审核历史源及待核对事件后再启用。
停止旧发放Worker再切换；购买Credits不随订阅到期清空；回滚不能删除流水或
重新启用旧bonus加法。命令、失败政策与清单见 [Billing 2.0](billing-2.md) 和
[旧权益迁移](billing-legacy-migration.md)。
