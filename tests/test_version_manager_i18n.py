"""Version manager template/catalog internationalisation contracts."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "backend/webui/templates/version_manager.html"
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


def test_version_manager_template_static_keys_exist_in_both_catalogs():
    template = TEMPLATE.read_text(encoding="utf-8")
    keys = {
        key
        for key in re.findall(r'_\("version_manager\.([A-Za-z0-9_]+)"', template)
        if key not in {"deployment_", "state_"}
    }
    assert keys
    for catalog_path in CATALOGS:
        catalog = _version_manager_catalog(catalog_path)
        missing = sorted(keys - set(catalog))
        assert not missing, f"{catalog_path.name}: missing version_manager keys {missing}"


def test_version_manager_has_no_hardcoded_user_status_labels():
    template = TEMPLATE.read_text(encoding="utf-8")
    for literal in (
        "最近检查",
        "尚未检查",
        "检查失败：",
        "已是最新版本",
        "暂无 Release 信息",
        "查看完整说明",
        "Release 历史",
        "已连接",
        "未连接",
    ):
        assert literal not in template
