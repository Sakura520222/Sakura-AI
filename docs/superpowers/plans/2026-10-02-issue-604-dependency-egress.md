# Issue #604 Dependency Egress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Enable automatic per-execution dependency networking under the default Agent policy.

**Architecture:** Authorize a typed Backend capability against fresh policy. Reuse sandboxd v2 `none|egress` without changing its wire schema or Docker isolation. Keep policy UI and documentation aligned with actual capability availability.

**Tech Stack:** Python 3.14, asyncio, Pydantic, FastAPI, pytest, Docker.

**Spec:** `docs/superpowers/specs/2026-10-02-issue-604-dependency-egress-design.md`

## Global Constraints

- Work only in `/home/firefly/.codex/worktrees/issue-604-dependency-egress/Sakura-AI`.
- No commits, pushes, merges, deployments, or GitHub comments in this implementation.
- Preserve protocol v2 and strict `network_mode: none|egress`; no new wire fields.
- Preserve all OCI, credential, resource, workspace, cancellation and cleanup boundaries.
- No command whitelist, proxy, destination allowlist or manual dependency approval.
- Local execution remains source-only and requires `full_access`.
- Preserve existing #627 retries, diagnostic sanitization and fail-closed infrastructure handling.
- Use CodeGraph first when an index exists, with the worktree path explicitly supplied. Do not create an index.
- Run from this worktree using `UV_CACHE_DIR=/tmp/sakura-604-uv-cache UV_PROJECT_ENVIRONMENT=/home/firefly/Projects/Sakura-AI/.venv uv run --no-sync python -m pytest -o cache_dir=/tmp/sakura-604-pytest-cache ...`.
- Use apply_patch for edits. Shell writes outside the original root may need sandbox escalation; report the actual result.
- Never spawn helper/reviewer agents; the controller owns reviews.

### Task 1: Backend capability authorization and dependency bootstrap

**Files:**
- Modify: `backend/services/agent_team/network_policy.py`
- Modify: `backend/services/agent_team/execution.py`
- Modify: `backend/services/agent_team/sandbox_client.py`
- Modify: `backend/services/agent_team/tools/shell_tool.py`
- Modify: `backend/services/agent_team/git_workspace_service.py`
- Test: `tests/test_agent_network_policy.py`, `tests/test_agent_sandbox_client.py`, `tests/test_agent_team_git_workspace.py`, relevant execution/Shell tests, optional focused new test module.

**Interfaces:**
- Produce `NetworkCapability(StrEnum)` in network_policy.py: `NONE = 'none'`, `DEPENDENCY_EGRESS = 'dependency_egress'`.
- Extend `ExecutionRequest.network_capability: NetworkCapability = NetworkCapability.NONE`; validate unknown/type-invalid capabilities and prohibit applying them to trusted Git operations.
- Extend `network_mode_for_policy(policy, *, profile='agent', capability=NetworkCapability.NONE) -> str`, preserving the one-argument mapping. Dependency profile implies dependency capability; explicit dependency capability is denied under offline. Offline implicit Dependency remains none for compatibility; bootstrap skips offline.
- Shell `run_command` gains optional `network_capability` with the same two values. Dependency install/build/test resolution instructions explain how to request it. Omission remains ordinary offline Agent execution under web_tools.
- Capability/action remain Backend metadata; wire payload contains only existing v2 fields.

- [x] Add behavioral RED coverage, including the literal policy matrix:

```python
assert network_mode_for_policy('web_tools') == 'none'
assert network_mode_for_policy('web_tools', profile='dependency') == 'egress'
assert network_mode_for_policy('web_tools', capability='dependency_egress') == 'egress'
assert network_mode_for_policy('offline', profile='dependency') == 'none'
assert network_mode_for_policy('full_access') == 'egress'
```

Exercise real Shell request construction and sandbox wire serialization, not just this helper. Assert invalid capability fails before transport, explicit offline request is denied, dependency bootstrap reaches sandbox under web_tools, and the next ordinary Shell is offline. Capture fresh policy changes between executions.
- [x] Run focused tests and record expected failures before implementing.
- [x] Implement the matrix with strict enum validation, fresh authorization, observable denials, and preserved local restrictions. `allows_dependency_network` becomes true for web_tools/full_access, with local admission still using `allows_local_backend`.
- [x] Extend every sandbox client audit outcome with capability and a fixed action (`dependency_resolution` or `agent_command`), never command text. Use the admitted health digest when available. Cover success, nonzero, timeout, cancellation, policy/transport/cleanup failures and secret-bearing commands in audit tests.
- [x] Run affected test modules plus Ruff on changed Python files; self-review and write report with RED/GREEN evidence. Do not commit.

### Task 2: Configuration projection and aligned documentation

**Files:**
- Modify: `backend/webui/routes/config.py`, relevant Agent status template.
- Modify: `backend/webui/translations/zh-CN.yaml`, `backend/webui/translations/en.yaml`.
- Modify: `docs/CONFIGURATION.md`, `docs/DEPLOYMENT.md`, `README.md`, `README_EN.md`.
- Test: existing configuration/Agent status tests.

**Interfaces:**
- Consume Task 1's network policy helper and enum.
- Retain all existing `/config/agent-network-status` fields; add `agent_network_mode`, `dependency_network_mode`, and `dependency_egress_available`.
- Ordinary mode is the default policy mode; dependency mode is the implicit Dependency profile mode. Availability additionally requires a ready sandbox and advertised egress; local must never be represented as temporary sandbox egress. Failed status returns safe unavailable/false values for the new fields.

- [x] Add RED API tests for web_tools ready egress, absent egress, offline, full_access, local and unavailable state, preserving super-admin scope.
- [x] Run focused tests and record failures.
- [x] Implement additive response fields and render the distinction in the existing status panel. Preserve the meaning of ordinary backend readiness; dependency egress readiness is separately visible.
- [x] Update both locale descriptions and labels, both READMEs, configuration/deployment docs. Explain explicit Shell capability usage with examples for Python, Node, Rust, Go and JVM; no domain-specific allowlists; egress lasts for only this one-shot execution; package hooks share that egress; local requires full_access.
- [x] Run affected tests/Ruff and render/inspect the changed status UI if practical. Self-review and report RED/GREEN evidence. Do not commit.

### Task 3: Actual Docker lifecycle regression

**Files:**
- Modify: `sandboxer/tests/integration/test_docker_isolation.py` or add a focused sibling integration module.
- Test additions only unless a demonstrated lifecycle bug requires controller discussion.

**Interfaces:**
- Exercise actual `DockerRuntimeAdapter` with existing strict sandboxer `ExecutionRequest` and immutable runner digest.
- When covering Backend->sandbox mapping, use Task 1's authorization helper or Backend runner through real service transport; do not replace Docker with a mock.

- [x] Add a regression that executes ordinary Agent none, Dependency egress, explicit Agent dependency egress, and ordinary Agent none again under web_tools. Use a controlled endpoint or route-level proof plus connectivity; avoid package-provider probes. Assert each actual container disappears, and enforce non-root/read-only/cap-drop/no-new-privileges/workspace boundaries while egress is enabled.
- [x] Cover timeout and cancellation while egress is enabled, with cleanup observed through Docker, and no runtime options accepted from requests.
- [x] Run the tests with `SAKURA_SANDBOX_DOCKER_INTEGRATION=1` and the verified official image digest. An unavailable image/runtime is a failure to report, not a passing skip. Existing Docker isolation tests must also pass.
- [x] Run sandboxer unit tests and focused Backend tests together, then report exact commands/results and limitations. Do not commit.

## Final verification

- [x] Independent review for each task and final whole-change review; fix actionable regressions and re-review.
- [x] Full root pytest including updater/sandboxer, `ruff check .`, `python run_ruff.py --check`, `git diff --check`.
- [x] Confirm primary checkout remains unchanged, show final worktree/branch, and report local test evidence separately from deployment and real GitHub acceptance.
