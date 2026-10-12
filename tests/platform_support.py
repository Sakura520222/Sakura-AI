"""Explicit prerequisites for platform-specific integration tests."""

import os
import shutil

import pytest


def posix_bash() -> str:
    if os.name != "posix":
        pytest.skip("Linux/POSIX deployment scripts require a POSIX Bash host")
    executable = shutil.which("bash")
    if executable is None:
        pytest.skip("POSIX Bash is not installed")
    return executable
