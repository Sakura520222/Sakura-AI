# Repository Guidelines

## Project Structure

Sakura AI is a Python 3.14+ FastAPI service for GitHub PR/Issue review, WebUI administration, Telegram notifications, RAG, billing, and repository scanning. The main application is under `backend/`: `main.py` owns application setup, while `core/`, `api/`, `services/`, `models/`, `webui/`, `workers/`, and `telegram/` contain infrastructure, routes, domain logic, persistence, UI, background jobs, and bot integration. GitHub webhooks enter through `backend/api/webhook.py`; `backend/workers/review_worker.py` coordinates PR analysis, AI review, comment publication, and Check Runs. Root tests live in `tests/`. The independent `updater/` package uses a `src/` layout and has its own `tests/`. Runtime configuration is stored in the database `app_config` table (including `strategy.*`/`label.*` section keys); `config/` only holds `connection.json` for the Setup Wizard. Super-admin non-AI configuration is edited on the unified `/config` page; the AI config and system core config pages remain separate. Deployment assets are in `docker/`, `docs/`, `res/`, and `start.sh`.

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

`uv sync` is the recommended setup: it creates `.venv`, installs all dependencies including the editable updater package, and locks them in `uv.lock`. In a uv environment, prefix commands with `uv run` (e.g. `uv run python -m pytest -q`) instead of activating `.venv`. `requirements.txt` remains the authoritative dependency source for pip/Docker builds; the root `pyproject.toml` mirrors it line by line, enforced by `tests/test_uv_pyproject.py` — update both when changing dependencies. Install the updater editable package (or use `uv sync`) before running the full suite. Source development requires Linux x86_64/arm64 with glibc >= 2.28 (non-musl; Alpine is unsupported) or Apple Silicon macOS 14+; other platforms are out of the lockfile's scope because onnxruntime ships no Python 3.14 wheel for them (and no sdist). `run_ruff.py` without `--check` may modify and format files; use the check form for read-only validation.

## Coding Style and Testing

Use four-space indentation, English identifiers, and async-first I/O. Chinese or bilingual comments are acceptable. Keep route handlers thin and delegate business logic to services; use `loguru` for application logging and follow the root `ruff.toml` (`py314`) configuration. User-visible WebUI text must update both `backend/webui/translations/zh-CN.yaml` and `en.yaml`. Pytest and `pytest-asyncio` are used; name files `test_*.py` and functions `test_*`. Mock MySQL, Redis, GitHub, AI providers, and ChromaDB in unit tests. CI runs Ruff and pytest but does not enforce a coverage threshold.

## Architecture Contracts

- **Unified time**: get timestamps from `backend/core/time_service.py` and use the timezone-aware types in `backend/models/time_types.py`; never call `datetime.now()` directly or store naive datetimes.
- **Docker image channels**: updates run on `stable`/`development` channels coordinated across `backend/services/container_registry.py` (registry queries), `backend/core/build_info.py` (current channel/version), and the updater's `registry.py` (channel and digest resolution) — change all three plus the WebUI version manager page together.
- **Setup backup restore**: backup import must not overwrite deployment-time connection settings (DB connection string, Redis address); `backend/core/setup_service.py` preserves them, and any newly imported field must bypass them too.

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
- Do not expose credentials, cookies, tokens, stack traces, or sensitive internal details in logs, comments, notifications, or client errors. Verify backup restore still excludes deployment-time database and Redis connection settings.
- For changed high-risk behavior, look for focused tests covering the failure mode and a valid counterexample: compatibility, provider differences, configuration fallback, async failure, access control, or delivery isolation as applicable. Do not demand unrelated coverage or style-only changes.
