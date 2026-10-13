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

`emit_event(session, ...)` never commits its caller's transaction or sends externally. Event identity hashes `(type, resource type, resource ID, run key, audience)`; database uniqueness handles simultaneous callbacks. Replays return the original event and snapshot. PR/Issue/scan runs have distinct record/version identities; Agent identities include resume and iteration counters. A new execution round produces a new event.

Application sessions opt into `notification_business` through `session.info['notification_outbox']`. ORM status changes are collected before flush and their events are inserted after flush on the same connection, before commit. Rollback removes both state and events. Migration/import sessions do not opt in, so reading or restoring historical records does not replay old business notifications. Bulk SQL writers explicitly persist their events before commit: Issue completion/failure/cancellation uses `emit_task_event`, sharing the ORM adapter's identity and result metadata, while quota exhaustion uses `emit_event`. Conditional updates emit only after winning the transition; Issue close locks the active owner-scoped rows and emits a cancellation for each version in the same transaction. Task-event persistence failures roll back the transition before control returns to a worker's error handler, preventing a later cleanup commit from retaining state without its event.

The existing runtime supervisor owns one bounded polling dispatcher. Event fanout is transactional, takes a database row lock with `SKIP LOCKED`, and creates inbox and delivery rows using database uniqueness. A crashed fanout rolls back completely. Pending infrastructure failures persist their category and next execution time.

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

## States, recovery and throttling

| Category | Outcome |
|---|---|
| `ineligible` | Cancelled/skipped; no external send and no automatic reactivation |
| `infrastructure` | Pending with durable retry time; no provider attempt consumed for an unknown eligibility result |
| `transient` | Pending until the configured attempt budget is exhausted |
| `rate_limit` | Durable Retry-After; Telegram/provider cooldown or independent Webhook endpoint cooldown |
| `configuration`, `credential`, `invalid_target`, `security`, `verification` | Terminal failure; correction and a controlled administrator retry are required |
| `exhausted` | Terminal failure after the configured attempt budget |

The provider budget table is the deployment-wide start/cooldown authority, with microsecond MySQL precision. Independent services/processes share its atomic update. Webhook Retry-After is endpoint-scoped so one user-controlled receiver cannot pause unrelated destinations; the deployment-wide start interval still applies. Legacy pending announcements use the same budget and Provider registry. In-memory throttling exists only for lightweight compatibility adapters without an application database. The poller does not wait for a legacy active lease or busy-loop on an absent endpoint.

Settings defaults are the only source of defaults. All `notification_*` controls are in `app_config` and the existing system configuration page. Recovery batches, concurrency, polling, timeouts, retry budget/backoff, response size and verification limits are configurable there.

## Endpoint and Webhook operations

Personal Settings provides endpoint names, enabling/disabling, soft unbinding, tests, status inspection, Email verification codes, Webhook verification/credential replacement and subscriptions. Multiple verified Email endpoints are independent; GitHub primary-email synchronization retires previous OAuth-managed addresses and preserves separately verified user-managed addresses. Telegram uses its existing one-time private-chat binding handshake.

The existing Announcement Center includes business inbox history/read markers and super-admin provider status, failure categories, statistics, controlled retries and audit records. Announcement rows are excluded in SQL before business-history pagination; legacy announcement reading remains independent. Agent details link to `/agent-team/?task_id=<id>`, validate the current task scope on entry and select the task in the existing console. Tests use the normal persisted event pipeline and the same permission/network policies. Users can inspect only their own endpoint delivery statuses. No channel gets an extra top-level navigation page.

Webhook endpoints require HTTPS on port 443, no URL credentials/query/fragment, and optional administrator-controlled host allowlisting. All DNS answers must be public; private/loopback/link-local/reserved/multicast/IPv4-mapped/transition addresses are denied. Validated addresses are pinned to the actual connection while TLS verifies the original host. Environment proxies and redirects are disabled. DNS, transport and response reads are bounded; response bodies never enter diagnostics. Internal targets are deliberately unsupported.

Credentials use the existing Fernet secret service, are stored separately from addresses/metadata, and are never returned by endpoint or diagnostics APIs. Replacing a signing key invalidates verification. Webhook ownership verification sends a signed random challenge and requires the receiver to echo `{"challenge":"..."}`.

Webhook messages are JSON with ID, type, resource/repository identity, occurrence time, run key, title, safe content, severity, relative details path and minimal payload. Email/Telegram messages link to the details path when the deployment domain is configured. Headers:

- `X-Sakura-Event-ID`: stable event ID
- `X-Sakura-Timestamp`: UTC Unix seconds
- `X-Sakura-Signature`: `sha256=<HMAC-SHA256(secret, timestamp + '.' + raw_body)>`

`verify_webhook_signature` authenticates raw bytes and rejects timestamps outside the replay window. Receivers additionally deduplicate event IDs. Private-network allowlisting cannot be bypassed by ordinary users or endpoint test sends.

## Migration and rollout

Stop all application instances before upgrading; do not mix old and new senders during schema migration. Back up the database and retain the deployment encryption key. The normal auto-migrator adds event/inbox/subscription/budget/audit tables and endpoint/delivery metadata, makes announcement identity optional, and replaces the old user/channel uniqueness with endpoint/round and event/endpoint uniqueness. MySQL/PostgreSQL changes are in-place and idempotent. SQLite is a test dialect; its copy/swap upgrade requires foreign-key enforcement off during migration.

Historical delivery rows keep their IDs, statuses, attempts, publication versions and `event_id=NULL`. They are neither copied into new pending events nor automatically rebroadcast. Current legacy pending rows still recover through the compatibility path; failed/sent/archived rows remain historical. Existing announcement content snapshots and read markers stay intact, and new archived states retain endpoint/event identities.

User backups include endpoint names, configuration versions and encrypted Webhook credentials. Imported Webhooks require a fresh ownership challenge. If the receiving deployment uses a different encryption key, replace the endpoint's signing credential and verify it again; plaintext credentials are not exported. Database/Redis connection settings retain the existing backup-restore protection.

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
