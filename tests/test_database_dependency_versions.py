"""Database dependency compatibility regression tests."""

from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[1]
SQLALCHEMY_AIOMYSQL_PRE_PING_FIX_VERSION = Version("2.0.26")


def _load_requirement(package_name: str) -> Requirement:
    """从 requirements.txt 读取指定依赖。"""
    normalized_name = package_name.lower()
    for raw_line in (
        (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    ):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue

        requirement = Requirement(line)
        if requirement.name.lower() == normalized_name:
            return requirement

    raise AssertionError(f"Missing dependency requirement: {package_name}")


def _configured_minimum_version(requirement: Requirement) -> Version:
    """返回依赖声明中的最低可安装版本。"""
    exact_versions = [
        Version(specifier.version)
        for specifier in requirement.specifier
        if specifier.operator == "=="
    ]
    if exact_versions:
        return min(exact_versions)

    lower_bounds = [
        Version(specifier.version)
        for specifier in requirement.specifier
        if specifier.operator in {">=", "~="}
    ]
    if not lower_bounds:
        raise AssertionError(
            f"{requirement.name} must declare a lower bound for reproducible installs"
        )

    return max(lower_bounds)


def test_sqlalchemy_version_keeps_aiomysql_pool_pre_ping_fix():
    """SQLAlchemy 2.0.25 会触发 aiomysql pool_pre_ping 兼容问题。"""
    sqlalchemy_requirement = _load_requirement("sqlalchemy")

    assert (
        _configured_minimum_version(sqlalchemy_requirement)
        >= SQLALCHEMY_AIOMYSQL_PRE_PING_FIX_VERSION
    )
