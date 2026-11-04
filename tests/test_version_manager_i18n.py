"""Version manager catalog internationalisation contracts."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
CATALOGS = [
    ROOT / "backend/webui/translations/zh-CN.yaml",
    ROOT / "backend/webui/translations/en.yaml",
]


def _version_manager_catalog(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return data["version_manager"]


def test_version_manager_catalogs_have_identical_keys():
    zh = _version_manager_catalog(CATALOGS[0])
    en = _version_manager_catalog(CATALOGS[1])
    assert set(zh) == set(en)
    assert all(isinstance(value, str) and value for value in zh.values())
    assert all(isinstance(value, str) and value for value in en.values())
