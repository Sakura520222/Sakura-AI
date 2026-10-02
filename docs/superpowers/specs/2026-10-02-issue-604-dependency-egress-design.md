# Issue #604: execution-scoped dependency egress

Source: https://github.com/Sakura520222/Sakura-AI/issues/604

## Scope and authorization

Implement the issue's requested network-capability model. Keep the existing
sandboxd, one-shot runner, immutable images, resource limits, credential
exclusion, and workspace boundaries. No domain/IP lists, proxy, command
whitelist, new global policy, or human dependency-approval flow is needed.

## Design

The Backend's typed execution request gains `network_capability`, accepting
only `none` (default) or `dependency_egress`. Dependency-profile executions
implicitly request dependency egress; Agent Shell can explicitly request it
for dependency installation and build/test dependency resolution. A missing
Shell capability keeps the normal Agent execution offline under `web_tools`.

Fresh policy authorization takes place immediately before every execution.
`web_tools` permits dependency egress, `full_access` continues permitting all
executions to use egress, and `offline` never grants egress. An explicit
dependency capability under `offline` fails with a policy denial. Automatic
bootstrap under `offline` retains its observable skip. Local host execution
remains source-only and requires `full_access`, regardless of capability.

Use the existing sandboxd v2 `network_mode: none|egress` wire protocol. The
Backend alone maps an authorized abstract capability to that mode; neither
the model nor the Backend passes Docker network names or runtime options.
There is no protocol-version migration because sandboxd already validates
these modes and creates and destroys a separate container per execution.

Every Backend execution audit includes workspace/task identity, request ID,
profile, policy, policy revision, capability, fixed action, mode, runner
digest, and admission/terminal result. Command text, user-provided reasons,
environment contents, and credentials must not enter audit records. Preserve
failure and cancellation semantics, including cleanup failures.

The configuration UI keeps existing response fields and adds a projection
that distinguishes ordinary Shell network from temporary dependency egress.
Both translations and configuration/deployment/README documentation explain
that temporary egress is unrestricted during the current execution and
does not grant network to the next ordinary Shell command.

## Alternatives considered

1. Change Dependency profile only: insufficient because Agent Shell must
   also install dependencies and resolve build/test dependencies.
2. Extend sandboxd wire metadata: unnecessary for enforcement; introduces
   version compatibility work while the existing protocol already provides
   the correct runtime boundary.
3. Explicit Backend capability plus existing wire protocol: selected; the
   authorization decision is fresh, auditable, and execution-scoped.

## Verification

Use failing behavioral tests before implementation. Cover all three policy
values across ordinary Agent, Dependency, and explicit Shell capability;
unknown capability rejection; local full-access gate; bootstrap under the
default policy; fresh policy revocation; audit secrecy and terminal states;
UI status; and existing Web-tool behavior.

Exercise the actual Docker adapter with an immutable official runner image:
offline Agent -> temporary Dependency/Agent egress -> offline Agent. Prove
network access changes and each container is gone; cover timeout and
cancellation cleanup without weakening the hardening flags. Run focused
tests, the full suite, Ruff, and diff checks. Keep local verification distinct
from deployment or real GitHub `/agent` acceptance.
