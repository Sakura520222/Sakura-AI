"""Strict SemVer parser and precedence contracts."""

from __future__ import annotations

import pytest
from sakura_ai_updater.semver import is_newer_version, parse_semver


@pytest.mark.parametrize(
    ("value", "core"),
    [
        ("0.0.0", (0, 0, 0)),
        ("3.1.0", (3, 1, 0)),
        ("1.2.3-alpha.1+build.42", (1, 2, 3)),
    ],
)
def test_parse_semver_accepts_valid_values(value: str, core: tuple[int, int, int]):
    parsed = parse_semver(value)

    assert parsed is not None
    assert parsed.core == core
    assert str(parsed) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "v1.2.3",
        "1.2",
        "1.2.3.4",
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1.2.3-01",
        "1.2.3-",
        "1.2.3+",
        "1.2.3-alpha..1",
        " 1.2.3",
        "1.2.3 ",
    ],
)
def test_parse_semver_rejects_non_semver_values(value: str):
    assert parse_semver(value) is None


def test_parse_semver_rejects_non_string_values():
    assert parse_semver(None) is None  # type: ignore[arg-type]
    assert parse_semver(123) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("current", "candidate", "expected"),
    [
        ("1.0.0", "1.0.1", True),
        ("1.0.0-alpha", "1.0.0", True),
        ("1.0.0-alpha.1", "1.0.0-alpha.beta", True),
        ("1.0.0+old", "1.0.0+new", False),
    ],
)
def test_is_newer_version_obeys_semver_precedence(
    current: str, candidate: str, expected: bool
):
    assert is_newer_version(current, candidate) is expected


def test_is_newer_version_rejects_invalid_or_downgrade_values():
    assert is_newer_version("1.0.0", "1.0.0") is False
    assert is_newer_version("2.0.0", "1.9.9") is False
    assert is_newer_version("1.0.0", "01.2.3") is False


def test_equal_build_metadata_values_have_equal_hashes():
    first = parse_semver("1.2.3+linux-amd64")
    second = parse_semver("1.2.3+linux-arm64")

    assert first is not None and second is not None
    assert first == second
    assert hash(first) == hash(second)
    assert {first, second} == {first}
