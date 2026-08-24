"""统一配置节服务（SectionConfigService）单元测试。

覆盖：save/reset 往返一致、校验失败拒绝写入、大文本审计不落全文、
保存后 reload 与 store 同步、非法 JSON 节容错、patch 模式保留未提交叶子。
"""

from __future__ import annotations

import json

import pytest

from backend.core import config_sections
from backend.core.config_section_defaults import (
    LABEL_SECTION_DEFAULTS,
    STRATEGY_SECTION_DEFAULTS,
)
from backend.services import section_config_service as service_module
from backend.services.section_config_service import (
    SECTION_VALIDATORS,
    section_config_service,
)


@pytest.fixture(autouse=True)
def _clean_section_store():
    """每个测试前后清空进程级节存储，避免测试间串扰。"""
    config_sections.clear_section_store()
    yield
    config_sections.clear_section_store()


class _Result:
    """模拟 SQLAlchemy execute 结果（单键场景）。"""

    def __init__(self, rows: list):
        self._rows = rows

    def scalar_one_or_none(self):
        if not self._rows:
            return None
        if len(self._rows) > 1:
            raise AssertionError("单键查询不应返回多行")
        return self._rows[0]


class _FakeSession:
    """最小 AsyncSession 模拟：select/add/delete/commit/rollback。

    commit 时把 added 行并入 rows，delete 时移除，模拟事务生效。
    """

    def __init__(self, rows: list | None = None):
        self.rows: list = list(rows or [])
        self.added: list = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, _stmt):
        return _Result(self.rows)

    def add(self, row) -> None:
        self.added.append(row)

    async def delete(self, row) -> None:
        if row in self.rows:
            self.rows.remove(row)

    async def commit(self) -> None:
        self.committed = True
        self.rows.extend(self.added)
        self.added = []

    async def rollback(self) -> None:
        self.rolled_back = True
        self.added = []


# --- 校验器注册表完整性 ---


def test_validators_cover_every_registered_section():
    assert set(SECTION_VALIDATORS) == set(config_sections.SECTION_REGISTRY)


# --- save / reset 往返一致 ---


@pytest.mark.asyncio
async def test_save_and_load_round_trip():
    db = _FakeSession()
    data = {
        "bug": {"color": "ff0000", "description": "重定义"},
        "question": {"color": "00ff00", "description": "提问"},
    }

    result = await section_config_service.save_section(db, "label.definitions", data)

    assert result["changed"] is True
    loaded = await section_config_service.load_section(db, "label.definitions")
    # 未覆盖的标签保留默认，覆盖的标签生效
    assert loaded["bug"] == {"color": "ff0000", "description": "重定义"}
    assert loaded["documentation"] == LABEL_SECTION_DEFAULTS["labels"]["documentation"]
    # DB 保存的是覆盖值（不含默认标签）
    stored = json.loads(db.rows[0].key_value)
    assert stored == data
    assert db.rows[0].key_name == "label.definitions"


@pytest.mark.asyncio
async def test_reset_restores_builtin_defaults():
    db = _FakeSession()
    await section_config_service.save_section(
        db, "label.recommendation", {"enabled": False, "confidence_threshold": 0.9}
    )
    assert db.rows

    result = await section_config_service.reset_section(db, "label.recommendation")

    assert result["existed"] is True
    assert db.rows == []
    loaded = await section_config_service.load_section(db, "label.recommendation")
    assert loaded == LABEL_SECTION_DEFAULTS["recommendation"]
    # store 覆盖也被清除
    assert (
        config_sections.get_section_config("label.recommendation")
        == LABEL_SECTION_DEFAULTS["recommendation"]
    )


@pytest.mark.asyncio
async def test_reset_without_existing_row_is_noop():
    db = _FakeSession()
    result = await section_config_service.reset_section(db, "strategy.pr_summary")
    assert result == {"section": "strategy.pr_summary", "existed": False}
    assert db.committed is False


# --- 校验失败拒绝写入 ---


@pytest.mark.asyncio
async def test_save_rejects_non_positive_strategy_conditions():
    db = _FakeSession()
    data = {
        "standard": {
            "name": "标准",
            "conditions": {"max_files": 0, "max_lines": 100},
            "prompt": "Review.",
        }
    }

    with pytest.raises(ValueError, match="max_files"):
        await section_config_service.save_section(db, "strategy.strategies", data)

    # 校验失败：不落库、不更新 store
    assert db.added == [] and db.rows == [] and db.committed is False
    assert (
        config_sections.get_section_config("strategy.strategies")
        == STRATEGY_SECTION_DEFAULTS["strategies"]
    )


@pytest.mark.asyncio
async def test_save_rejects_invalid_label_color():
    db = _FakeSession()
    with pytest.raises(ValueError, match="颜色格式错误"):
        await section_config_service.save_section(
            db, "label.definitions", {"bug": {"color": "#zzzzz", "description": "x"}}
        )
    assert db.rows == [] and db.committed is False


@pytest.mark.asyncio
async def test_save_rejects_out_of_range_confidence_threshold():
    db = _FakeSession()
    with pytest.raises(ValueError, match="confidence_threshold"):
        await section_config_service.save_section(
            db, "label.recommendation", {"confidence_threshold": 1.5}
        )
    assert db.rows == [] and db.committed is False


@pytest.mark.asyncio
async def test_save_rejects_template_placeholder_loss():
    db = _FakeSession()
    # pr_summary.user_template 默认占位符：title/file_count/additions/deletions/
    # file_list/commits，程序化提取自内置默认
    broken = {"user_template": "总结 {title} 的变更"}
    with pytest.raises(ValueError, match="丢失必需占位符"):
        await section_config_service.save_section(db, "strategy.pr_summary", broken)
    assert db.rows == [] and db.committed is False


@pytest.mark.asyncio
async def test_save_accepts_template_with_extra_placeholders():
    db = _FakeSession()
    template = (
        "请总结以下 PR 的变更内容：\n\nPR 标题: {title}\n"
        "变更文件数: {file_count}\n代码变更: +{additions}/-{deletions}\n\n"
        "变更文件列表:\n{file_list}\n\nCommit 信息:\n{commits}\n\n仓库: {repository}"
    )
    result = await section_config_service.save_section(
        db, "strategy.pr_summary", {"user_template": template}
    )
    assert result["changed"] is True


# --- 大文本审计不落全文 ---


@pytest.mark.asyncio
async def test_prompt_section_audit_uses_digest_not_full_text():
    db = _FakeSession()
    long_prompt = "你是资深审查员。\n" + "深度审查要点。\n" * 50

    result = await section_config_service.save_section(
        db, "strategy.pr_summary", {"system_prompt": long_prompt}
    )

    change = result["changes"]["system_prompt"]
    assert long_prompt not in json.dumps(result["changes"], ensure_ascii=False)
    assert "sha256=" in change["new"]
    assert f"len={len(long_prompt)}" in change["new"]


@pytest.mark.asyncio
async def test_non_prompt_section_audit_keeps_plain_values():
    db = _FakeSession()
    result = await section_config_service.save_section(
        db, "label.recommendation", {"enabled": False}
    )
    assert result["changes"]["enabled"] == {"old": True, "new": False}
    assert "sha256" not in json.dumps(result["changes"])


# --- 保存后 reload 与 store 同步 ---


@pytest.mark.asyncio
async def test_save_triggers_strategy_reload_and_updates_store(monkeypatch):
    calls = []
    monkeypatch.setattr(
        service_module, "reload_strategy_config", lambda: calls.append("strategy")
    )
    db = _FakeSession()

    await section_config_service.save_section(
        db,
        "strategy.strategies",
        {"quick": {"name": "⚡️ 极速", "prompt": "Fast pass."}},
    )

    assert calls == ["strategy"]
    merged = config_sections.get_section_config("strategy.strategies")
    assert merged["quick"]["name"] == "⚡️ 极速"
    # 未覆盖的叶子保持默认
    assert merged["quick"]["conditions"] == {"max_files": 10, "max_lines": 5000}
    assert merged["standard"] == STRATEGY_SECTION_DEFAULTS["strategies"]["standard"]


@pytest.mark.asyncio
async def test_save_triggers_label_reload(monkeypatch):
    calls = []
    monkeypatch.setattr(
        service_module, "reload_label_config", lambda: calls.append("label")
    )
    db = _FakeSession()

    await section_config_service.save_section(
        db, "label.conflict_rules", {"enhancement": ["bug", "performance"]}
    )

    assert calls == ["label"]


# --- 非法 JSON 节容错 ---


@pytest.mark.asyncio
async def test_load_section_tolerates_invalid_json_row():
    from backend.models.database import AppConfig

    db = _FakeSession(
        [
            AppConfig(
                key_name="strategy.review_policy",
                key_value="not-json{",
                description="corrupted",
            )
        ]
    )

    loaded = await section_config_service.load_section(db, "strategy.review_policy")

    assert loaded == STRATEGY_SECTION_DEFAULTS["review_policy"]


@pytest.mark.asyncio
async def test_load_section_tolerates_non_dict_json_row():
    from backend.models.database import AppConfig

    db = _FakeSession(
        [
            AppConfig(
                key_name="strategy.issue_analysis",
                key_value=json.dumps(["not", "a", "dict"]),
                description="corrupted",
            )
        ]
    )

    loaded = await section_config_service.load_section(db, "strategy.issue_analysis")

    assert loaded == STRATEGY_SECTION_DEFAULTS["issue_analysis"]


@pytest.mark.asyncio
async def test_load_section_rejects_unknown_key():
    with pytest.raises(KeyError):
        await section_config_service.load_section(_FakeSession(), "strategy.unknown")


# --- patch 模式与默认等价保存 ---


@pytest.mark.asyncio
async def test_patch_mode_preserves_unrelated_leaves():
    db = _FakeSession()
    # 先保存包含表单外字段的覆盖
    await section_config_service.save_section(
        db,
        "strategy.context_enhancement",
        {
            "max_structure_files": 123,
            "sakura_memory": {"enabled": False},
        },
    )

    # patch 模式只更新提交的叶子，未提交的保留
    await section_config_service.save_section(
        db,
        "strategy.context_enhancement",
        {"max_structure_files": 456},
        mode="patch",
    )

    loaded = await section_config_service.load_section(
        db, "strategy.context_enhancement"
    )
    assert loaded["max_structure_files"] == 456
    # 合并语义：sakura_memory 覆盖 enabled，其余默认叶子自动补全
    assert loaded["sakura_memory"]["enabled"] is False
    assert (
        loaded["sakura_memory"]["knowledge_extraction"]
        == STRATEGY_SECTION_DEFAULTS["context_enhancement"]["sakura_memory"][
            "knowledge_extraction"
        ]
    )
    # patch 不删既有覆盖叶子：覆盖里两个键都在
    stored = json.loads(db.rows[0].key_value)
    assert set(stored) == {"max_structure_files", "sakura_memory"}


@pytest.mark.asyncio
async def test_save_prunes_only_default_leaves_and_preserves_valid_overrides():
    """裁剪默认叶子时保留非默认、未知及 patch 未覆盖的有效值。"""
    db = _FakeSession()
    await section_config_service.save_section(
        db,
        "label.recommendation",
        {
            "enabled": False,
            "confidence_threshold": 0.9,
            "future_option": {"value": "keep"},
        },
    )

    await section_config_service.save_section(
        db,
        "label.recommendation",
        {"enabled": True},
        mode="patch",
    )

    stored = json.loads(db.rows[0].key_value)
    assert stored == {
        "confidence_threshold": 0.9,
        "future_option": {"value": "keep"},
    }
    loaded = await section_config_service.load_section(db, "label.recommendation")
    assert loaded["enabled"] is True
    assert loaded["confidence_threshold"] == 0.9
    assert loaded["future_option"] == {"value": "keep"}


@pytest.mark.asyncio
async def test_save_equivalent_to_defaults_removes_override():
    db = _FakeSession()
    await section_config_service.save_section(
        db, "label.recommendation", {"enabled": False}
    )
    assert db.rows

    # 保存与内置默认等价的值：移除 DB 覆盖（保持「无覆盖=用默认」语义）
    result = await section_config_service.save_section(
        db,
        "label.recommendation",
        {
            "enabled": True,
            "confidence_threshold": 0.7,
            "auto_create": True,
        },
    )

    assert result["changed"] is True  # 发生了实际变更（false → true 并移除键）
    assert db.rows == []
    assert (
        config_sections.get_section_config("label.recommendation")
        == LABEL_SECTION_DEFAULTS["recommendation"]
    )


@pytest.mark.asyncio
async def test_save_unchanged_override_skips_write(monkeypatch):
    db = _FakeSession()
    data = {"enabled": False}
    await section_config_service.save_section(db, "label.recommendation", data)
    db.committed = False

    result = await section_config_service.save_section(db, "label.recommendation", data)

    assert result["changed"] is False
    assert result["changes"] == {}
    assert db.committed is False
