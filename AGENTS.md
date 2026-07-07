# AGENTS.md

## Project overview

Sakura AI is a Python 3.11+ FastAPI service for GitHub PR review, Issue analysis, WebUI administration, Telegram notifications, RAG/code indexing, billing, and repository scanning. Prefer Chinese when communicating in this repository.

## Architecture map

- `backend/main.py`: FastAPI application, lifespan startup/shutdown, middleware, and router registration.
- `backend/core/`: settings, bootstrap/setup mode, GitHub App integration, Redis, language/model context utilities.
- `backend/api/`: GitHub webhook routes and versioned REST API under `backend/api/v1/`.
- `backend/services/`: domain services for PR/Issue analysis, AI review, RAG, vector stores, payment, scan scheduling, Telegram, and GitHub writes.
- `backend/services/ai_reviewer/`: main AI review engine components; keep prompt/tool logic isolated here when possible.
- `backend/models/`: SQLAlchemy models and async database session setup.
- `backend/webui/`: Jinja2/HTMX/Alpine WebUI routes, auth, SSE, i18n, templates, and translations.
- `backend/telegram/`: Telegram bot handlers, menus, and notifications.
- `backend/workers/`: Celery workers for review, issue, and scan jobs.
- `config/`: YAML defaults for labels/strategies plus runtime-generated connection config.
- `docker/`: Dockerfile and Compose setup.
- `docs/`: feature and API documentation; link to these docs instead of duplicating them.

## Commands agents should know

- Install runtime dependencies: `python -m pip install -r requirements.txt`
- Install CI/dev extras when needed: `python -m pip install ruff pytest pytest-asyncio`
- Run tests: `pytest -q`
- Run Ruff check only: `python run_ruff.py --check`
- Run Ruff check/fix/format: `python run_ruff.py`
- Start with Docker Compose helper: `./start.sh`
- Rebuild Docker image: `./start.sh --rebuild`
- Direct local app command when environment is configured: `uvicorn backend.main:app --host 0.0.0.0 --port 8000`

On Windows, use the active Python environment and PowerShell equivalents. Do not run deployment, release, or git commit/push commands unless the user explicitly asks.

## Coding conventions

- Use English identifiers and code; comments/docstrings may be Chinese or bilingual when helpful.
- Prefer async-first implementations for I/O: HTTP, database, Redis, GitHub API, AI calls, and filesystem-heavy service flows.
- Use SQLAlchemy async sessions for database work; avoid sync ORM patterns in request handlers and services.
- Use `loguru` for logging; do not replace it with stdlib logging in new code.
- Keep configuration flow consistent: database `app_config` overrides runtime settings; YAML files under `config/` provide defaults/templates; initial database connection is handled by Setup Wizard/bootstrap.
- Avoid hardcoding secrets, API keys, domains, GitHub App private keys, or database URLs.
- For WebUI text, use the i18n system and update both `backend/webui/translations/zh-CN.yaml` and `backend/webui/translations/en.yaml` for user-visible strings.
- Preserve existing service boundaries: route handlers should stay thin and delegate business logic to services.
- When adding dependencies, update `requirements.txt` and consider Docker/start script implications.
- Use Ruff formatting/checking before finishing Python changes when practical.

## Testing and validation

- Prefer focused tests in `tests/` for changed behavior.
- The CI workflow runs `ruff check .` and `pytest -q` on Python 3.11.
- Many services depend on MySQL, Redis, GitHub App, AI providers, or ChromaDB; for unit tests, mock external systems rather than requiring live services.
- If modifying WebUI templates/routes, verify translation keys and auth/session behavior.
- If modifying setup/bootstrap/config behavior, consider first-run mode where only `/setup` is available.

## Git and release rules

- Follow standard Gitflow:
  - `main` is production.
  - `develop` is integration.
  - Create daily work on `feature/<name>` from `develop`, with PR target `develop`.
  - Create releases on `release/x.y.z` from `develop`, update versions and both READMEs, with PR target `main`.
  - Create hotfixes on `hotfix/x.y.z` from `main`, with PR target `main`.
  - After release/hotfix merges to `main`, merge `main` back to `develop`.
- Never directly push to `main` or `develop`.
- Never commit autonomously; do not let subagents commit either.
- Commit messages should use English Conventional Commits.
- For release or user-facing documentation changes, keep `README.md` and `README_EN.md` aligned.

## Key docs to link, not duplicate

- General project features and quick start: `README.md` and `README_EN.md`.
- Project-specific high-level rules: `CLAUDE.md`.
- API reference: `docs/api-v1-reference.md`.
- Manual review flow: `docs/MANUAL_REVIEW_FEATURE.md`.
- Approval/decision features: `docs/APPROVAL_FEATURE_SUMMARY.md`.
- Model context handling: `docs/MODEL_CONTEXT_FEATURE.md`.
- Telegram setup: `docs/TELEGRAM_SETUP.md`.
- Design plans: files under `docs/plans/`.

## Common pitfalls

- `run_ruff.py` default mode modifies files; use `--check` for read-only validation.
- Bootstrap mode starts when required connection/config data is missing; avoid assuming the full app is always initialized.
- `config/connection.json` may be generated at runtime and can contain sensitive environment-specific values.
- The Docker Compose service uses host networking and mounts source directories for development.
- AI review, RAG, scan, and GitHub write flows are highly asynchronous; preserve cancellation/error handling and do not block the event loop.
- Translation fallback returns the key itself when missing, so missing i18n entries can be easy to overlook.
