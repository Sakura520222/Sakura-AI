"""AI configuration template internationalisation contract tests."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from backend.webui.deps import get_templates
from backend.webui.i18n import make_translation_func
from tests.stubs import RequestStub

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "backend" / "webui" / "templates" / "config_ai.html"
TRANSLATIONS_DIR = ROOT / "backend" / "webui" / "translations"
LOCALE_PATTERN = re.compile(
    r"const AI_LOCALE = (\{.*?\});\n\nfunction aiText", re.DOTALL
)


def _flatten(data: dict, prefix: str = "") -> dict[str, object]:
    flattened: dict[str, object] = {}
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flattened.update(_flatten(value, full_key))
        else:
            flattened[full_key] = value
    return flattened


def _render(lang: str) -> str:
    template = get_templates().get_template("config_ai.html")
    return template.render(
        request=RequestStub(),
        lang=lang,
        _=make_translation_func(lang),
        current_user={"sub": "admin", "role": "user"},
        user_prefs={"language": lang},
        csrf_token="test-token",
        active_page="config_ai",
        app_version="test",
    )


def _locale(rendered: str) -> dict[str, object]:
    match = LOCALE_PATTERN.search(rendered)
    assert match, "config_ai must inject a locale-specific AI_LOCALE dictionary"
    return json.loads(match.group(1))


def test_config_ai_template_parses_and_all_translation_keys_have_catalog_parity():
    """Every config.* key referenced by the page must exist in both catalogs."""
    source = TEMPLATE_PATH.read_text(encoding="utf-8")
    get_templates().env.parse(source)
    referenced = set(re.findall(r"_\(\s*['\"](config\.[^'\"]+)['\"]", source))

    for filename in ("zh-CN.yaml", "en.yaml"):
        with (TRANSLATIONS_DIR / filename).open(encoding="utf-8") as stream:
            catalog = yaml.safe_load(stream)
        assert isinstance(catalog, dict)
        flattened = _flatten(catalog)
        missing = sorted(key for key in referenced if key not in flattened)
        assert not missing, f"{filename} is missing config_ai keys: {missing}"


@pytest.mark.parametrize(
    ("lang", "title", "saved"),
    [
        ("zh-CN", "AI 配置", "账号已保存"),
        ("en", "AI Config", "Account saved"),
    ],
)
def test_config_ai_renders_bilingual_page_and_locale_dictionary(lang, title, saved):
    rendered = _render(lang)
    locale = _locale(rendered)

    assert title in rendered
    assert "{{ _(" not in rendered
    assert "AI_LOCALE" in rendered
    assert locale["accountSaved"] == saved
    assert locale["modelsFetched"]
    assert "{count}" in locale["modelsFetched"]


def test_config_ai_has_no_raw_chinese_js_state_or_unsafe_html_message_path():
    """Dynamic UI messages must use the injected dictionary and textContent."""
    source = TEMPLATE_PATH.read_text(encoding="utf-8")
    script = source[source.index("<script>") : source.rindex("</script>")]

    assert not re.search(r"[\u4e00-\u9fff]", script)
    for hardcoded in (
        "保存失败",
        "账号已保存",
        "确定删除该账号？",
        "角色绑定已保存",
        "调用策略已保存",
        "获取到 ${this.form.models.length} 个模型并已保存",
        "主模型",
        "辅助/摘要模型",
    ):
        assert hardcoded not in script

    assert "p.textContent = message" in script
    assert "confirm(aiText('accountDeleteConfirm'))" in script
    assert "innerHTML" not in script


def test_config_ai_model_selects_match_account_provider_controls():
    source = TEMPLATE_PATH.read_text(encoding="utf-8")

    assert source.count('x-model="form.default_model"') == 1
    assert source.count('x-model="bindings[role].primary.model"') == 1
    assert source.count('x-model="fb.model"') == 1
    assert "toggleModelPicker" not in source
    assert "modelPickerOpen" not in source
    assert '<option value="" selected>' not in source
    assert "accountModels(bindings[role].primary.account, bindings[role].primary.model)" in source
    assert "accountModels(fb.account, fb.model)" in source
