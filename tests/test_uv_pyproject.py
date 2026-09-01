"""uv 项目清单契约 / uv project manifest contracts.

校验根 pyproject.toml 的依赖与 requirements.txt 逐条同步、Python 版本约束一致,
以及 uv 将根项目作为虚拟应用(不构建、不安装)。
Verifies that the root pyproject.toml dependencies stay line-by-line in sync with
requirements.txt (the pip/Docker source of truth), that Python version constraints
agree, and that uv treats the root project as a virtual application.
"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _requirements_entries() -> list[str]:
    """解析 requirements.txt,剥离行内注释与空行后的需求条目列表。

    Parse requirements.txt into requirement specifiers with inline
    comments and blank lines stripped.
    """
    entries: list[str] = []
    for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            entries.append(" ".join(line.split()))  # 归一化空白 / normalize whitespace
    return entries


def test_pyproject_dependencies_mirror_requirements_txt() -> None:
    """requirements.txt 与 pyproject dependencies 集合一致(顺序无关)。"""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert sorted(data["project"]["dependencies"]) == sorted(_requirements_entries())


def test_pyproject_targets_python_314() -> None:
    """requires-python 与 .python-version 必须一致指向 3.14。"""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["requires-python"] == ">=3.14"
    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.14"


def test_uv_treats_project_as_virtual_application() -> None:
    """根项目为虚拟应用:不构建、不安装,以 python -m backend.main 运行。"""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["tool"]["uv"]["package"] is False
    assert "build-system" not in data
    assert "scripts" not in data["project"]


def test_dev_group_installs_updater_editable() -> None:
    """dev 组通过 editable path 依赖安装 updater 及其 dev extra。"""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "sakura-ai-updater[dev]" in data["dependency-groups"]["dev"]
    source = data["tool"]["uv"]["sources"]["sakura-ai-updater"]
    assert source["editable"] is True
    assert source["path"] == "updater"
