"""统一配置节存储（config_sections）单元测试。

覆盖：注册表完整性、deep_merge 叶子级语义、内置默认结构完整性、
无 DB 环境的 facade 回退、store 写侧辅助与启动加载。
"""

import json
from copy import deepcopy

import pytest
import yaml

from backend.core import config_sections
from backend.core.config import LabelConfig, Settings, StrategyConfig
from backend.core.config_section_defaults import (
    LABEL_SECTION_DEFAULTS,
    STRATEGY_SECTION_DEFAULTS,
)
from backend.core.config_sections import (
    deep_merge,
    get_section_config,
    get_section_defaults,
    get_sections_for_target,
)

EXPECTED_SECTION_KEYS = frozenset(
    {
        "strategy.strategies",
        "strategy.file_filters",
        "strategy.context_enhancement",
        "strategy.review_policy",
        "strategy.issue_analysis",
        "strategy.pr_summary",
        "strategy.pr_dependency_graph",
        "strategy.scan",
        "label.definitions",
        "label.recommendation",
        "label.conflict_rules",
    }
)


@pytest.fixture(autouse=True)
def _clean_section_store():
    """每个测试前后清空进程级节存储，避免测试间串扰。"""
    config_sections.clear_section_store()
    yield
    config_sections.clear_section_store()


# --- 注册表完整性 ---


def test_registry_contains_exactly_ten_section_keys():
    assert set(config_sections.SECTION_REGISTRY) == EXPECTED_SECTION_KEYS


def test_registry_sections_map_to_facade_top_level_names():
    strategy_sections = [
        spec["section"]
        for spec in config_sections.SECTION_REGISTRY.values()
        if spec["target"] == "strategy"
    ]
    label_sections = [
        spec["section"]
        for spec in config_sections.SECTION_REGISTRY.values()
        if spec["target"] == "label"
    ]
    assert strategy_sections == [
        "strategies",
        "file_filters",
        "context_enhancement",
        "review_policy",
        "issue_analysis",
        "pr_summary",
        "pr_dependency_graph",
        "scan",
    ]
    assert label_sections == ["labels", "recommendation", "conflict_rules"]


def test_registry_defaults_reference_builtin_constants():
    for key, spec in config_sections.SECTION_REGISTRY.items():
        source = (
            STRATEGY_SECTION_DEFAULTS
            if spec["target"] == "strategy"
            else LABEL_SECTION_DEFAULTS
        )
        assert spec["defaults"] is source[spec["section"]], key


# --- deep_merge 叶子级语义 ---


def test_deep_merge_user_leaf_overrides_default():
    defaults = {"a": {"x": 1, "y": 2}, "b": "keep"}
    merged = deep_merge(defaults, {"a": {"x": 99}})
    assert merged == {"a": {"x": 99, "y": 2}, "b": "keep"}


def test_deep_merge_new_default_leaf_appears_for_existing_overrides():
    # 模拟升级场景：默认新增叶子，用户旧覆盖自动补全默认值
    defaults = {"a": {"x": 1}}
    merged = deep_merge(defaults, {"a": {"x": 5}})
    upgraded_defaults = {"a": {"x": 1, "new_leaf": "default"}}
    assert deep_merge(upgraded_defaults, {"a": {"x": 5}}) == {
        "a": {"x": 5, "new_leaf": "default"}
    }
    assert merged == {"a": {"x": 5}}


def test_deep_merge_recurses_nested_dicts():
    defaults = {"l1": {"l2": {"l3": {"x": 1, "y": 2}}}}
    merged = deep_merge(defaults, {"l1": {"l2": {"l3": {"y": 9}}}})
    assert merged["l1"]["l2"]["l3"] == {"x": 1, "y": 9}


def test_deep_merge_replaces_list_wholesale():
    defaults = {"items": [1, 2, 3], "nested": {"items": [1, 2]}}
    merged = deep_merge(defaults, {"items": [9], "nested": {"items": []}})
    assert merged["items"] == [9]
    assert merged["nested"]["items"] == []


def test_deep_merge_replaces_dict_with_scalar():
    # 用户把默认 dict 位置改成标量：按叶子整体替换（不合并、不报错）
    merged = deep_merge({"a": {"x": 1}}, {"a": "scalar"})
    assert merged == {"a": "scalar"}


def test_deep_merge_returns_copy_and_does_not_mutate_inputs():
    defaults = {"a": {"x": [1, 2]}}
    overrides = {"a": {"y": [3]}}
    merged = deep_merge(defaults, overrides)
    merged["a"]["x"].append(999)
    assert defaults["a"]["x"] == [1, 2]
    assert overrides["a"]["y"] == [3]
    assert merged["a"]["x"] == [1, 2, 999]


# --- 内置默认结构完整性 ---


def test_builtin_strategy_defaults_contain_four_tiers():
    strategies = STRATEGY_SECTION_DEFAULTS["strategies"]
    assert set(strategies) == {"quick", "standard", "deep", "large"}
    for name, spec in strategies.items():
        assert spec["name"], name
        assert "max_files" in spec["conditions"], name
        assert "max_lines" in spec["conditions"], name
        assert spec["prompt"], name


def test_builtin_file_filters_contain_three_lists():
    filters = STRATEGY_SECTION_DEFAULTS["file_filters"]
    assert set(filters) == {"skip_extensions", "skip_paths", "code_extensions"}
    for key, value in filters.items():
        assert isinstance(value, list) and value, key


def test_builtin_label_defaults_contain_sixteen_labels():
    labels = LABEL_SECTION_DEFAULTS["labels"]
    assert len(labels) == 16
    for name, spec in labels.items():
        assert isinstance(spec["color"], str) and len(spec["color"]) == 6, name
        assert isinstance(spec["description"], str), name


def test_builtin_conflict_rules_not_empty():
    rules = LABEL_SECTION_DEFAULTS["conflict_rules"]
    assert rules
    for key, blocked in rules.items():
        assert isinstance(blocked, list), key


def test_builtin_recommendation_settings_shape():
    recommendation = LABEL_SECTION_DEFAULTS["recommendation"]
    assert set(recommendation) == {"enabled", "confidence_threshold", "auto_create"}


# --- 无 DB 环境的 facade 回退（_section_store 未初始化） ---


def test_strategy_config_facade_uses_builtin_defaults_without_db():
    config = StrategyConfig()
    assert set(config.get_all_strategies()) == {"quick", "standard", "deep", "large"}
    assert config.get_file_filters() == STRATEGY_SECTION_DEFAULTS["file_filters"]
    assert (
        config.get_context_enhancement_config()
        == STRATEGY_SECTION_DEFAULTS["context_enhancement"]
    )
    assert config.config["review_policy"] == STRATEGY_SECTION_DEFAULTS["review_policy"]
    assert config.config["pr_summary"] == STRATEGY_SECTION_DEFAULTS["pr_summary"]
    assert (
        config.config["pr_dependency_graph"]
        == STRATEGY_SECTION_DEFAULTS["pr_dependency_graph"]
    )
    # 回退行为统一：内置默认而非 raise / 空 dict
    assert config.determine_strategy(file_count=1, line_count=1) == "quick"


def test_label_config_facade_uses_builtin_defaults_without_db():
    config = LabelConfig()
    assert config.get_labels() == LABEL_SECTION_DEFAULTS["labels"]
    assert config.get_recommendation_settings() == LABEL_SECTION_DEFAULTS["recommendation"]
    assert config.get_conflict_rules() == LABEL_SECTION_DEFAULTS["conflict_rules"]


def test_sections_for_target_cover_all_top_level_names():
    strategy_sections = get_sections_for_target("strategy")
    assert set(strategy_sections) == set(STRATEGY_SECTION_DEFAULTS)
    label_sections = get_sections_for_target("label")
    assert set(label_sections) == set(LABEL_SECTION_DEFAULTS)


def test_sakura_memory_section_is_single_source_of_truth():
    """双轨合并后：sakura_memory 嵌套节是唯一事实源，平铺 Settings 键全灭。"""
    sakura = StrategyConfig().get_context_enhancement_config()["sakura_memory"]

    # 全部保留旋钮仍在节内 / All retained knobs remain in the section
    assert sakura["enabled"] is True
    assert sakura["reflection"]["enabled"] is True
    assert sakura["issue_reflection"]["enabled"] is True
    assert sakura["consolidation"]["interval"] == 5
    assert sakura["consolidation"]["max_memory_chars"] == 2000
    assert sakura["consolidation"]["max_sakura_chars"] == 3000
    assert sakura["consolidation"]["partial_commit"] is True
    assert sakura["knowledge_extraction"]["enabled"] is True
    assert sakura["knowledge_extraction"]["min_reflections"] == 15
    assert sakura["initialization"]["auto_init"] is True
    assert sakura["directory_convention"]["auto_create_subdirs"] is True

    # 两个迭代键叶子已不存在 / The two iteration-cap leaves are gone
    assert "max_iterations" not in sakura["knowledge_extraction"]
    assert "max_iterations" not in sakura["consolidation"]

    # 13 个平铺 Settings 字段已删除 / The 13 flat Settings fields are gone
    removed_flat_fields = {
        "sakura_memory_enabled",
        "sakura_reflection_enabled",
        "sakura_issue_reflection_enabled",
        "sakura_consolidation_interval",
        "sakura_max_memory_chars",
        "sakura_max_sakura_chars",
        "sakura_auto_init",
        "sakura_consolidation_partial_commit",
        "sakura_knowledge_extraction_enabled",
        "sakura_extraction_min_reflections",
        "sakura_extraction_max_iterations",
        "sakura_consolidation_max_iterations",
        "sakura_auto_create_subdirs",
    }
    assert not removed_flat_fields & set(Settings.model_fields)


# --- store 写侧辅助与覆盖合并 ---


def test_update_section_store_overrides_only_requested_leaves():
    config_sections.update_section_store(
        "strategy.strategies", {"standard": {"name": "改名"}}
    )
    merged = get_section_config("strategy.strategies")
    assert merged["standard"]["name"] == "改名"
    # 未覆盖的叶子保持默认（prompt 与 conditions）
    assert merged["standard"]["prompt"] == STRATEGY_SECTION_DEFAULTS["strategies"][
        "standard"
    ]["prompt"]
    assert merged["standard"]["conditions"] == {"max_files": 50, "max_lines": 20000}
    # 其他节不受影响
    assert "quick" in merged
    assert get_section_config("label.definitions") == LABEL_SECTION_DEFAULTS["labels"]


def test_update_section_store_rejects_unknown_key_and_non_dict():
    with pytest.raises(KeyError):
        config_sections.update_section_store("strategy.unknown", {})
    with pytest.raises(ValueError):
        config_sections.update_section_store("strategy.strategies", ["not-a-dict"])


def test_clear_section_store_restores_builtin_defaults():
    config_sections.update_section_store("label.recommendation", {"enabled": False})
    assert get_section_config("label.recommendation")["enabled"] is False
    config_sections.clear_section_store("label.recommendation")
    assert get_section_config("label.recommendation") == LABEL_SECTION_DEFAULTS[
        "recommendation"
    ]


def test_get_section_defaults_returns_mutable_copy():
    defaults = get_section_defaults("label.definitions")
    defaults["bug"]["color"] = "000000"
    assert (
        get_section_defaults("label.definitions")["bug"]["color"]
        == LABEL_SECTION_DEFAULTS["labels"]["bug"]["color"]
    )


def test_get_section_config_rejects_unknown_key():
    with pytest.raises(KeyError):
        get_section_config("strategy.unknown")


# --- load_section_configs（fake session，无真实 DB） ---


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        return _FakeResult(self._rows)


@pytest.mark.asyncio
async def test_load_section_configs_fills_store_from_db_rows():
    rows = [
        ("strategy.strategies", json.dumps({"standard": {"name": "X"}})),
        ("label.recommendation", json.dumps({"enabled": False})),
    ]
    await config_sections.load_section_configs(_FakeSession(rows))

    merged = get_section_config("strategy.strategies")
    assert merged["standard"]["name"] == "X"
    assert "quick" in merged
    assert get_section_config("label.recommendation")["enabled"] is False
    # 未覆盖的键回退内置默认
    assert get_section_config("label.definitions") == LABEL_SECTION_DEFAULTS["labels"]


@pytest.mark.asyncio
async def test_load_section_configs_skips_missing_and_invalid_keys():
    rows = [
        # 键存在但值为空：跳过
        ("strategy.strategies", None),
        # 非 JSON 文本：跳过
        ("strategy.review_policy", "not-json{"),
        # JSON 但非 dict：跳过
        ("strategy.issue_analysis", json.dumps(["not", "a", "dict"])),
    ]
    await config_sections.load_section_configs(_FakeSession(rows))

    assert config_sections._section_store == {}
    assert get_section_config("strategy.strategies") == STRATEGY_SECTION_DEFAULTS[
        "strategies"
    ]


@pytest.mark.asyncio
async def test_load_section_configs_replaces_previous_store():
    config_sections.update_section_store("label.definitions", {"bug": {}})
    rows = [("label.recommendation", json.dumps({"auto_create": False}))]
    await config_sections.load_section_configs(_FakeSession(rows))

    # 重新加载整体替换旧 store，而非叠加
    assert set(config_sections._section_store) == {"label.recommendation"}
    assert get_section_config("label.definitions") == LABEL_SECTION_DEFAULTS["labels"]


# ============================================================================
# 一次性迁移：migrate_yaml_files_to_db（旧 YAML 差异节导入 DB）
# ============================================================================


class _ExecRows:
    """模拟单列 select 的 execute 结果。"""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _MigrationSession:
    """迁移所需最小 AsyncSession：节键存在性查询 + add_all + commit/rollback。"""

    def __init__(self, existing_key_names=()):
        self.existing_rows = [(name,) for name in existing_key_names]
        self.added = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, _stmt):
        return _ExecRows(self.existing_rows)

    def add_all(self, items) -> None:
        self.added.extend(items)

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


def _write_yaml(path, data) -> None:
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def test_prune_default_equal_leaves_semantics():
    prune = config_sections._prune_default_equal_leaves
    sentinel = config_sections._PRUNED

    defaults = {"a": {"x": 1, "y": 2}, "b": "keep", "lst": [1, 2]}
    # 完全一致 → 剪除
    assert prune(defaults, deepcopy(defaults)) is sentinel
    # 值等价子集（缺少键不算差异）→ 剪除
    assert prune(defaults, {"a": {"x": 1}, "b": "keep"}) is sentinel
    # 仅保留差异叶子
    assert prune(defaults, {"a": {"x": 1, "y": 9}}) == {"a": {"y": 9}}
    # 列表整体替换语义
    assert prune(defaults, {"lst": [3]}) == {"lst": [3]}
    # 用户额外键保留
    assert prune(defaults, {"extra": "mine"}) == {"extra": "mine"}
    # 类型不匹配（dict → 标量）保留整体值
    assert prune(defaults, {"a": "scalar"}) == {"a": "scalar"}


@pytest.mark.asyncio
async def test_migrate_imports_only_diff_sections_and_prunes_default_leaves(tmp_path):
    strategies = deepcopy(STRATEGY_SECTION_DEFAULTS)
    strategies["strategies"]["standard"]["prompt"] = "custom standard prompt"
    # 与默认完全一致的节 → 不导入
    strategies["file_filters"] = deepcopy(STRATEGY_SECTION_DEFAULTS["file_filters"])
    # 值等价子集节（只含与默认相同的 system_prompt）→ 不导入
    strategies["pr_summary"] = {
        "system_prompt": STRATEGY_SECTION_DEFAULTS["pr_summary"]["system_prompt"]
    }
    strategies_path = tmp_path / "strategies.yaml"
    _write_yaml(strategies_path, strategies)

    labels = deepcopy(LABEL_SECTION_DEFAULTS)
    labels["labels"]["bug"]["color"] = "000000"
    labels_path = tmp_path / "labels.yaml"
    _write_yaml(labels_path, labels)

    session = _MigrationSession()
    imported = await config_sections.migrate_yaml_files_to_db(
        session, strategies_path=strategies_path, labels_path=labels_path
    )

    assert imported == ["strategy.strategies", "label.definitions"]
    assert session.committed is True
    assert session.rolled_back is False
    rows = {row.key_name: json.loads(row.key_value) for row in session.added}
    # 叶子级剪枝：DB 只存用户差异，不物化与默认相同的叶子
    assert rows["strategy.strategies"] == {"standard": {"prompt": "custom standard prompt"}}
    assert rows["label.definitions"] == {"bug": {"color": "000000"}}
    # 写入的覆盖经 deep_merge 能还原出完整节值
    restored = deep_merge(
        STRATEGY_SECTION_DEFAULTS["strategies"], rows["strategy.strategies"]
    )
    assert restored["standard"]["prompt"] == "custom standard prompt"
    assert restored["quick"] == STRATEGY_SECTION_DEFAULTS["strategies"]["quick"]


@pytest.mark.asyncio
async def test_migrate_keeps_user_keys_missing_from_defaults(tmp_path):
    strategies = {
        "strategies": {
            "custom": {"name": "X", "prompt": "Y", "conditions": {"max_files": 1}}
        }
    }
    strategies_path = tmp_path / "strategies.yaml"
    _write_yaml(strategies_path, strategies)

    session = _MigrationSession()
    imported = await config_sections.migrate_yaml_files_to_db(
        session,
        strategies_path=strategies_path,
        labels_path=tmp_path / "labels.yaml",
    )

    assert imported == ["strategy.strategies"]
    stored = json.loads(session.added[0].key_value)
    # 默认中不存在的用户自定义键整体保留
    assert stored == strategies["strategies"]


@pytest.mark.asyncio
async def test_migrate_skips_when_any_section_key_already_in_db(tmp_path):
    strategies_path = tmp_path / "strategies.yaml"
    _write_yaml(strategies_path, deepcopy(STRATEGY_SECTION_DEFAULTS))

    session = _MigrationSession(existing_key_names=("label.definitions",))
    imported = await config_sections.migrate_yaml_files_to_db(
        session,
        strategies_path=strategies_path,
        labels_path=tmp_path / "labels.yaml",
    )

    assert imported == []
    assert session.added == []
    assert session.committed is False


@pytest.mark.asyncio
async def test_migrate_skips_when_files_missing(tmp_path):
    session = _MigrationSession()
    imported = await config_sections.migrate_yaml_files_to_db(
        session,
        strategies_path=tmp_path / "strategies.yaml",
        labels_path=tmp_path / "labels.yaml",
    )

    assert imported == []
    assert session.added == []
    assert session.committed is False


@pytest.mark.asyncio
async def test_migrate_is_idempotent_on_repeated_calls(tmp_path):
    strategies = deepcopy(STRATEGY_SECTION_DEFAULTS)
    strategies["strategies"]["standard"]["prompt"] = "custom standard prompt"
    strategies_path = tmp_path / "strategies.yaml"
    _write_yaml(strategies_path, strategies)

    first = _MigrationSession()
    imported_first = await config_sections.migrate_yaml_files_to_db(
        first, strategies_path=strategies_path, labels_path=tmp_path / "labels.yaml"
    )
    assert imported_first == ["strategy.strategies"]

    # 第二次调用时 DB 已有节键（模拟首次落库后的重启），整体跳过
    second = _MigrationSession(existing_key_names=imported_first)
    imported_second = await config_sections.migrate_yaml_files_to_db(
        second, strategies_path=strategies_path, labels_path=tmp_path / "labels.yaml"
    )
    assert imported_second == []
    assert second.added == []


@pytest.mark.asyncio
async def test_migrate_skips_invalid_yaml_file(tmp_path):
    # 损坏的 strategies.yaml → 跳过该文件；正常的 labels.yaml 继续导入
    (tmp_path / "strategies.yaml").write_text(
        "strategies: [unterminated\n", encoding="utf-8"
    )
    labels = deepcopy(LABEL_SECTION_DEFAULTS)
    labels["recommendation"]["enabled"] = False
    labels_path = tmp_path / "labels.yaml"
    _write_yaml(labels_path, labels)

    session = _MigrationSession()
    imported = await config_sections.migrate_yaml_files_to_db(
        session,
        strategies_path=tmp_path / "strategies.yaml",
        labels_path=labels_path,
    )

    assert imported == ["label.recommendation"]


@pytest.mark.asyncio
async def test_migrate_skips_section_with_non_dict_value(tmp_path):
    strategies_path = tmp_path / "strategies.yaml"
    _write_yaml(strategies_path, {"file_filters": "oops"})

    session = _MigrationSession()
    imported = await config_sections.migrate_yaml_files_to_db(
        session,
        strategies_path=strategies_path,
        labels_path=tmp_path / "labels.yaml",
    )

    assert imported == []
    assert session.added == []
