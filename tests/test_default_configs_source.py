"""app_config 种子默认行单一来源回归测试。

S4 统一默认表后（docs/plans/2026-08-16-unified-config-store.md §6.1/§6.2），
DB 种子行必须从 Settings 派生，防止再次出现两份手抄表在
review_timeout_seconds (600/300)、web_search_enabled (true/false) 与
web_search_* 限制键上的分叉。
"""

import pytest

from backend.core.config import DYNAMIC_CONFIG_GROUPS, get_settings
from backend.models import database as database_module


def _seed_rows() -> list:
    """构建与建库路径完全一致的种子行（app_version + 动态组补插）。"""
    configs = database_module._build_default_configs()
    database_module._append_dynamic_config_defaults(configs)
    return configs


def _seed_map() -> dict[str, str]:
    return {row.key_name: row.key_value for row in _seed_rows()}


# --- 单一来源结构 ---


def test_static_builder_only_yields_app_version():
    """静态构建器仅产出 app_version 行，其余键全部由动态组补插。"""
    rows = database_module._build_default_configs()
    assert [row.key_name for row in rows] == ["app_version"]


def test_seed_rows_cover_every_dynamic_group_key():
    """每个 DYNAMIC 组键都必须有且仅有一行种子，且值来自 Settings。"""
    settings = get_settings()
    seed_map = _seed_map()
    dynamic_keys = {
        key for group in DYNAMIC_CONFIG_GROUPS.values() for key in group["keys"]
    }
    assert dynamic_keys <= set(seed_map), (
        f"动态组键缺少种子行: {sorted(dynamic_keys - set(seed_map))}"
    )
    for key in dynamic_keys:
        expected = str(getattr(settings, key, ""))
        assert seed_map[key] == expected, (
            f"键 {key} 种子值 {seed_map[key]!r} 与 Settings 默认 {expected!r} 分叉"
        )


def test_full_seed_list_has_no_duplicate_keys():
    rows = _seed_rows()
    keys = [row.key_name for row in rows]
    assert len(keys) == len(set(keys)), "建库种子行存在重复键"


# --- 历史冲突键的绝对值断言（防手抄回归）---


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        # review_timeout_seconds 曾在同步路径手抄为 "300"
        ("review_timeout_seconds", "600"),
        # web_search_enabled 曾在同步路径手抄为 "false"（R3 起随动态组以
        # str() 序列化 bool，格式为 "True"，_cast_config_type 大小写兼容）
        ("web_search_enabled", "True"),
        # 以下三键的 DB 硬编码行曾偏离 Settings 默认（3/500/15/15）
        ("web_search_max_results", "5"),
        ("web_search_max_content_length", "2000"),
        ("web_search_timeout", "30"),
    ],
)
def test_critical_seed_values(key: str, expected: str):
    assert _seed_map()[key] == expected


def test_app_version_row_uses_module_constant():
    rows = database_module._build_default_configs()
    version_rows = [r for r in rows if r.key_name == "app_version"]
    assert len(version_rows) == 1
    # app_version 单一来源化（backend.__version__ 派生）列入后续路线，暂为字面量
    assert version_rows[0].key_value == database_module.APP_VERSION_DEFAULT
