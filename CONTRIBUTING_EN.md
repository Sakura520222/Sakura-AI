# Sakura AI Contributing Guide

**English** | [中文](CONTRIBUTING.md)

Thank you for your interest in contributing to **Sakura AI**! Sakura AI is an AI-powered GitHub PR and Issue code review & analysis bot capable of proactively exploring repositories. To maintain code quality, architectural consistency, and a smooth collaboration workflow, please read this guide thoroughly before submitting contributions.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Gitflow Branching Model](#gitflow-branching-model)
- [Development Setup](#development-setup)
- [Code Style & Architecture Contracts](#code-style--architecture-contracts)
- [Commit Message Conventions](#commit-message-conventions)
- [Testing & Quality Checks](#testing--quality-checks)
- [Pull Request Workflow](#pull-request-workflow)
- [Reporting Security Vulnerabilities](#reporting-security-vulnerabilities)

---

## Code of Conduct

We are dedicated to providing a welcoming, inclusive, respectful, and harassment-free experience for everyone in our community. Please communicate with empathy, accept constructive feedback, and treat all contributors with respect.

---

## Gitflow Branching Model

This project strictly follows the standard **Gitflow** branching workflow:

- **`main`**: Production stable branch. Protected; direct pushes are strictly prohibited.
- **`develop`**: Daily integration branch. The target for all active development; direct pushes are strictly prohibited.
- **`feature/<name>`**: Feature branches. All new features, enhancements, or routine bug fixes must branch off from `develop`, and PRs must target `develop`.
- **`release/x.y.z`**: Release branches. Created from `develop` by maintainers to bump version numbers, update docs, and verify release readiness before merging into `main`. Once merged into `main`, changes are synced back to `develop`.
- **`hotfix/x.y.z`**: Production emergency fixes. Created from `main`, verified, merged into `main`, and then immediately synced back to `develop`.

```
main       ----------------------●-----------------● (v3.2.4)
                                 ^                 ^
release                          |--- release/x ---|
                                 |                 |
develop    ------●---------------●-----------------●
                 \              /
feature           \-- feature --/
```

---

## Development Setup

### System Prerequisites

- **Python Version**: Python 3.14+
- **Supported Platforms**:
  - Linux x86_64 / aarch64 (glibc ≥ 2.28 required; musl-based distributions such as Alpine are not supported)
  - Apple Silicon macOS 14+
  - *Note*: Underlying dependencies such as `onnxruntime` (required by ChromaDB) only publish Python 3.14 pre-built wheels for these platforms.

### Installing Dependencies

We recommend using [`uv`](https://github.com/astral-sh/uv) for dependency management. It automatically creates `.venv`, installs the editable package [updater/](updater/), and locks dependencies into `uv.lock`:

```bash
# Recommended (uv)
uv sync
```

Alternatively, using traditional `pip`:

```bash
python -m pip install -r requirements.txt
python -m pip install -e './updater[dev]'
```

> **Important**:
> - [requirements.txt](requirements.txt) is the authoritative dependency source for pip and Docker builds. The root [pyproject.toml](pyproject.toml) mirrors it line-by-line, enforced by unit test [tests/test_uv_pyproject.py](tests/test_uv_pyproject.py). When modifying dependencies, update both files.
> - The entire repository utilizes PEP 758 syntax and requires Python 3.14+.

### Common Development Commands

```bash
# Start the main app in supervisor loop mode (with module hot-reloading)
python -m backend.main

# Run directly via uvicorn
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000

# Run Setup Wizard in dev bootstrap mode
python scripts/dev_bootstrap.py
```

---

## Code Style & Architecture Contracts

To maintain long-term maintainability and codebase elegance, please adhere to the following conventions:

### 1. Code Style
- **Python**: 4 spaces per indentation level, English identifiers for variables, functions, and classes.
- **Comments**: Bilingual (English/Chinese) or English/Chinese comments explaining business rationale and complex algorithms.
- **Async-First**: All I/O-bound operations (database queries, network requests, background workers) must be asynchronous (`async`/`await`). Never introduce blocking synchronous I/O in async paths.
- **Logging**: Use `loguru` exclusively for application logs. Do not use `print()`.

### 2. Static Analysis & Linting (Ruff)
Ruff is used across the workspace with rules tailored for Python 3.14:

```bash
# Read-only lint check (run before committing)
python run_ruff.py --check
# or
ruff check .

# Automatically apply safe fixes and formatting
python run_ruff.py
```

### 3. Unified Time Service Contract
- Do not call `datetime.now()` directly or instantiate naive datetime objects.
- All timestamps must be obtained from [backend/core/time_service.py](backend/core/time_service.py) and typed via [backend/models/time_types.py](backend/models/time_types.py) to guarantee DST fold safety and timezone awareness across the system.

### 4. Internationalization (i18n)
- Any user-visible text in the WebUI or API responses must be synchronized across translation catalogs:
  - [backend/webui/translations/zh-CN.yaml](backend/webui/translations/zh-CN.yaml)
  - [backend/webui/translations/en.yaml](backend/webui/translations/en.yaml)

### 5. Configuration Architecture
- Dynamic runtime configuration is stored in the database `app_config` table.
- Default settings defined in [backend/core/settings.py](backend/core/settings.py) serve as the single source of truth for initial seeding and fallback.
- Never hardcode secrets, tokens, or private endpoint URLs.
- Runtime credential files such as [config/connection.json](config/connection.json) and `.deploy/deployment.env` must never be committed to Git.

---

## Commit Message Conventions

Commit messages must follow the English **[Conventional Commits](https://www.conventionalcommits.org/)** specification:

```
<type>(<scope>): <short description>

[optional body]

[optional footer(s)]
```

### Types

- **`feat`**: A new feature
- **`fix`**: A bug fix
- **`docs`**: Documentation only changes
- **`style`**: Changes that do not affect the meaning of the code (formatting, white-space, etc.)
- **`refactor`**: A code change that neither fixes a bug nor adds a feature
- **`perf`**: A code change that improves performance
- **`test`**: Adding missing tests or correcting existing tests
- **`chore`**: Changes to the build process, tooling, dependencies, or auxiliary files

### Examples

```bash
feat(review): add support for semantic code diff analysis
fix(updater): resolve unix socket connection timeout on reload
docs(readme): update deployment instructions for Docker Compose
test(models): add unit tests for timezone-aware timestamp fields
```

---

## Testing & Quality Checks

Ensure all tests pass locally before opening a pull request:

```bash
# Run full test suite (main app, updater, and sandboxer)
python -m pytest -q

# Run using uv
uv run python -m pytest -q

# Run specific test modules
python -m pytest tests/test_main.py -q
python -m pytest updater/tests -q
python -m pytest sandboxer/tests -q
```

### Test Principles
1. **Mock External Services**: Unit tests must never connect directly to production external services. MySQL, Redis, GitHub API, AI providers (OpenAI, Gemini, Claude, etc.), and ChromaDB must be properly mocked.
2. **Naming Convention**: Test files must be named `test_*.py` and test functions `test_*`.

---

## Pull Request Workflow

1. **Fork the repository** on GitHub.
2. **Clone and create a branch**:
   ```bash
   git clone https://github.com/<your-username>/Sakura-AI.git
   cd Sakura-AI
   git checkout develop
   git checkout -b feature/your-feature-name
   ```
3. **Develop & Test**: Implement changes with corresponding unit test coverage.
4. **Run Local Checks**:
   ```bash
   python run_ruff.py --check
   python -m pytest -q
   ```
5. **Commit Changes**: Use Conventional Commits in English.
6. **Push to GitHub**:
   ```bash
   git push origin feature/your-feature-name
   ```
7. **Open a Pull Request**:
   - Ensure the **base branch is `develop`**, never `main`.
   - Fill in the PR description thoroughly, outlining the problem, solution, validation methods, and any database/config impact.
   - Include screenshots or recordings if WebUI changes are involved.
8. **Code Review & Iteration**:
   - Maintainers and the Sakura AI review bot will review your PR.
   - Address feedback and push updates. Once checks pass and reviews are approved, it will be merged into `develop`.

---

## Reporting Security Vulnerabilities

If you discover a security vulnerability or security-sensitive issue, please **do not** report it via public GitHub issues.

Please refer to our [Security Policy (SECURITY.md)](SECURITY.md) (or [Chinese Version](SECURITY_CN.md)) and report privately via **[GitHub Private Vulnerability Reporting](https://github.com/Sakura520222/Sakura-AI/security/advisories/new)** or by email to <sakura520222@outlook.com>.
