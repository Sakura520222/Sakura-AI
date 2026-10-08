# Agent Harness 2.0 Implementation Plan and Acceptance Ledger

> Final scope (2026-10-08): all mandatory Phase 1–6 runtime features and extra Skills/compaction/recovery requirements are implemented and locally verified. WIP `656c7b65` and Phase1/2 checkpoint `30c4152f` retain history. The same worktree is retained. Local commits only; no push, PR, merge or issue closure. Exact evidence and deployment limits: `docs/AGENT_HARNESS_2_ACCEPTANCE.md`.

**Full goal (unchanged):** implement every mandatory requirement of [Issue #628](https://github.com/Sakura520222/Sakura-AI/issues/628). **Delivery:** integrated runtime, configuration, bilingual guides, behavioral tests and independent review; production deployment remains outside local evidence.
**Baseline:** origin/develop `38a021934461bae4932928c3f466df6d47ddc2b8`, fetched 2026-10-04; clean source checkout.
**Spec:** Issue #628 body, fetched with its sole comment and full timeline. No linked PRs. The bot comment is investigative context, not specification or current-code evidence.
**Architecture:** retain the existing Agent, worker, checkpoint, runner, Skills and network services. Add runtime-owned scheduling, repository context, orchestration, capability evaluation and trusted plugin adapters. Only the runner chooses infrastructure parameters. Repository data never grants authority.
**Stack:** Python 3.14, asyncio, FastAPI, SQLAlchemy, existing model-provider abstraction and sandboxd v2.

## Global constraints

- Latest user contract (2026-10-05): no model-round, total-tool-call or task-step budgets, no call-count termination for subagents, no threshold-forced completion or cheaper-model switch. All nine introduced execution/repository quotas have been removed without hidden replacements. Detect repeated no-progress behavior and request autonomous strategy self-checks; detection is not a workload cutoff. This overrides the earlier once-reminded text termination and bounded-read-count proposals in the Issue.

- User clarification (2026-10-04): Agent operation is fully autonomous. Capabilities are automatically granted or denied by system policy; never add per-tool, dependency, MCP or subagent manual approval/authorization gates. Existing administrator configuration defines policy, not runtime human approval. Preserve #604 unattended execution.
- Further clarification: avoid a second authorization workflow around ordinary sandboxed operations. The capability layer unifies existing boundary checks, read-only delegation and external/MCP access. Reuse #604 execution-scoped egress/cleanup/audit; do not invent grant tokens, approval states or redundant admission handshakes for ordinary reads/writes/Shell.
- Preserve checkpoint/resume, queued human guidance, compression and Skills progressive disclosure.
- Settings remains the default source; dynamic values use app_config and super-admin WebUI. Update both translations and both READMEs.
- Keep #604 execution-scoped dependency egress and #627 retry/diagnostic/degradation behavior; do not widen global network access or add probes/proxies.
- Workspace boundaries, non-root/read-only-rootfs/cap-drop/no-new-privileges, secret isolation and runner ownership remain intact.
- Never trust model-supplied concurrency/permission/terminal metadata. Unknown and invalid configuration fails closed.
- Optional: CLAUDE.md compatibility. Future, outside this delivery: isolated writer subagents and example third-party integrations.
- Tests use real local runtime components with external services/model replies controlled. Live production/provider/Docker claims require separate observed evidence.

## Task 1 — Completion, scheduler and durable recovery

- [x] Latest contract verification: pure text receives completion reminders and cannot claim success; repeated identical work triggers a nonterminal strategy self-check, with no cumulative execution budgets or text-response cutoff.
- [x] Only successful finish_task admits success; cancelled, blocked and unrecoverable_error are distinct runtime/session outcomes. Compatible task statuses retain precise current_phase/reasons. Forged terminal outputs are rejected.
- [x] Tool metadata is runtime-owned. Safe read batches preserve result order without an artificial read-count cap; writes, shell, Git mutations and finish use writer-preferring event-loop barriers plus advisory directory locks shared by cooperating processes on the same filesystem. This is not distributed task ownership.
- [x] Persist running/completed/failed/cancelled states with locked sequence allocation and atomic results; propagate cancellation and drain actual mutation work.
- [x] Resume checks terminal/tool consistency and atomic legacy migration; safe reads may retry, uncertain mutations do not blindly replay.
- [x] Completion, scheduling, cancellation, guidance, resume, no-progress and local browser checks passed; exact commands/limits are recorded in `docs/AGENT_HARNESS_2_ACCEPTANCE.md`.

Implementation surface: fullstack_expert.py, tools/base.py, tool_scheduler.py, finish_task_tool.py, conversation_checkpoint.py, iteration_loop.py, worker/model status projections and focused tests. Keep policy/hook extension points in the executor for later phases.

## Task 2 — Repository instructions and progressive Skills

- [x] Honor agent_team_skills_enabled during discovery, model context/schema projection, cached/direct use_skill calls and resume; independent repository instructions continue working. Runtime changes must not create approval steps.
- [x] Persist runtime-owned historical Skill restriction ceilings with tool/checkpoint results. Resume intersects historical and current restrictions; changed, removed or corrupt metadata cannot expand them. End-workflow cleanup restores only existing runtime access.
- [x] Read current root/ancestor AGENTS.md, optional CLAUDE.md and Sakura rules for every schema-supported path/path array. Replace and deduplicate the relevant scope snapshot; refresh updates/deletions/resume; remove all five introduced repository byte/count quotas without replacement; never retain obsolete sibling rules.
- [x] Preserve existing DB Skill size contracts (installation code currently permits 512 KiB per file) and actual documented/tested selectors such as Shell(git status) and Bash(git status:*). Unsupported selectors fail explicitly, never silently widen. Repository reads have no newly introduced size/count quotas; the pre-existing DB install/read contract remains unchanged.
- [x] Separate metadata discovery, directory listing and explicit body loading. Validate freshness/content cache invalidation, secure descriptors and explicit errors with behavioral tests.
- [x] Synchronize existing Skill settings and removal of all nine quota controls in both locales and user docs, explaining switch, priority, scope, recovery and progressive loading. Verify actual configuration-page interaction.
- [x] Run focused RED/GREEN, affected-chain regressions, Ruff and diff checks; perform one Phase 2 scoped independent review and evidence-backed fixes. Update the acceptance report and commit locally; no unrelated feature work or replacement quotas.

Implementation surface: repository_context.py, skill_service.py, tools/use_skill_tool.py, fullstack_expert.py and focused tests. Prefer a new focused service over rewriting unrelated prompt modules.

## Task 3 — Read-only subagents

- [x] spawn_agent / wait_agent / cancel_agent use independent context and durable child sessions; return structured results.
- [x] Fixed runtime allowlist: safe read/search/diff/detect/web and policy-approved read-only MCP. No Shell, file writes, Git write/push or permission expansion.
- [x] Schedule live children within available concurrency (Issue requirement), with no cumulative spawn/model/tool budget or lifetime call-count termination. Child tools cannot recursively spawn writers; main remains sole writer. Shared workspace scheduler coordinates readers with parent writes.
- [x] Parent cancellation/shutdown recursively cancels and awaits children. Resume never silently abandons or duplicates active children.
- [x] Tests: concurrent isolated children, saturation, deny writes via every executor path, wait/cancel, model failures and parent cancellation.

Implementation surface: subagents.py, tools/subagent_tools.py, registry.py, fullstack_expert.py, checkpoint integration and tests.

## Task 4 — MCP runtime

- [x] Administrator-managed server configuration; at least one working standards-based transport and real local protocol integration test.
- [x] Initialize/list/call lifecycle, I/O idle timeouts, collision-safe namespaces and semantics-preserving schema validation. No new discovery/output count or size caps; SDK discovery has its own documented protocol probe deadline.
- [x] Server advertisement does not grant permission: explicit administrator tool policy, runtime capability/network/workspace/secret checks before visibility and each execution.
- [x] Credentials remain transport-only and never enter model context/checkpoints/logs; no repository-controlled servers or host commands.
- [x] Fail closed per unavailable server/tool while preserving core tools; audit discovery, invocation, deny, failure and cancellation.
- [x] Tests: actual protocol server, malformed schemas/results, unavailable server, revoked authorization, secret redaction, network denial and cancellation.

Implementation surface: mcp_runtime.py, plugin configuration schema/service, executor/registry integration and tests. Do not introduce host stdio execution outside the sandbox.

## Task 5 — Unified capability engine

- [x] Capability schema covers filesystem, shell, web/egress, dependency, git/github, MCP, subagent and completion operations.
- [x] Profiles read_only/workspace_write/autonomous/full_access; full_access requires trusted administrator selection. Intersect network policy, subagent restrictions and skill constraints.
- [x] Model may request an abstract capability only. Task/execution temporary grants expire in finally on success/error/cancel and are not restored from repository/model/checkpoint claims.
- [x] Both schema visibility and every execution entry point enforce current policy; host/runner infrastructure remains server-owned.
- [x] Grant/deny/revoke audit contains identifiers and decisions, no commands/secrets. Existing worker GitHub publication also respects applicable task policy.
- [x] Tests: matrix, profile downgrade/revocation, forged contexts, unknown capabilities, grant cleanup, #604/#627 regressions.

Implementation surface: capability_policy.py, tools/base.py, execution/network adapters, worker admission/publication, Settings/dynamic config and tests. Earlier phases may introduce this shared foundation as required by dependencies; acceptance remains tracked here.

## Task 6 — Hooks, plugin management and integrated delivery

- [x] System hooks session_start/before_model/after_model/before_tool/after_tool/before_write/after_write/before_finish/after_finish/task_failed/task_cancelled.
- [x] Trusted configuration selects sandboxed formatter/lint/test/repository-validation hooks with bounded execution and current policy. Repositories may supply data, never execution grants or host credentials.
- [x] Failed required before_finish hooks veto success; after hooks cannot erase failures. Sanitize/audit hook outputs and propagate cancellation.
- [x] Unified Skills/MCP/Hooks plugin abstraction with super-admin WebUI management, strict validation, authorization and bilingual copy.
- [x] Compression audit records before/after token estimates, retained active task/recent tools/unresolved errors without upgrading untrusted content.
- [x] Unit/integration/local E2E tests, real browser interaction, related regressions, full pytest, Ruff, diff/scope review.
- [x] Configuration/deployment/user documentation and requirement→implementation→executed-test evidence matrix.

Implementation surface: lifecycle_hooks.py, plugins.py, config routes/templates, translations, compression/checkpoint integration, documentation and focused/browser tests.

## Evidence and status

Baseline inspection on 2026-10-04 found pure-text success, serial execution and a static registry. #604 and #627 are already integrated in baseline; preserve their APIs. The acceptance record now contains the final executed commands and outcomes; reported suites overlap.

| Phase | Implementation | Verification | State |
|---|---|---|---|
| 1 | Explicit completion, durable checkpoints, nonterminal self-check, workspace barriers and directory locks | Runtime and real cross-process/cancellation tests; final suite below | Verified in the documented scope |
| 2 | Repository rules, lazy Skills, fresh scope and durable historical restrictions | Behavioral regressions and bilingual browser controls | Verified; committed checkpoint 30c4152f |
| 3 | Readonly child sessions, queue, wait/cancel/results, idempotent usage | SQLite and real process/file tests; corrected descriptor-safe search; independent review PASS | Verified |
| 4 | Official MCP SDK/HTTP, dynamic registry, policy and secrets | Real TCP modern/legacy tests, SDK logging/schema repairs and independent re-review PASS | Verified locally; no paid-provider claim |
| 5 | Runtime/external control-plane capabilities, profiles, #604 integration | Fresh-policy/network narrowing and teardown regressions; independent review PASS | Verified |
| 6 | Eleven hooks, durable effects, plugin/config/API/UI, compaction audit | Real formatter/cancellation, browser, SQLite+TCP+child+hook E2E and independent review PASS | Verified locally |

Final combined command: task-local Python `-m pytest tests updater/tests sandboxer/tests -q -rs -p no:cacheprovider --tb=short`: **5166 passed, 17 skipped**. Installing optional aiosqlite only into the isolated test environment then running `tests/test_legacy_placeholder_atomicity.py` produced **3 passed**, including the formerly skipped case. Sixteen host-gated tests remain skipped. Required Ruff and both diff checks passed. Deployment, MySQL competition, live Docker, macOS and arm64 hardware are not inferred from these results. See the acceptance matrix for exact commands, requirements and limits.
