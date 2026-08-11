# Repository Guidelines

## Project Structure

Sakura AI is a Python 3.14+ FastAPI service for GitHub PR/Issue review, WebUI administration, Telegram notifications, RAG, billing, and repository scanning. The main application is under `backend/`: `main.py` owns application setup, while `core/`, `api/`, `services/`, `models/`, `webui/`, `workers/`, and `telegram/` contain infrastructure, routes, domain logic, persistence, UI, background jobs, and bot integration. Root tests live in `tests/`. The independent `updater/` package uses a `src/` layout and has its own `tests/`. Runtime defaults and deployment assets are in `config/`, `docker/`, `docs/`, `res/`, and `start.sh`.

## Setup, Build, and Development Commands

```bash
python -m pip install -r requirements.txt
python -m pip install -e "./updater[dev]"
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
python scripts/dev_bootstrap.py
python -m pytest -q
python -m pytest tests/test_main.py -q
python -m pytest updater/tests -q
ruff check .
python run_ruff.py --check
docker compose -f docker/docker-compose.yml up --build
```

Install the updater editable package before running the full suite. `run_ruff.py` without `--check` may modify and format files; use the check form for read-only validation.

## Coding Style and Testing

Use four-space indentation, English identifiers, and async-first I/O. Chinese or bilingual comments are acceptable. Keep route handlers thin and delegate business logic to services; use `loguru` for application logging and follow the root `ruff.toml` (`py314`) configuration. User-visible WebUI text must update both `backend/webui/translations/zh-CN.yaml` and `en.yaml`. Pytest and `pytest-asyncio` are used; name files `test_*.py` and functions `test_*`. Mock MySQL, Redis, GitHub, AI providers, and ChromaDB in unit tests. CI runs Ruff and pytest but does not enforce a coverage threshold.

## Commits and Pull Requests

Use English Conventional Commits, for example `feat(updater): add socket recovery` or `fix(ruff): align Python 3.14 rules`. Follow Gitflow: daily work uses `feature/*` from `develop` and targets `develop`; `release/*` and `hotfix/*` follow the repository’s release flow. Do not push directly to `main` or `develop`. PRs should explain behavior changes, list validation commands, note configuration/database/deployment impact, link related issues when applicable, and include screenshots for WebUI changes. Keep `README.md` and `README_EN.md` aligned for user-facing changes.

## Security and Agent Notes

Never hardcode credentials or commit runtime files such as `config/connection.json` or `.deploy/deployment.env`. For structural code questions, prefer the configured CodeGraph index; use `rg` or direct file reads for literal text, configuration, and documentation.
