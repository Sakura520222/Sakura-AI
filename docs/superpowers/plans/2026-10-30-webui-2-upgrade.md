# WebUI 2.0 replacement

The user requested a complete WebUI replacement for Issue #638, retaining the
original website icon. The final instruction is to stop running tests and finish
the replacement directly. This supersedes the earlier staged rollout checklist.

## Interface and entry points

- The React application owns all page rendering at `/app/*`; `/` and saved URLs
  redirect to the corresponding screen. There is no old-interface switch.
- Login, MFA, Setup, legal pages and payment results also use React. Existing
  OAuth callbacks and cookie sessions retain their established protocol.
- Settings, review and issue workflows, Agent tasks/candidates/skills/workspaces,
  activity, scans, queue, GitHub authorization, billing and administration share
  the new navigation and controls.
- Announcements, Star community, repository memory, vector storage and security
  administration have native React pages and authorized JSON APIs.
- The original blossom mark and favicon configuration are preserved.
- The old template tree and settings scripts are retired; reusable Python
  operations remain behind authorized JSON adapters.

## Configuration and delivery

The configuration metadata, durable revisions, defaults, permission boundaries,
secret masking, backup protections and deployment connection ownership remain in
the existing services. Payment and notification operations use the same services.
Docker builds the frontend in a Node stage and serves it from the Python runtime.
Release archives and images include the prebuilt dist. Git/source startup downloads a matching verified bundle automatically; only frontend contributors use `npm run build --prefix web`.

## Execution record

Earlier test results belong to the earlier implementation checkpoint. No tests
are run after the user's stop-testing instruction. Production asset generation
is required to make the final replacement runnable. Changes are local; no push,
merge, release, deployment, live payment or production configuration change is
part of this work.
