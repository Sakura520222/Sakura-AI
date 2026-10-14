# Unified configuration centre / 统一配置中心

Issue #677 is implemented against `develop` at `5e379ade` (checked 2026-10-13).
#622 consolidated navigation domains; #638 remains an open React migration epic.
This baseline has no `web/`, React Settings, Vite, or frontend test project. The
shipping centre therefore uses the existing shell with framework-independent ES
modules. Future React Settings can reuse its API, metadata, typed contract and
field state machine; there is no second configuration centre to maintain.

## Entry points and ownership

| Previous URL | New category | Ownership and storage |
| --- | --- | --- |
| `/settings/` | `/settings-center/personal` | Current user; WebUIConfig and UserConfig |
| `/config/ai` | `/settings-center/ai` | Super admin; account/binding services and AppConfig |
| `/config` | `/settings-center/review` | Super admin; scalars and registered strategy sections |
| Strategy/label anchors | Review, Issue, Agent, Labels | Existing section JSON overrides |
| `/system-config/` | `/settings-center/system` | Super admin; system validation and deployment settings |
| `/config/backup` | `/settings-center/backup` | Super admin; existing backup services |

The sidebar has one Settings entry. Only the selected category's values and DOM
are loaded. Search uses a role-filtered, value-free index of names, descriptions
and keys in both languages. Groups collapse and advanced settings are initially
closed. Business pages (tasks, Skills, billing, repositories, users) stay separate.
Plugin permissions/credentials retain their dedicated editor, linked from Agent.

Old GET routes redirect with their query parameters; old section bookmarks map to
categories/groups. POST/API routes remain and use shared persistence services.
Missing scalar fields remain unchanged, including booleans: legacy clients must
explicitly submit `false` to turn a switch off. The obsolete long configuration
form and dynamic-card template have been removed. Accounts, 2FA, Passkeys,
notification binding, connection tests, App-slug lookup, restart, database reset
and backup import/export retain dedicated workflows. Setup stays independent and
continues sharing system/section validation.

## Metadata and API

`backend/services/config_metadata.py` is the inventory adapter. It derives fields
from Settings definitions, existing registries, section defaults and model
metadata. `backend/webui/assets/settings/contract.d.ts` documents the browser
projection. Sensitive `value`, `default` and `inheritedValue` are always null;
`hasValue` conveys presence. Ordinary users receive no administrator descriptors.

Authenticated endpoints:

* `GET /api/v1/config-center?category=review&language=en`
* `GET /api/v1/config-center?search=true&language=zh-CN` (no values)
* `GET /api/v1/config-center?category=ai&provider=openai&model=...`
* `PATCH /api/v1/config-center` with `{field, version, value, confirmed?}`
* `POST /api/v1/config-center/reset` with `{field, version}`

Mutations require `X-CSRF-Token` and return a server-confirmed field, source,
version, `persisted: true`, and `effect` (`live`, `restart_required`, `pending`).
Stale versions return 409, invalid values 422, inaccessible fields 404 and storage
failures 503 without exception details. Limits are 120 mutations/minute and 1 MiB.
OpenAPI includes the mutation schema. Legacy cookie configuration APIs also
require CSRF; bearer clients retain their existing mode.

## Persistence, effects and concurrency

The additive `config_revisions` table provides durable locks and versions.
Revision rows survive deletion of an override, preventing absent/override/reset
ABA. Database upsert and locking reads serialize independent processes, including
creation of missing rows. A shared validation lock protects cross-field
constraints; per-storage-unit revisions and existing row identity detect stale
clients. One field or atomic object commits with a metadata-only audit; values,
credentials and prompt text never enter that audit. Legacy scalar/section/system
writers, account persistence, model overrides and backup restore participate in
the shared protocol.

Account edits also carry a durable revision (independent of wall-clock time),
and deletion checks role references again inside the same database lock.

Post-commit effects invalidate caches, update live Settings, reload strategy and
label facades, reset review/Issue semaphores, and reconcile scan/Star Aid schedules.
Embedding/reranker clients retain their existing live refresh. Frozen listener,
GitHub identity and Chroma directory bindings require restart. Restart-only fields do not mutate the active process. Each application process
polls durable revisions using `config_sync_interval_seconds` (Settings default:
2 seconds), restricted to registered runtime keys and cached users. This is
bounded eventual propagation across replicas, not an instantaneous fleet switch.
Failed application returns `pending` and is retried by the supervised worker.
Missed notifications require no Redis recovery. The worker is restart-only under
source hot reload so active synchronization state is not replaced.

## Defaults and reset policy

* Scalars resolve immutable field default → deployment environment → database
  override. Reset deletes the database row and resolves inheritance. A mutable
  runtime Settings singleton is never the factory-default source. Startup stops
  materializing dynamic defaults; existing overrides are retained unchanged.
* Sections deep-merge registered defaults with database leaves. Reset deletes only
  the selected leaf and empty ancestors. Arrays and extensible map editors are
  atomic JSON units; fixed dictionaries expose nested leaves. Unknown historical
  overrides survive unrelated edits. The legacy dependency-graph mode alias is
  reconciled without changing its prompt overrides.
* Personal null columns mean inheritance. Reset touches one column; output
  language deletes its UserConfig override and inherits global output language.
  An explicit empty output-language string follows the request/UI language and
  remains distinct from deletion, including through legacy forms/APIs. Other
  users' preferences remain unchanged.
* Nonsensitive nullable scalars can reset to null or an environment-provided
  value. Restart-only fields still require restart when their runtime value differs.
* Model defaults use the provider/model metadata resolver. Reset deletes one
  capability/parameter override; legacy partial updates preserve omitted fields.
* Credentials require explicit confirm-and-save. Empty/masked values are rejected
  and generic reset is disabled. Accounts retain a dedicated validated operation.
  Role bindings cannot reset to an unusable, unconfigured account.
* Database/Redis connections are deployment-owned, shown without credentials and
  tested through the authorized connection test. AppConfig cannot atomically move
  the active connection or connection.json. The centre directs administrators to
  deployment configuration and restart rather than reporting an ineffective
  runtime write as success. Imports continue protecting deployment connections.

## Browser behaviour and validation

`FieldController` separates draft, confirmed value, request generation and version.
Switches/selects save immediately; text/numbers debounce for 650 ms or flush on
blur. IME composition suppresses submission. Writes are serialized, sibling
versions refreshed, and old responses cannot replace a newer draft. Failures keep
input with Retry. Conflicts require adopting the server value before editing
again; changed remote values are never silently overwritten. BroadcastChannel and
focus refresh reconcile tabs. Outstanding drafts/requests trigger a navigation
warning. Credentials are never persisted in browser storage.

```sh
uv sync --locked
# Classic pip: python -m pip install -r requirements-dev.txt
uv run pytest -q
uv run python run_ruff.py --check
uv run python -m compileall -q backend
cd tests/webui
npm ci
npm run check
npm test
npx playwright install chromium
npm run e2e
```

On Windows, an installed Edge can be selected with `PLAYWRIGHT_CHANNEL=msedge`.
CI installs Chromium. The browser fixture uses real routes and isolated SQLite,
fixture-only authentication, and disables destructive/external effects. Tests
exercise persistence, CSRF, permissions, secrets, nested/model reset, legacy
aliasing, runtime recovery, two independent writer processes, debounce/IME, retry,
navigation, language/theme and 1440/820/390 px layouts. Separate CI jobs run the
transaction and independent-process suite against MySQL 8.4 and PostgreSQL 17.
These fixtures do not establish fleet deployment performance. Real OAuth/provider calls and hardware
authenticator ceremonies remain outside this fixture.

Production serves these ES modules directly; no React bundle exists at this
baseline. JavaScript syntax, template rendering, Python compilation and browser
tests validate the assets. Docker's existing backend copy includes them, so no
production Node server is required.

## Upgrade and rollback

Export configuration and the separate user backup before upgrading. Installation
does not rewrite configurations, accounts or bindings. Startup creates the new
revision table without destructive schema changes. Upgrade all replicas before
relying on revision propagation; older binaries do not publish revision changes.

To roll back, stop writers and restore the earlier image/source. Leave the
additive table in place; older binaries ignore it. Older API versions may return
null preference columns after a reset. Before downgrading, materialize documented
personal defaults (`system`, `zh-CN`, `20`) for null columns or restore the user
backup. Use the validated backup importer and preserve deployment database/Redis
settings. Account and model storage formats remain compatible.

## 配置清单

下面的字段清单由当前注册表生成。所有全局分类只对超级管理员开放；个人分类
只作用于当前登录用户。数组和可扩展对象作为最小原子单元，固定对象展开到叶子。
模型覆盖字段根据所选提供商和模型元数据按需生成。

### PR 审查

**review_basic** — `app`

`max_concurrent_reviews`, `review_timeout_seconds`, `enable_auto_review`, `enable_check_runs`, `enable_analysis_check`, `enable_findings_check`, `analysis_min_interval_sec`, `protocol_repair_max_attempts`。

**code_index** — `app`

`enable_code_index`, `code_chunk_size`, `code_chunk_overlap`。

**review_strategy** — `app`

`enable_inline_comments`。

**pr_summary** — `app`

`enable_pr_summary`。

**pr_dependency_graph** — `app`

`enable_pr_dependency_graph`, `pr_dependency_graph_max_nodes`, `pr_dependency_graph_max_files`, `pr_dependency_graph_mode`。

**semantic_issue_linking** — `app`

`enable_semantic_issue_linking`, `semantic_issue_similarity_threshold`, `semantic_issue_max_links`, `pr_issue_related_confidence_threshold`, `pr_issue_closing_confidence_threshold`, `pr_issue_max_files`, `pr_issue_max_input_tokens`。

**strategy.strategies** — `section`

`quick/name`, `quick/conditions/max_files`, `quick/conditions/max_lines`, `quick/prompt`, `standard/name`, `standard/conditions/max_files`, `standard/conditions/max_lines`, `standard/prompt`, `deep/name`, `deep/conditions/max_files`, `deep/conditions/max_lines`, `deep/prompt`, `large/name`, `large/conditions/max_files`, `large/conditions/max_lines`, `large/prompt`, `strategy.strategies`。

**strategy.file_filters** — `section`

`skip_extensions`, `skip_paths`, `code_extensions`。

**strategy.context_enhancement** — `section`

`enable_project_structure`, `max_structure_files`, `enable_ai_tools`, `max_file_size`, `max_files_for_deep_strategy`, `max_file_lines`, `max_file_output_chars`, `default_context_lines`, `max_context_lines`, `search_in_files/use_search_api`, `search_in_files/default_context_lines`, `search_in_files/default_max_results`, `search_in_files/skip_binary`, `search_in_files/max_files_to_search`, `search_in_files/concurrency`, `search_in_files/max_file_bytes`, `search_in_files/max_total_scan_bytes`, `search_in_files/max_matches_per_file`, `search_in_files/max_output_chars`, `git_tools/default_branch_count`, `git_tools/default_commit_count`, `ci_failure_injection/enabled`, `ci_failure_injection/retention_days`, `ci_failure_injection/max_records`, `ci_failure_injection/max_annotations_per_record`, `sakura_memory/enabled`, `sakura_memory/reflection/enabled`, `sakura_memory/reflection/model`, `sakura_memory/reflection/prompt_template`, `sakura_memory/reflection/max_comments`, `sakura_memory/reflection/max_changed_files`, `sakura_memory/reflection/max_new_commits`, `sakura_memory/issue_reflection/enabled`, `sakura_memory/issue_reflection/model`, `sakura_memory/issue_reflection/prompt_template`, `sakura_memory/consolidation/interval`, `sakura_memory/consolidation/model`, `sakura_memory/consolidation/max_memory_chars`, `sakura_memory/consolidation/max_sakura_chars`, `sakura_memory/consolidation/cleanup_old_reflections`, `sakura_memory/consolidation/partial_commit`, `sakura_memory/knowledge_extraction/enabled`, `sakura_memory/knowledge_extraction/min_reflections`, `sakura_memory/initialization/auto_init`, `sakura_memory/initialization/init_commit_message`, `sakura_memory/directory_convention/enabled`, `sakura_memory/directory_convention/auto_create_subdirs`, `sakura_memory/directory_convention/categories/memory/description`, `sakura_memory/directory_convention/categories/memory/priority`, `sakura_memory/directory_convention/categories/memory/skip_placeholder`, `sakura_memory/directory_convention/categories/rules/description`, `sakura_memory/directory_convention/categories/rules/priority`, `sakura_memory/directory_convention/categories/rules/placeholder`, `sakura_memory/directory_convention/categories/docs/description`, `sakura_memory/directory_convention/categories/docs/priority`, `sakura_memory/directory_convention/categories/docs/placeholder`, `sakura_memory/directory_convention/categories/plans/description`, `sakura_memory/directory_convention/categories/plans/priority`, `sakura_memory/directory_convention/categories/plans/placeholder`。

**strategy.review_policy** — `section`

`enabled`, `approve_threshold`, `block_threshold`, `block_on_critical`, `max_major_issues`, `ignored_patterns`, `repo_overrides`, `enable_idempotency_check`, `review_templates/approve`, `review_templates/request_changes`, `review_templates/comment`, `review_templates_en/approve`, `review_templates_en/request_changes`, `review_templates_en/comment`。

**strategy.pr_summary** — `section`

`system_prompt`, `user_template`。

**strategy.pr_dependency_graph** — `section`

`system_prompt`, `user_template`。


### Agent

**web_search** — `app`

`web_search_enabled`, `web_search_provider`, `web_search_api_key`, `web_search_max_results`, `web_search_max_content_length`, `web_search_timeout`。

**agent_team** — `app`

`agent_team_enabled`, `agent_team_workspace_root`, `agent_team_execution_backend`, `agent_team_network_policy`, `agent_team_repo_allowlist`, `agent_team_max_concurrent`, `agent_team_subagent_concurrency`, `agent_team_min_priority`, `agent_team_feasibility_keywords`, `agent_team_draft_pr`, `agent_team_pr_closed_loop_enabled`, `agent_team_pr_review_pass_score`, `agent_team_pr_review_blocking_severities`, `agent_team_run_tests`, `agent_team_auto_install_deps`, `agent_team_dependency_install_attempts`, `agent_team_dependency_retry_delay_seconds`, `agent_team_skills_enabled`, `agent_team_skills_root`, `agent_team_candidate_cache_ttl`。

**fetch_url** — `app`

`fetch_url_enabled`, `fetch_url_timeout`, `fetch_url_max_content_length`, `fetch_url_max_download_size`, `fetch_url_domain_policy`, `fetch_url_domain_list`, `fetch_url_force_https`, `fetch_url_allowed_content_types`, `fetch_url_max_redirects`。


### AI 与模型

**rag** — `app`

`enable_rag`, `chroma_persist_dir`。

**embedding** — `app`

`embedding_model`, `embedding_provider`, `embedding_base_url`, `embedding_api_key`, `embedding_dimension`。

**rerank** — `app`

`rerank_model`, `rerank_provider`, `rerank_base_url`, `rerank_api_key`, `rerank_score_threshold`。

**ai_strategy** — `app`

`ai_api_timeout_seconds`, `ai_api_max_retries`, `ai_api_initial_retry_delay_seconds`, `ai_api_total_timeout_seconds`, `ai_fallback_enabled`, `ai_fallback_max_candidates`, `ai_fallback_sticky_candidate`, `enable_context_compression`, `context_compression_threshold`, `activity_reasoning_capture_enabled`, `activity_request_response_capture_enabled`, `activity_reasoning_provider_allowlist`, `activity_reasoning_protocol_allowlist`, `activity_artifact_retention_days`, `activity_artifact_encryption_key_id`, `activity_artifact_super_admin_read_enabled`。

**bindings** — `binding`

`main`, `summary`, `agent_team`。


### Issue 分析

**issue_analysis** — `app`

`enable_issue_analysis`, `issue_auto_assign`, `issue_auto_rewrite_title`, `issue_assignee_confidence_threshold`, `issue_auto_assign_max`, `issue_detect_duplicates`, `issue_relation_max_candidates`, `issue_relation_max_input_tokens`, `issue_relation_candidate_max_comments`, `issue_relation_candidate_comment_max_chars`, `issue_relation_similarity_threshold`, `issue_relation_confidence_threshold`, `issue_duplicate_confidence_threshold`, `issue_suggest_assignees`, `issue_suggest_milestones`, `issue_max_files_per_analysis`, `max_concurrent_issues`, `issue_vector_store_rich_metadata`, `issue_corpus_freshness_seconds`, `issue_corpus_batch_size`, `issue_candidate_pool_multiplier`, `issue_include_comments`, `issue_vision_enabled`。

**strategy.issue_analysis** — `section`

`categories`, `priority_rules/critical/keywords`, `priority_rules/high/keywords`, `priority_rules/medium/keywords`, `priority_rules/low/keywords`, `issue_reference_keywords`, `max_linked_issues_in_prompt`, `system_prompt`, `comment_template`, `comment_template_en`。


### 标签与扫描

**repo_scan** — `app`

`enable_repo_scan`, `scan_interval_minutes`, `scan_cooldown_hours`, `scan_min_severity_for_issue`。

**strategy.scan** — `section`

`system_prompt`。

**label.definitions** — `section`

`bug/color`, `bug/description`, `documentation/color`, `documentation/description`, `duplicate/color`, `duplicate/description`, `enhancement/color`, `enhancement/description`, `good first issue/color`, `good first issue/description`, `help wanted/color`, `help wanted/description`, `invalid/color`, `invalid/description`, `question/color`, `question/description`, `wontfix/color`, `wontfix/description`, `refactor/color`, `refactor/description`, `performance/color`, `performance/description`, `test/color`, `test/description`, `dependencies/color`, `dependencies/description`, `ci/color`, `ci/description`, `style/color`, `style/description`, `build/color`, `build/description`, `label.definitions`。

**label.recommendation** — `section`

`enabled`, `confidence_threshold`, `auto_create`。

**label.conflict_rules** — `section`

`enhancement`, `refactor`, `documentation`, `test`, `style`, `label.conflict_rules`。


### 系统与集成

**i18n** — `app`

`default_language`, `output_language`。

**payment** — `app`

`payment_enabled`, `payment_order_expire_minutes`, `payment_default_currency`, `stripe_enabled`, `stripe_api_key`, `stripe_webhook_secret`, `stripe_currency`, `paddle_enabled`, `paddle_api_key`, `paddle_webhook_secret`, `paddle_currency`, `paddle_vendor_id`, `alipay_enabled`, `alipay_app_id`, `alipay_private_key`, `alipay_public_key`, `alipay_currency`, `alipay_sandbox`, `nowpayments_enabled`, `nowpayments_api_key`, `nowpayments_ipn_secret`, `nowpayments_pay_currency`, `tron_enabled`, `tron_wallet_address`, `tron_api_key`。

**registration_quota** — `app`

`register_quota_multiplier`。

**init_quota** — `app`

`init_admin_daily_quota`, `init_admin_weekly_quota`, `init_admin_monthly_quota`, `init_user_daily_quota`, `init_user_weekly_quota`, `init_user_monthly_quota`, `init_user_issue_daily_quota`, `init_user_issue_weekly_quota`, `init_user_issue_monthly_quota`, `init_admin_agent_daily_quota`, `init_admin_agent_weekly_quota`, `init_admin_agent_monthly_quota`, `init_user_agent_daily_quota`, `init_user_agent_weekly_quota`, `init_user_agent_monthly_quota`。

**star_aid** — `app`

`star_aid_enabled`, `star_aid_auto_star_enabled`, `star_aid_scheduler_enabled`, `star_aid_min_interval_minutes`, `star_aid_max_interval_minutes`, `star_aid_batch_size`, `star_aid_user_daily_limit`, `star_aid_repo_daily_limit`, `star_aid_summary_enabled`, `star_aid_summary_language`。

**database** — `app`

`database_url`, `redis_url`。

**github_app** — `app`

`github_app_id`, `github_private_key`, `github_webhook_secret`。

**github_oauth** — `app`

`github_oauth_client_id`, `github_oauth_client_secret`, `github_oauth_redirect_uri`, `mobile_oauth_allowed_redirect_uris`。

**star_aid_app** — `app`

`star_aid_github_app_client_id`, `star_aid_github_app_client_secret`, `star_aid_github_app_slug`, `star_aid_github_app_callback_url`, `star_aid_github_app_discovery_timeout_seconds`。

**telegram** — `app`

`telegram_enabled`, `telegram_bot_token`, `telegram_bind_token_expire_seconds`。

**email** — `app`

`email_enabled`, `smtp_host`, `smtp_port`, `smtp_username`, `smtp_password`, `smtp_from`, `smtp_from_name`, `smtp_security`。

**notifications** — `app`

`notification_max_concurrency`, `notification_retry_max_attempts`, `notification_retry_initial_delay_seconds`, `notification_retry_backoff_factor`, `notification_rate_limit_seconds`。

**application** — `app`

`app_domain`, `app_port`, `app_timezone`, `log_level`, `webui_secret_key`, `activity_cursor_signing_secret`, `bot_username`。


### 个人设置

**preferences** — `preference`

`language`, `items_per_page`, `theme`, `output_language`。
