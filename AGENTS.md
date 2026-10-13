# Repository Guidelines

## Project Structure

Sakura AI is a Python 3.14+ FastAPI service for GitHub PR/Issue review, WebUI administration, unified announcement notifications (optional web/email/Telegram providers), RAG, billing, and repository scanning. The main application is under `backend/`: `main.py` owns application setup, while `core/`, `api/`, `services/`, `models/`, `webui/`, `workers/`, and `telegram/` contain infrastructure, routes, domain logic, persistence, UI, background jobs, and the optional Telegram notification-binding bot. Telegram business notifications in review/issue workers were removed; announcement delivery goes through the provider registry in `backend/services/notification_service.py`. GitHub webhooks enter through `backend/api/webhook.py`; `backend/workers/review_worker.py` coordinates PR analysis, AI review, comment publication, and Check Runs. Root tests live in `tests/`. The independent `updater/` package uses a `src/` layout and has its own `tests/`. Runtime configuration is stored in the database `app_config` table (including `strategy.*`/`label.*` section keys); `config/` holds deployment connection settings (`connection.json`) and the transient private Setup handoff (`.setup-token`). Super-admin non-AI configuration is edited on the unified `/config` page; the AI config and system core config pages remain separate. Deployment assets are in `docker/`, `docs/`, `res/`, and `start.sh`.

## Setup, Build, and Development Commands

```bash
uv sync
# Classic pip alternative:
python -m pip install -r requirements.txt
python -m pip install -e "./updater[dev]"
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
python scripts/dev_bootstrap.py
python -m pytest -q
python -m pytest tests/test_main.py -q
python -m pytest updater/tests -q
python -m pytest sandboxer/tests -q
ruff check .
python run_ruff.py --check
docker compose -f docker/docker-compose.yml up --build
```

`uv sync` is the recommended setup: it creates `.venv`, installs all dependencies including the editable updater package, and locks them in `uv.lock`. In a uv environment, prefix commands with `uv run` (e.g. `uv run python -m pytest -q`) instead of activating `.venv`. `requirements.txt` remains the authoritative dependency source for pip/Docker builds; the root `pyproject.toml` mirrors it line by line, enforced by `tests/test_uv_pyproject.py` — update both when changing dependencies. Install the updater editable package (or use `uv sync`) before running the full suite. Source development supports Windows 11 x86_64, Linux x86_64/arm64 with glibc >= 2.28 (non-musl; Alpine is unsupported), and Apple Silicon macOS 14+. Native Windows PowerShell supports `uv sync --locked`, `uv run pytest`, and Ruff without WSL/Git Bash. Keep portable tests active on every platform; check prerequisites only for POSIX deployment scripts, Linux isolation/systemd, or link privileges. Windows workspace locking and secure reads use native handles; do not remove cross-process exclusion or path-admission checks. The Host Updater, sandboxd, and local read-only Landlock/seccomp process isolation retain their Linux requirements. `run_ruff.py` without `--check` may modify and format files; use the check form for read-only validation.

## Coding Style and Testing

Use four-space indentation, English identifiers, and async-first I/O. Chinese or bilingual comments are acceptable. Keep route handlers thin and delegate business logic to services; use `loguru` for application logging and follow the root `ruff.toml` (`py314`) configuration. User-visible WebUI text must update both `backend/webui/translations/zh-CN.yaml` and `en.yaml`. Pytest and `pytest-asyncio` are used; name files `test_*.py` and functions `test_*`. Mock MySQL, Redis, GitHub, AI providers, and ChromaDB in unit tests. CI runs Ruff and pytest but does not enforce a coverage threshold.

## Architecture Contracts

- **Unified time**: get timestamps from `backend/core/time_service.py` and use the timezone-aware types in `backend/models/time_types.py`; never call `datetime.now()` directly or store naive datetimes.
- **Docker image channels**: updates run on `stable`/`development` channels coordinated across `backend/services/container_registry.py` (registry queries), `backend/core/build_info.py` (current channel/version), and the updater's `registry.py` (channel and digest resolution) — change all three plus the WebUI version manager page together.
- **Setup backup restore**: backup import must not overwrite deployment-time connection settings (DB connection string, Redis address); `backend/core/setup_service.py` preserves them, and any newly imported field must bypass them too.

## Logging and Observability

- **One logging pipeline**: use Loguru for application logs. `backend/core/logging_bridge.py` owns startup configuration, console/file sinks, standard-library interception, source preservation, level filtering, redaction and retention. Route handlers, providers and workers must not add independent sinks or bypass this pipeline with credential-bearing `print()`/traceback output. Preserve the originating standard `LogRecord` module, file, function, line and timestamp, including queued records and `stacklevel` callers.
- **Exception semantics**: in a Python `except` block, use Loguru's `logger.exception(...)` or `logger.opt(exception=True).error(...)`; use `logger.opt(exception=exc)` when recording a specific exception outside its handler or at another severity. `exc_info=True` belongs to standard `logging`, not Loguru. Do not mechanically replace valid standard-logging calls or change severity. Preserve exception identity, type, args, causes, context, notes and groups; propagate cancellation. Keep traceback diagnostics while leaving local-variable dumps disabled (`diagnose=False`, `backtrace=False` on sinks).
- **Central redaction**: use `backend/core/log_sanitizer.py` for messages, structured fields, headers, OAuth values, URL/query credentials and exception text. Redaction must run before formatting, queueing, output or persistence, including fields added by per-logger patchers. Never rely only on recognizable token prefixes: bind opaque credentials with `sensitive_log_scope(...)` around the concrete operation, including retry/fallback and stream cleanup. Preserve useful counters, status codes, endpoints and unrelated diagnostic text.
- **Escaping errors and copies**: `sensitive_log_scope` retains inherited and existing redaction knowledge on escaping exceptions and their cause/context/group graph before resetting its ContextVar. Keep this behavior for Starlette/Uvicorn re-logging, cancellation, group splitting and detached leaves. Do not dump `_log_redaction_secrets` or exception `__dict__` into logs/responses. When creating warning strings, truncated summaries or persisted failure reasons, call `sanitize_exception_text(exc, secrets=...)` before copying/truncating; arbitrary new text without a linked exception does not inherit provenance. Use `sanitize_text`/`sanitize` for other payloads; do not add local masking implementations.
- **Live levels and hot paths**: both sinks read the cached Settings singleton through the shared filter so database loading, WebUI edits and backup restores take effect without replacing sinks or rotating files. Preserve configured levels; production defaults to `INFO`, and unconfigured development to `DEBUG`. Do not hard-code competing thresholds in business code or suppress operational failures to reduce noise. Filters/patchers must not query external services or reread configuration files per record; avoid redundant full scans. Cache live Settings-derived decisions per record or with explicit invalidation, without serializing raw secrets or delaying updates. Keep log timestamps on the shared TimeService and preserve aware source instants.
- **Structured correlation**: use `backend/core/log_context.py` helpers (`log_context`, `task_logging`, `traced_operation`, `invocation_log_fields`, `update_log_context`) and existing Activity identifiers. Put `request_id`, `session_id`, `task_id`, `invocation_id`, `work_unit_id`, `thread_id`, `attempt_id`, `logical_call_id`, `tool_call_id` and `worker` in structured context rather than only message text. Generate HTTP request IDs locally; do not trust an incoming correlation header. Bind actual session/invocation IDs around the entire worker lane, including clone/index/report and terminal failures. After an observed attempt begins, bind its ID before the SDK call for chat, streaming and embedding; unavailable observation must not fabricate a persisted attempt ID.
- **Async isolation and reload**: copy context on update and restore it in `finally`. New task lanes may retain the submission request ID but must clear unrelated task/session/attempt IDs. Use context-propagating thread APIs or explicit snapshots for executors; never reset a ContextVar token from another task. Async generators must restore the consumer context before yielding and run `aclose()` with their own correlation/redaction scope, retaining cleanup errors. `logging_bridge`, `log_context` and `log_sanitizer` are restart-only in `backend/core/hot_reload.py`; do not reload their process state/ContextVars while scopes are active. This does not disable Settings hot updates.
- **Storage and access**: preserve the JSON Lines `text`/`record` shape and structured IDs used by `backend/services/operational_log_service.py`. Activity log lookup must stay authorized, session-scoped, no-cache and bounded; it reads local structured logs, not arbitrary paths or all replicas. Keep application rotation, age and historical-capacity budgets (currently 50 MB, 10 days and 500 MiB, excluding active files). Keep Docker `json-file` limits for Compose, sandboxd and runners (currently `max-size=50m`, `max-file=3`); deployment changes require container recreation. Never delete an active log to enforce a budget.
- **Setup credentials**: never send the Setup Token to application/Docker logs. Keep its handoff beside the active connection config, private from creation: POSIX `0600`, or a protected process-user Windows DACL with a non-inheritable handle and ACL-capability checks. Publish only after atomic replacement succeeds. Revoke the memory credential immediately on completion; a failed file deletion emits a safe warning and must not turn committed Setup/admin state into failure or prevent restart. Keep README/README_EN and deployment instructions aligned with file-based handoff.
- **Regression evidence**: for logging changes, verify actual console and serialized sinks, original source/traceback, live level changes, opaque/encoded secrets, scope exit followed by framework/worker re-logging, groups/cancellation/stream close, concurrent isolation and Activity attempt/session matching as relevant. Reuse `tests/test_logging_security.py`, `test_logging_review_regressions.py`, `test_logging_bridge.py`, `test_log_context.py`, `test_operational_logs.py`, `test_activity_observability_attempts.py`, `test_hot_reload.py` and `test_setup_token.py`. Keep portable tests active; native ACL and symlink tests may check their real platform prerequisites. Do not treat a safe first log or regex-only check as proof that later copies/persistence are safe.

## Commits and Pull Requests

Use English Conventional Commits, for example `feat(updater): add socket recovery` or `fix(ruff): align Python 3.14 rules`. Follow Gitflow: daily work uses `feature/*` from `develop` and targets `develop`; `release/*` and `hotfix/*` follow the repository’s release flow. Do not push directly to `main` or `develop`. PRs should explain behavior changes, list validation commands, note configuration/database/deployment impact, link related issues when applicable, and include screenshots for WebUI changes. Keep `README.md` and `README_EN.md` aligned for user-facing changes.

## Security and Agent Notes

Never hardcode credentials or commit runtime files such as `config/connection.json` or `.deploy/deployment.env`. For structural code questions, prefer the configured CodeGraph index; use `rg` or direct file reads for literal text, configuration, and documentation.

## Code Review Rules

Review changed behavior against the PR's stated intent and the repository contracts above. Report actionable regressions with a concrete failure scenario; leave formatting, import order, and other deterministic checks to Ruff and pytest. Apply each rule only where the changed code makes it relevant.

### Correctness and compatibility

- Flag incomplete fixes, incorrect edge cases, race conditions, and behavior changes not explained by the PR. Check that the implementation addresses the root cause rather than hiding the symptom.
- Preserve existing API endpoints, request semantics, and response fields unless the change explicitly provides a compatible transition. Check webhook event handling for duplicate or out-of-order delivery.
- Keep existing database rows and deployments usable across migrations; flag destructive schema changes or changed column meaning without an explicit migration and compatibility path.
- Check timestamps and persisted time values against `backend/core/time_service.py` and `backend/models/time_types.py`; do not introduce naive datetimes or bypass the shared time service.

### Configuration and provider behavior

- Do not hard-code configurable model capabilities or provider-specific limits in business logic, including output/context token limits, reasoning and image support, streaming behavior, model IDs, and provider URLs. Resolve them from model metadata, provider configuration, or the appropriate configuration layer when available; flag silent constants that alter behavior.
- Review new timeouts, retry counts, rate limits, and concurrency limits for unjustified constants or duplicate sources of truth. Do not flag protocol-required constants or documented invariant defaults merely for being literals.
- Keep dynamic configuration in `app_config` with Settings defaults as the single default source. For `strategy.*` and `label.*`, preserve default deep merging and tolerant recovery when a section key is missing or invalid; do not restore reads from removed config files.
- Keep provider-specific request, response, and capability handling within the provider abstraction. Flag assumptions about one provider or model that leak into shared review logic or silently change other providers' behavior.
- When changing container update channels, verify the registry service, build info, updater registry, and WebUI version manager remain consistent.

### Async, failures, and delivery

- Flag blocking I/O in async handlers and workers, unbounded background work, or fire-and-forget tasks whose exceptions or cancellations can be lost.
- Do not silently swallow failures or convert an operational error into a success-shaped fallback. Intentional degradation must retain an observable signal and preserve the caller's error or retry semantics.
- Preserve idempotency, retry safety, and status consistency across webhook processing, review publication, comments, Check Runs, and notification delivery. Check failure paths as well as the happy path.
- Keep notification channel and recipient eligibility isolated: a disabled channel, failed endpoint, or unauthorized recipient must not receive another channel's delivery or leak its content.

### Security and regression evidence

- Check authentication, authorization, and repository or organization ownership on new or changed protected endpoints; never trust client-supplied IDs without verifying their scope.
- Never expose credentials, cookies or authentication tokens in logs, comments, notifications or client errors. Keep necessary, sanitized stack traces and internal diagnostics in protected operational logs; never publish them in GitHub comments, notifications or client errors. Verify backup restore still excludes deployment-time database and Redis connection settings.
- For changed high-risk behavior, look for focused tests covering the failure mode and a valid counterexample: compatibility, provider differences, configuration fallback, async failure, access control, or delivery isolation as applicable. Do not demand unrelated coverage or style-only changes.
