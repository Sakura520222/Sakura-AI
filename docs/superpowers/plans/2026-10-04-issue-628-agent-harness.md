# Agent Harness 2.0 Implementation Plan and Acceptance Ledger

> Current scope (latest user instruction 2026-10-05): an active goal now covers the complete mandatory Agent Harness 2.0 delivery. Finish and commit the current Phase 2 unit, then continue Phases 3–6 by dependency. WIP checkpoint `656c7b65` preserves the prior work; Phase 5 foundation is not yet connected. Keep the same worktree. Local commits only; no push, PR, merge or issue closure.

**Full goal (unchanged):** implement every mandatory requirement of [Issue #628](https://github.com/Sakura520222/Sakura-AI/issues/628). **Current work:** finish Phase 2's known gaps and independent review, update evidence and commit locally, then continue the remaining mandatory phases.
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
- [x] Tool metadata is runtime-owned. Safe read batches preserve result order without an artificial read-count cap; writes, shell, Git mutations and finish use event-loop-local workspace barriers shared across executors. No cross-process exclusion claim.
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

- [ ] spawn_agent / wait_agent / cancel_agent use independent context and durable child sessions; return structured results.
- [ ] Fixed runtime allowlist: safe read/search/diff/detect/web and policy-approved read-only MCP. No Shell, file writes, Git write/push or permission expansion.
- [ ] Schedule live children within available concurrency (Issue requirement), with no cumulative spawn/model/tool budget or lifetime call-count termination. Child tools cannot recursively spawn writers; main remains sole writer. Shared workspace scheduler coordinates readers with parent writes.
- [ ] Parent cancellation/shutdown recursively cancels and awaits children. Resume never silently abandons or duplicates active children.
- [ ] Tests: concurrent isolated children, saturation, deny writes via every executor path, wait/cancel, model failures and parent cancellation.

Implementation surface: subagents.py, tools/subagent_tools.py, registry.py, fullstack_expert.py, checkpoint integration and tests.

## Task 4 — MCP runtime

- [ ] Administrator-managed server configuration; at least one working standards-based transport and real local protocol integration test.
- [ ] Initialize/list/call lifecycle, bounded discovery/output/timeouts, namespace collision prevention and schema normalization/validation.
- [ ] Server advertisement does not grant permission: explicit administrator tool policy, runtime capability/network/workspace/secret checks before visibility and each execution.
- [ ] Credentials remain transport-only and never enter model context/checkpoints/logs; no repository-controlled servers or host commands.
- [ ] Fail closed per unavailable server/tool while preserving core tools; audit discovery, invocation, deny, failure and cancellation.
- [ ] Tests: actual protocol server, malformed schemas/results, unavailable server, revoked authorization, secret redaction, network denial and cancellation.

Implementation surface: mcp_runtime.py, plugin configuration schema/service, executor/registry integration and tests. Do not introduce host stdio execution outside the sandbox.

## Task 5 — Unified capability engine

- [ ] Capability schema covers filesystem, shell, web/egress, dependency, git/github, MCP, subagent and completion operations.
- [ ] Profiles read_only/workspace_write/autonomous/full_access; full_access requires trusted administrator selection. Intersect network policy, subagent restrictions and skill constraints.
- [ ] Model may request an abstract capability only. Task/execution temporary grants expire in finally on success/error/cancel and are not restored from repository/model/checkpoint claims.
- [ ] Both schema visibility and every execution entry point enforce current policy; host/runner infrastructure remains server-owned.
- [ ] Grant/deny/revoke audit contains identifiers and decisions, no commands/secrets. Existing worker GitHub publication also respects applicable task policy.
- [ ] Tests: matrix, profile downgrade/revocation, forged contexts, unknown capabilities, grant cleanup, #604/#627 regressions.

Implementation surface: capability_policy.py, tools/base.py, execution/network adapters, worker admission/publication, Settings/dynamic config and tests. Earlier phases may introduce this shared foundation as required by dependencies; acceptance remains tracked here.

## Task 6 — Hooks, plugin management and integrated delivery

- [ ] System hooks session_start/before_model/after_model/before_tool/after_tool/before_write/after_write/before_finish/after_finish/task_failed/task_cancelled.
- [ ] Trusted configuration selects sandboxed formatter/lint/test/repository-validation hooks with bounded execution and current policy. Repositories may supply data, never execution grants or host credentials.
- [ ] Failed required before_finish hooks veto success; after hooks cannot erase failures. Sanitize/audit hook outputs and propagate cancellation.
- [ ] Unified Skills/MCP/Hooks plugin abstraction with super-admin WebUI management, strict validation, authorization and bilingual copy.
- [ ] Compression audit records before/after token estimates, retained active task/recent tools/unresolved errors without upgrading untrusted content.
- [ ] Unit/integration/local E2E tests, real browser interaction, related regressions, full pytest, Ruff, diff/scope review.
- [ ] Configuration/deployment/user documentation and requirement→implementation→executed-test evidence matrix.

Implementation surface: lifecycle_hooks.py, plugins.py, config routes/templates, translations, compression/checkpoint integration, documentation and focused/browser tests.

## Evidence and status

Initial code inspection confirms pure-text success, serial execution and static registry still exist. #604 and #627 are already integrated in baseline; preserve their APIs. Phase execution evidence will be appended here only after commands run.

| Phase | Implementation | Verification | State |
|---|---|---|---|
| 1 | Completion/scheduler/checkpoint/worker/resume UI; latest unlimited execution/self-check correction in progress | Latest affected regression 938 passed/1 prerequisite skip; nonterminal self-check, cancellation, resume, Ruff and scoped review passed | Verified in one process/event loop |
| 2 | Switch, durable Skill ceilings, refreshed rules, selector compatibility and on-demand loading connected | 938 passed/1 prerequisite skip; two review PoCs fixed, 112-test independent recheck passed; bilingual real config browser/CSRF verified | Verified; local checkpoint before Phase 3 |
| 3 | Pending | Pending | Pending |
| 4 | Pending | Pending | Pending |
| 5 | Standalone capability schema/profile/direct boundary checks; not integrated | 30 foundation unit tests pass; not phase acceptance | Partial; expansion stopped |
| 6 | Pending | Pending | Pending |
