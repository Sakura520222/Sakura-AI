"""Static guard for the small, documented wall-clock primitive boundary.

Business code must use ``now_utc()``/``TimeService``.  The entries below are
the only intentional primitive calls left in production: the TimeService's OS
wall clock, the Alipay protocol's fixed UTC+08 timestamp, the TOTP epoch
counter, and the updater's UTC protocol helper.  File-mtime retention remains
wall-clock metadata but is computed from ``now_utc`` rather than a primitive.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
PRODUCTION_ROOTS = (PROJECT_ROOT / "backend", PROJECT_ROOT / "updater" / "src")

TIME_PRIMITIVE_ALLOWLIST = (
    {
        "file": "backend/core/time_service.py",
        "position": "backend/core/time_service.py:33",
        "primitive": "datetime.now(UTC)",
        "category": "time-service-wall-clock",
        "reason": "The sole OS wall-clock implementation for the shared TimeService.",
    },
    {
        "file": "backend/services/payment/alipay_gateway.py",
        "position": "backend/services/payment/alipay_gateway.py:608",
        "primitive": "datetime.now(bj_tz)",
        "category": "fixed-protocol-timezone",
        "reason": "Alipay requires its timestamp in fixed UTC+08:00 protocol time.",
    },
    {
        "file": "backend/services/two_factor_service.py",
        "position": "backend/services/two_factor_service.py:102",
        "primitive": "time.time()",
        "category": "protocol-epoch",
        "reason": "TOTP RFC epoch step is defined from Unix wall-clock seconds.",
    },
    {
        "file": "updater/src/sakura_ai_updater/time.py",
        "position": "updater/src/sakura_ai_updater/time.py:15",
        "primitive": "datetime.now(UTC)",
        "category": "updater-protocol-time",
        "reason": "Updater emits independent UTC RFC3339 Z protocol timestamps.",
    },
)

# Not a primitive call, but an intentional wall-clock boundary that should
# remain visible to reviewers: retention compares filesystem metadata to the
# UTC wall clock.  It must never become an application event timestamp.
TIME_BOUNDARY_ALLOWLIST = TIME_PRIMITIVE_ALLOWLIST + (
    {
        "file": "backend/core/logging_bridge.py",
        "position": "backend/core/logging_bridge.py:32",
        "primitive": "Path.stat().st_mtime",
        "category": "filesystem-metadata-wall-clock",
        "reason": "Log retention uses filesystem mtime only, never a domain instant.",
    },
)

FROMTIMESTAMP_ALLOWLIST = (
    {
        "file": "backend/core/logging_bridge.py",
        "position": "backend/core/logging_bridge.py:95",
        "category": "stdlib-logrecord-boundary",
        "reason": "LogRecord.created is a Unix instant and is immediately made aware UTC.",
    },
    {
        "file": "backend/main.py",
        "position": "backend/main.py:74",
        "category": "health-boundary",
        "reason": "The health payload converts its legacy numeric startup instant to aware UTC.",
    },
    {
        "file": "backend/webui/routes/agent_team.py",
        "position": "backend/webui/routes/agent_team.py:1365",
        "category": "filesystem-metadata-boundary",
        "reason": "Worktree mtime is filesystem metadata, converted to an aware UTC display value.",
    },
    {
        "file": "backend/webui/routes/agent_team.py",
        "position": "backend/webui/routes/agent_team.py:1599",
        "category": "filesystem-metadata-boundary",
        "reason": "Workspace mtime is filesystem metadata, converted to an aware UTC display value.",
    },
    {
        "file": "backend/services/activity_observability/conversation_service.py",
        "position": "backend/services/activity_observability/conversation_service.py:54",
        "category": "epoch-sentinel",
        "reason": "The observability projection uses Unix epoch as an explicit no-value sentinel.",
    },
    {
        "file": "backend/services/star_aid_github_service.py",
        "position": "backend/services/star_aid_github_service.py:87",
        "category": "github-protocol-boundary",
        "reason": "GitHub rate-limit reset is a Unix epoch instant and is normalized to aware UTC.",
    },
)


def _primitive_calls() -> set[tuple[str, int, str]]:
    calls: set[tuple[str, int, str]] = set()
    for root in PRODUCTION_ROOTS:
        for path in root.rglob("*.py"):
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(
                    node.func, ast.Attribute
                ):
                    continue
                receiver = node.func.value
                if (
                    isinstance(receiver, ast.Name)
                    and receiver.id == "datetime"
                    and node.func.attr == "now"
                ):
                    calls.add((relative, node.lineno, "datetime.now"))
                elif (
                    isinstance(receiver, ast.Name)
                    and receiver.id == "time"
                    and node.func.attr == "time"
                ):
                    calls.add((relative, node.lineno, "time.time"))
    return calls


def _production_python_files() -> list[Path]:
    return [path for root in PRODUCTION_ROOTS for path in root.rglob("*.py")]


def _fromtimestamp_calls() -> set[tuple[str, int, str]]:
    calls: set[tuple[str, int, str]] = set()
    for path in _production_python_files():
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "datetime"
                and node.func.attr == "fromtimestamp"
            ):
                calls.add((relative, node.lineno, "datetime.fromtimestamp"))
                assert any(keyword.arg == "tz" for keyword in node.keywords), (
                    f"{relative}:{node.lineno} must pass an explicit tz="
                )
    return calls


def _production_source() -> dict[str, str]:
    files: dict[str, str] = {}
    for root in PRODUCTION_ROOTS:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in {
                ".py",
                ".html",
                ".js",
                ".ts",
                ".svelte",
            }:
                files[path.relative_to(PROJECT_ROOT).as_posix()] = path.read_text(
                    encoding="utf-8"
                )
    return files


def test_all_wall_clock_primitives_are_documented_allowlist_entries():
    actual = _primitive_calls()
    expected = {
        (
            entry["file"],
            int(entry["position"].rsplit(":", 1)[1]),
            entry["primitive"].split("(", 1)[0],
        )
        for entry in TIME_PRIMITIVE_ALLOWLIST
    }
    assert actual == expected
    assert all(
        entry["file"] and entry["position"] and entry["category"] and entry["reason"]
        for entry in TIME_BOUNDARY_ALLOWLIST
    )
    assert any(
        entry["category"] == "filesystem-metadata-wall-clock"
        for entry in TIME_BOUNDARY_ALLOWLIST
    )


def test_all_fromtimestamp_calls_are_aware_and_allowlisted():
    actual = _fromtimestamp_calls()
    expected = {
        (
            entry["file"],
            int(entry["position"].rsplit(":", 1)[1]),
            "datetime.fromtimestamp",
        )
        for entry in FROMTIMESTAMP_ALLOWLIST
    }
    assert actual == expected
    assert all(
        entry["category"] and entry["reason"] for entry in FROMTIMESTAMP_ALLOWLIST
    )


def test_naive_date_primitives_and_model_datetime_defaults_are_absent():
    source = "\n".join(_production_source().values())
    forbidden_patterns = (
        (r"\bdatetime\.utcnow\s*\(", "naive datetime.utcnow"),
        (r"\bdatetime\.today\s*\(", "datetime.today"),
        (r"\bdate\.today\s*\(", "date.today"),
        (
            r"\b(?:default|onupdate)\s*=\s*(?:datetime\.)?utcnow\b",
            "naive ORM default/onupdate",
        ),
    )
    for pattern, label in forbidden_patterns:
        assert not re.search(pattern, source), label


def test_application_time_boundary_antipatterns_are_absent_or_explicitly_configured():
    sources = _production_source()
    assert not any("Asia/Shanghai" in text for text in sources.values())

    route_sources = {
        path: text
        for path, text in sources.items()
        if path.startswith(("backend/api/", "backend/webui/routes/"))
    }
    assert all(".isoformat(" not in text for text in route_sources.values())

    template_sources = {
        path: text
        for path, text in sources.items()
        if path.startswith("backend/webui/templates/")
    }
    assert all("strftime(" not in text for text in template_sources.values())
    assert all(
        not re.search(r"\btoLocale(?:String|DateString|TimeString)\s*\(", text)
        for text in template_sources.values()
    )

    intl_calls = []
    for path, text in template_sources.items():
        for match in re.finditer(r"Intl\.DateTimeFormat\s*\(", text):
            intl_calls.append((path, text[match.start() : match.start() + 600]))
    assert intl_calls, "the shared browser time formatter must be present"
    assert all("timeZone" in snippet for _path, snippet in intl_calls)
