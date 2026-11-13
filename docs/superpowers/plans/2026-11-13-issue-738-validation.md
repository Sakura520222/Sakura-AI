# Issue #738 acceptance evidence

Branch: `feature/738-webui-regressions`, based on `develop` at `ad8f38c602401eab5a0af457cf47923b4b69e6db`.

## Acceptance mapping

| Issue items | Implemented boundary | Regression evidence |
| --- | --- | --- |
| 1 | User-scoped SQL credentials, required identity match, private no-store response, user-keyed browser authorization cache and cache/state replacement on authenticated identity changes; community API role guards | `tests/test_issue_738_authorization.py`, `tests/test_webui_v2_github_app.py`, auth/GitHub frontend tests, `authorization-review-738.spec.ts` |
| 2 | Preserve global `pr_id`, add nullable repository `pr_number`, use only complete valid repository identity/number for links and disable missing links | workflow API and frontend tests, `authorization-review-738.spec.ts` |
| 3 | Refresh cached strategy before calls, default full chain and configured order, opt-in sticky invalidated on chain changes, per-candidate skip diagnostics through existing Activity log pipeline | dynamic configuration, fallback, Activity attempt and config-center integration tests |
| 4–5 | Drag and keyboard ordering retain account/model pair and dirty state; explicit Save persists priority. Capability selection uses enabled configured accounts/models with provider/model identity and unsaved-switch protection | editors and SettingsPage tests, `ai-settings-738.spec.ts` using real local configuration API |
| 6–7 | Direct authorized canonical messages; exact canonical tool argument/result artifact requests retain existing server ACL. Paired tool call/result filtering, JSON/Markdown/code, bounded formatting/rendering and copy/expand states | Activity access and frontend regressions, `activity-community-738.spec.ts` with backend-shaped fixtures |
| 8 | All permitted category groups and advanced fields visible continuously; search/save/reset and dirty navigation retained | SettingsPage tests, existing settings browser suite |
| 9 | Header restart uses original protected endpoint, explicit confirmation, startup identity recovery checks and bounded polling | AppShell and system panel tests; existing endpoint authorization unchanged |
| 10 | Automatic/manual checks share one in-flight task/cache; last-known-good survives errors; last/next/status displayed, shutdown and restart recover cleanly | update checker and main lifecycle tests, version browser regressions |
| 11–13 | Running build channel initializes version filter, full revision/digest/time displayed when present, inline Markdown release notes; header current version and update availability remain separate | version API, VersionBrowser/AppShell tests, `version.spec.ts` desktop/mobile fixtures |
| 14 | Star community and Issues use distinct icons | AppShell regression |
| 15–17 | Community discovery/personal plan separated from administrator tab, personal repository dialog, management search/filter/sort/pagination/totals, global toggle only in Settings, responsive cards with topics and expandable summaries | Star Aid/member sorting and frontend tests, `activity-community-738.spec.ts` |

Both YAML catalogs and the compiled React message catalog are synchronized. Account/model, billing, notification, Activity authorization and updater interfaces retain their existing ownership and security boundaries. Per-call strategy snapshots preserve in-flight chat/stream behavior when administrators save a new policy; obsolete calls cannot write sticky preference into a changed chain. API additions are additive; no database migration or new runtime dependency is introduced.

## Verification

- Frontend unit suite: 108 passed.
- Playwright browser suite: 20 passed, including real local configuration persistence, account-switch cache replacement, PR links, responsive community, structured tool artifacts and version provenance/notes.
- TypeScript and production build: passed.
- Ruff and `run_ruff.py --check`: passed.
- Translation catalog check: 1,467 bilingual WebUI messages verified.
- Source ESLint: passed with `npm run lint --prefix web -- --ignore-pattern .dist-cache`; `.dist-cache` contains generated bundle snapshots and is excluded from this source check.
- `git diff --check`: passed.
- Final backend full suite: **6,699 passed, 81 skipped**, one dependency deprecation warning, in 250.63 seconds. Command: `UV_CACHE_DIR=/tmp/sakura-738-uv uv run --no-sync pytest -q`. Final source was held unchanged throughout this run.

All provider/GitHub/updater browser fixtures use isolated data or mocked external services. No production recipients, credentials, AI requests or payment gateways were exercised. Browser screenshots are local ignored artifacts under `web/artifacts/`: `738-activity-*`, `738-community-*`, `ai-settings-*`, and `version-738-*`.

Actual lifespan tests cover source and stable/development image routing, manual checks with background workers disabled, shared singleton registration and supervisor shutdown. Four adapter-gated concurrency regressions cover chat/stream policy replacement and late-winner isolation.

## Review

The deployment lifecycle regression also registers its temporary database connection with monkeypatch, preserving subsequent Setup/backup test isolation (45 lifecycle/backup tests passed together).

Independent review found two Activity gaps: real protected tool artifacts still required clicks, and deeply nested compact JSON could expand beyond rendering limits. Both were reproduced, repaired and reviewed again. Exact artifact IDs/kinds plus server read checks preserve authorization; pretty printing has explicit depth/work bounds and pagination uses rendered representation size.
