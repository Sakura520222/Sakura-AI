# Unified notification center / 统一通知中心

Implemented for [Issue #646](https://github.com/Sakura520222/Sakura-AI/issues/646), using the existing notification endpoint table, delivery table, Provider registry and announcement lifecycle. There is no new authentication system, Telegram business sender or message broker.

## Architecture

```text
Business state transaction
  └─ notification_events (immutable summary + stable business/run identity)
        └─ resource ACL + current subscriptions
              ├─ user_notifications (one inbox/read marker per user/event)
              └─ notification_deliveries (one exact external endpoint/event)
                    └─ existing Provider registry
                          ├─ WebUI
                          ├─ SMTP Email
                          ├─ Telegram private chat
                          └─ signed HTTPS Webhook
```

`emit_event(session, ...)` never commits its caller's transaction or sends externally. Event identity hashes `(type, resource type, resource ID, run key, audience)`; database uniqueness handles simultaneous callbacks. Replays return the original event and snapshot. PR/Issue/scan runs have distinct record/version identities. Agent identities include a persisted execution round plus the existing resume and iteration counters. Queue admission advances the round under a database row lock in the task transaction; duplicate pending admissions share it. A full retry that fails before skills/cloning or its first iteration still produces a new failure event. Legacy round zero retains the former key format. A new execution round produces a new event.

Application sessions opt into `notification_business` through `session.info['notification_outbox']`. ORM status changes are collected before flush and their events are inserted after flush on the same connection, before commit. Rollback removes both state and events. Migration/import sessions do not opt in, so reading or restoring historical records does not replay old business notifications. Bulk SQL writers explicitly persist their events before commit: Issue completion/failure/cancellation uses `emit_task_event`, sharing the ORM adapter's identity and result metadata, while quota exhaustion uses `emit_event`. Conditional updates emit only after winning the transition; Issue close uses UPDATE RETURNING where supported and a locked active snapshot on MySQL, emitting a cancellation for each transitioned owner-scoped version in the same transaction. Task-event persistence failures roll back the transition before control returns to a worker's error handler, preventing a later cleanup commit from retaining state without its event.

The existing runtime supervisor owns one bounded polling dispatcher. Event fanout is transactional, takes a database row lock with `SKIP LOCKED`, and creates inbox and delivery rows using database uniqueness. A crashed fanout rolls back completely. Pending infrastructure failures persist their category and next execution time.

Fanout filters event families and scope/severity matches in SQL, deduplicates target tuples and groups routing by user. Disabled or nonmatching scoped rules still suppress compatibility defaults. Users/endpoints are fetched in configured batches; the resource snapshot is reused only within that transaction. User shared locks precede endpoint locks, preserving the lock order with serialized preference writes and foreign-key checks. Endpoint row locks serialize fanout with disabling, so disabling also cancels rows produced by a concurrent fanout before that transaction can finish. Sending and history still recheck current access independently. Repeated subscription writes serialize on the user's row and update the existing event/scope/endpoint rule rather than creating duplicates; existing duplicate rules for that scope are consolidated on update.

Delivery claims use conditional SQL updates and bounded leases. Every worker refreshes account status, the exact endpoint owner/enabled/verified state, resource access and routing preferences before sending. A final lease fence persists the attempt count before external I/O. A heartbeat renews slow sends; result updates require the same token. Announcement sends also require the original publication version to remain published.

External delivery is retryable with internal deduplication. A receiver may have accepted a message when the process loses the acknowledgement, so end-to-end exactly-once delivery is not promised. Webhook receivers should deduplicate `X-Sakura-Event-ID`.

## Business events and audiences

| Source | Meaningful transitions | Eligible recipients |
|---|---|---|
| PR Review / Issue Analysis | started, completed, failed, cancelled | Record author/initiator, repository owner and authorized subscribers |
| Repository scan | started, completed, failed, cancelled | Current administrators with matching routing; repository ownership alone grants no access |
| Agent | started, completed, failed, cancelled, action required | Task initiator (`started_by`) and authorized administrators; resume/iteration identity is preserved |
| Announcement / release announcement | publication round | Active users, with compatibility defaults and current preferences |
| Account security / Passkey / MFA | account change, lockout | The affected account's private verified endpoints and inbox |
| Quota | exhaustion, deduplicated per kind/period/reset/limit | The affected account |
| Payment | fulfillment or expiration/failure | Current order owner |
| Refund | request, completion, rejection, execution failure | Current order/request owner; explicit administrator events for pending requests/failures |

Permissions follow the current WebUI resource scopes. PR/Issue access recognizes repository owners, record authors and administrators. Scans require a current administrator role. Agent tasks require the current `started_by` initiator or an administrator, including organization-owned repositories; repository ownership does not grant access to someone else's Agent task. These checks precede generic repository eligibility and run again before delivery and history reads. Subscriptions never confer access. Legacy repository subscriptions are resolved by joining to the internal user ID; Telegram IDs are never notification recipient IDs. No missing-recipient fallback broadcasts repository events to all administrators. An event with no legal recipient records `no_recipients`.

Severity uses business result metadata: critical scan findings/critical Issues are `critical`; major findings, high-priority Issues, requested PR changes and human action requests are `warning`; execution failures are `error`. Minimal payloads include result classification and business issue/PR numbers, without private findings or operational stack traces.

Business events default to the user's inbox. External endpoints require a matching subscription. Existing announcement routing remains enabled by default for verified enabled endpoints; setting announcement preferences replaces that default. Rules select event/family, repository, resource type/ID, minimum severity and one explicit endpoint (NULL means WebUI). Disabled rules provide explicit opt-out. Current rules and ACLs are checked again before sending/retrying; ACLs are also checked when reading history. Announcement preferences affect unread prompts while the full announcement archive remains accessible.

Security, payment, refund and quota types are intrinsically sensitive even if a producer omits the flag. Sensitive events cannot use an all-user audience and do not use Webhooks or shared targets. Telegram remains private-chat only. Ownership uniqueness for private addresses is retained.

MFA lockout state resides in Redis, so its lockout and the SQL event cannot share one transaction. A failure to persist that alert is logged without undoing the account lock. This does not redesign the existing authentication/lockout system.

`notification_lockout_rounds` stores notification-only generations and reset/window timestamps under a row lock. Concurrent Redis/fallback callbacks share one event identity; successful resets and expired windows admit a new generation, while superseded callbacks are ignored. Redis and the existing fallback still enforce authentication. SQL alert/reset persistence failures remain observable and do not undo primary authentication state. The standard additive migrator creates the table without replaying old alerts.

## States, recovery and throttling

Gateway expiration keeps the established `cancelled` order state and explicitly persists `payment.failed` in that transaction. User cancellation remains distinct. Confirmation, expiration, cancellation and refund writers lock and refresh the order before deciding its transition; repeated or competing callbacks cannot overwrite a committed terminal state. An expiration-event failure rolls back the order and payment log before propagating.

Scan retries advance a persisted `execution_round` under the scan row lock, including failure before indexing. Duplicate pending admissions share a round. Legacy round zero retains the record-ID key; later rounds include the generation. Agent retry admission retains its task lock through quota consumption and queue commit; the quota service supports a caller-owned successful transaction while preserving the default commit and denial behavior.

Scan status/outbox commit failures propagate after the worker's bounded database retries; a rolled-back terminal state is never treated as a successful update. Quota denial is already determined by its atomic SQL update: optional exhaustion-alert insert/commit failures roll back, emit a sanitized warning, and preserve the original denial reason and Agent orphan-task cleanup. The reason is captured before rollback can expire the user object. System configuration renders effective booleans as `true`/`false` and preserves numeric zero, so saving unrelated settings cannot silently enable environment-disabled Webhooks.

| Category | Outcome |
|---|---|
| `ineligible` | Cancelled/skipped; no external send and no automatic reactivation |
| `infrastructure` | Pending with durable retry time; no provider attempt consumed for an unknown eligibility result |
| `transient` | Pending until the configured attempt budget is exhausted |
| `rate_limit` | Durable Retry-After; Telegram/provider cooldown or independent Webhook endpoint cooldown |
| `configuration`, `credential`, `invalid_target`, `security`, `verification` | Terminal failure; correction and a controlled administrator retry are required |
| `exhausted` | Terminal failure after the configured attempt budget |

The provider budget table is the deployment-wide start/cooldown authority, with microsecond MySQL precision. Independent services/processes share its atomic update. Webhook Retry-After is endpoint-scoped so one user-controlled receiver cannot pause unrelated destinations; the deployment-wide start interval still applies. Legacy pending announcements use the same budget and Provider registry. In-memory throttling exists only for lightweight compatibility adapters without an application database. The poller does not wait for a legacy active lease or busy-loop on an absent endpoint.

Short provider-budget waits drain inside a bounded, lease-fenced worker. Recovery wakes at the earliest durable retry deadline and selects channels fairly. A bounded child lane of the same supervised dispatcher recovers legacy announcements using the same SQL claims/budgets; its network waits do not block business events. Final attempt-budget failures use `exhausted`, retaining only the normalized cause. Completion/error paths never overwrite SQL attempt counts from detached snapshots. MySQL event/delivery retry deadlines retain microseconds.

Announcement fanout locks lifecycle before event, matching publication/withdrawal ordering and preserving immutable archived outcomes. Routing is reread after each recipient batch is locked, using current locking reads on MySQL REPEATABLE READ. GitHub owner/author/initiator and legacy repository names match case-insensitively. Runtime endpoint retirement, backup restore, OAuth email rotation and Telegram rebinding cancel exact old endpoint queues; binding operations serialize on the user row.

Settings defaults are the only source of defaults. All `notification_*` controls are in `app_config` and `/settings-center/system`; `/system-config/` redirects there. Recovery batches, concurrency, polling, timeouts, retry budget/backoff, response size and verification limits use the shared typed fields and autosave/reset contract. Endpoint and subscription operations remain in the personal category with their dedicated authorization and verification flows; `/settings/` remains a compatible redirect.

## Endpoint and Webhook operations

Verification uses an immutable configuration snapshot across provider I/O. Its conditional SQL update checks ownership, provider, address, enabled state, configuration generation and encrypted credential. Credential replacement and disable/re-enable transitions advance the generation; restore cannot rewind it. A stale challenge cannot mark a replacement credential verified. Email code consumption, expiration/attempt checks and verification succeed in one conditional update, so replacing a code cannot be overwritten by an older verification. Imports clear pending codes.

GitHub identities compare case-insensitively in PR/Issue list/detail scopes and all Agent entry, list, detail, prompt, stream and task-operation scopes, matching notification authorization on both database dialects. Preference deletion uses the same user-before-rule lock order as updates and fanout.

Personal Settings provides endpoint names, enabling/disabling, soft unbinding, tests, status inspection, Email verification codes, Webhook verification/credential replacement and subscriptions. Multiple verified Email endpoints are independent; GitHub primary-email synchronization retires previous OAuth-managed addresses and preserves separately verified user-managed addresses. Telegram uses its existing one-time private-chat binding handshake.

Disabling or unbinding an endpoint atomically cancels its pending deliveries and clears their lease/retry state; re-enabling it does not replay that queue. Sent/failed history and other endpoints remain unchanged. Telegram activation requires the existing binding handshake, preserving the single active chat; the generic endpoint API can edit or disable the current binding but cannot reactivate a retired one.

The existing Announcement Center includes business inbox history/read markers and super-admin provider status, failure categories, statistics, controlled retries and audit records. Announcement rows are excluded in SQL before business-history pagination; legacy announcement reading remains independent. Agent details link to `/agent-team/?task_id=<id>`, validate the current task scope on entry and select the task in the existing console. Tests use the normal persisted event pipeline and the same permission/network policies. Users can inspect only their own endpoint delivery statuses. No channel gets an extra top-level navigation page.

Webhook endpoints require HTTPS on port 443, no URL credentials/query/fragment, and optional administrator-controlled host allowlisting. All DNS answers must be public; private/loopback/link-local/reserved/multicast/IPv4-mapped/transition addresses are denied. Validated addresses are pinned to the actual connection while TLS verifies the original host. Environment proxies and redirects are disabled. DNS, transport and response reads are bounded; response bodies never enter diagnostics. Internal targets are deliberately unsupported.

Creation validates all DNS answers before persisting an endpoint. Unsafe DNS is rejected without saving; DNS timeout/unavailability returns a retryable API failure. Verification and delivery revalidate independently and pin the admitted addresses, so a later DNS change cannot bypass the sending policy.

Credentials use the existing Fernet secret service, are stored separately from addresses/metadata, and are never returned by endpoint or diagnostics APIs. Replacing a signing key invalidates verification. Webhook ownership verification sends a signed random challenge and requires the receiver to echo `{"challenge":"..."}`.

Webhook messages are JSON with ID, type, resource/repository identity, occurrence time, run key, title, safe content, severity, relative details path and minimal payload. Email/Telegram messages link to the details path when the deployment domain is configured. Headers:

- `X-Sakura-Event-ID`: stable event ID
- `X-Sakura-Timestamp`: UTC Unix seconds
- `X-Sakura-Signature`: `sha256=<HMAC-SHA256(secret, timestamp + '.' + raw_body)>`

`verify_webhook_signature` authenticates raw bytes and rejects timestamps outside the replay window. Receivers additionally deduplicate event IDs. Private-network allowlisting cannot be bypassed by ordinary users or endpoint test sends.

## Migration and rollout

Stop all application instances before upgrading; do not mix old and new senders during schema migration. Back up the database and retain the deployment encryption key. The normal auto-migrator adds event/inbox/subscription/budget/audit tables and endpoint/delivery metadata, makes announcement identity optional, and replaces the old user/channel uniqueness with endpoint/round and event/endpoint uniqueness. MySQL/PostgreSQL changes are in-place and idempotent. SQLite is a test dialect; its copy/swap upgrade requires foreign-key enforcement off during migration.

Constraint ALTER statements use isolated table metadata, retaining the mapped schema's inline uniqueness declarations for later fresh table creation in the same process. Real database regressions verify both upgrade idempotency and creating a fresh delivery table after an upgrade.

Historical delivery rows keep their IDs, statuses, attempts, publication versions and `event_id=NULL`. They are neither copied into new pending events nor automatically rebroadcast. Current legacy pending rows still recover through the compatibility path; failed/sent/archived rows remain historical. Existing announcement content snapshots and read markers stay intact, and new archived states retain endpoint/event identities. Agent tasks and repository scans receive an additive `execution_round` column defaulting to zero; existing statuses and counters are preserved without generating historical events.

User backups include endpoint names, positive configuration generations and encrypted Webhook credentials. Endpoint `config_version` is a monotonic generation, independent of the Provider descriptor's `configuration_version` schema. Exports with generations above one remain importable, including repeated export/restore cycles; restoring an existing endpoint advances its current generation. Imported Webhooks require a fresh ownership challenge. If the receiving deployment uses a different encryption key, replace the endpoint's signing credential and verify it again; plaintext credentials are not exported. Database/Redis connection settings retain the existing backup-restore protection.

User restoration disables the ORM notification hook for its entire transaction, including flushes and passkey restoration, and restores the caller's exact session setting on success, failure or cancellation. Historical account/MFA/role changes cannot create security events or alert restored endpoints; subsequent live changes still use the outbox. Cancellation rolls back before the hook is restored.

## Verification and acceptance evidence

The tests cover the three reported recovery failures, transactional rollback, duplicate/new-round behavior, concurrent claims, lease expiration/cancellation, infrastructure failures, per-endpoint isolation, ACL revocation, subscriptions, sensitive-target rejection, credentials, CSRF, inbox ownership and old-schema upgrades. A two-process SQLite test verifies the shared provider budget. A real local TLS receiver tests the full signed Webhook event pipeline, challenge, credential errors, redirects, rate limiting and bounded response reads; it is not a third-party delivery claim.

```bash
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run python run_ruff.py --check
uv run pytest tests/test_notification_live_db_646.py -q  # isolated CI MySQL/PostgreSQL only
uv run --with playwright python scripts/validate_notification_ui_646.py
```

The dedicated notification CI workflow runs real MySQL and PostgreSQL transactions/concurrent producers/claims/schema upgrades and browser E2E against the real bilingual templates/JavaScript/API with fixture authentication. Browser artifacts contain Settings/history screenshots. Production SMTP, Telegram and remote Webhook recipients are not contacted by these tests.

Issue #646 acceptance maps to the recovery regressions; event/delivery and business integration tests; API/credential/SSRF/TLS tests; schema/legacy announcement tests; cross-process provider-budget test; database integration workflow; and browser E2E. Authentication redesign, Telegram management/login commands, public/shared Telegram targets, all possible providers and new middleware remain outside the scope.
