"""Static and pure-function contracts for the updater build toolchain."""

from __future__ import annotations

import importlib.util
from pathlib import Path

BUILD = Path(__file__).parents[1] / "build"
CHECKER_PATH = BUILD / "check_glibc.py"
ROOT = Path(__file__).parents[2]

BUILD_IMAGE = (
    "python:3.12-slim-bullseye@"
    "sha256:411fa4dcfdce7e7a3057c45662beba9dcd4fa36b2e50a2bfcd6c9333e59bf0db"
)
RUNTIME_IMAGE = (
    "debian:bullseye-slim@"
    "sha256:f313b4bd62667092a59b3a664d7d3ab8b5e65f41675f48e81455a15dc5abe792"
)


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_glibc", CHECKER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load checker: {CHECKER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pyinstaller_build_is_onefile_and_pinned():
    spec = (BUILD / "sakura-ai-updater.spec").read_text(encoding="utf-8")
    requirements = (BUILD / "requirements-build.txt").read_text(encoding="utf-8")

    assert "COLLECT(" not in spec
    assert "EXE(" in spec
    assert 'collect_submodules("sakura_ai_updater")' in spec
    assert "upx=False" in spec
    assert "pyinstaller==6.21.0" in requirements
    assert "pyinstaller-hooks-contrib==2026.6" in requirements


def test_root_ci_installs_updater_before_recursive_tests():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    python_quality = workflow.split("  updater-quality:", maxsplit=1)[0]

    assert "pip install -e './updater[dev]'" in python_quality


def test_build_script_has_pinned_bullseye_and_outer_elf_gate():
    script = (BUILD / "build.sh").read_text(encoding="utf-8")
    checker = CHECKER_PATH.read_text(encoding="utf-8")

    assert BUILD_IMAGE in script
    assert "Python 3.12" in script or "3.12" in script
    assert "2.31" in script
    assert "apt-get" in script
    assert "binutils" in script
    assert "build-essential" in script
    assert "pip install" in script
    assert "./updater" in script
    assert "sakura-ai-updater-linux-amd64" in script
    assert "sakura-ai-updater-linux-arm64" in script
    assert 'file "$final_binary"' in script
    assert "check_glibc.py" in script
    assert "backend status" in script
    assert "json.load" in script
    assert "docker" not in script

    assert "outer" in checker.lower()
    assert "bootloader" in checker.lower()
    assert "CArchive" in checker
    assert "readelf" in checker
    assert "(2, 31)" in checker
    assert "embedded" in checker.lower()


def test_fresh_runtime_smoke_contract():
    helper = (BUILD / "run-fresh-runtime-smoke.sh").read_text(encoding="utf-8")

    assert RUNTIME_IMAGE not in helper
    assert 'install -d -m 0700 "$state_dir" "$runtime_tmp"' in helper
    assert 'export TMPDIR="$runtime_tmp"' in helper
    assert 'install -m 0700 "$mounted_binary" "$installed_binary"' in helper
    assert 'socket_path=/run/sakura-ai/updater.sock' in helper
    assert 'backend install "${common_args[@]}"' in helper
    assert 'backend start "${common_args[@]}"' in helper
    assert 'backend status "${common_args[@]}"' in helper
    assert 'backend is-running "${common_args[@]}"' in helper
    assert 'backend stop "${common_args[@]}"' in helper
    assert 'if "$installed_binary" backend is-running "${common_args[@]}"; then' in helper
    assert 'curl --unix-socket "$socket_path" http://localhost/v1/health' in helper
    assert "apt-get update" in helper
    assert "apt-get install" in helper
    assert "curl" in helper and "passwd" in helper
    assert "infrastructure failure" in helper


def test_glibc_parser_uses_numeric_comparison():
    checker = _load_checker()
    versions = checker.parse_glibc_version_needs(
        """
Version needs section '.gnu.version_r' contains 1 entry:
  0x0010:   Name: GLIBC_2.9  Flags: none  Version: 2
  0x0020:   Name: GLIBC_2.10 Flags: none  Version: 3
  0x0030:   Name: GLIBC_2.31 Flags: none  Version: 4
  0x0040:   Name: GLIBC_PRIVATE Flags: none Version: 5
"""
    )

    assert versions == [(2, 9), (2, 10), (2, 31)]
    assert max(versions) == (2, 31)
    assert (2, 9) < (2, 10) < (2, 31) < (2, 34)


def test_gitignore_keeps_controlled_build_sources_visible():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "build/" in gitignore
    assert "dist/" in gitignore
    assert "!updater/build/" in gitignore
    assert "!updater/build/*.spec" in gitignore
    assert "!updater/build/*.sh" in gitignore
    assert "!updater/build/*.py" in gitignore
    assert "!updater/build/*.txt" in gitignore
    assert "updater/build/.pyinstaller/" in gitignore
