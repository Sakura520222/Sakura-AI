"""Configured active slots render separately from removed lifetime quotas."""

from types import SimpleNamespace

import pytest

from backend.webui.deps import get_templates
from backend.webui.i18n import i18n, make_translation_func
from backend.webui.routes.config import _build_dynamic_groups
from tests.test_agent_repository_config_ui import REMOVED_SETTINGS, Inputs


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['zh-CN', 'en'])
async def test_subagent_slots_are_localized_and_keep_saved_value(language):
    key = 'agent_team_subagent_concurrency'

    class Database:
        async def execute(self, _statement):
            rows = [SimpleNamespace(key_name=key, key_value='3')]
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    i18n.reload()
    groups = await _build_dynamic_groups(Database(), language)
    group = next(item for item in groups if item['id'] == 'agent_team')
    fields = {item['key']: item for item in group['fields']}
    assert key in fields
    assert not set(REMOVED_SETTINGS) & fields.keys()
    for kind, namespace in (('label', 'label'), ('description', 'desc')):
        catalog = f'config.{namespace}.{key}'
        translated = i18n.t(catalog, lang=language)
        assert translated != catalog
        assert fields[key][kind] == translated
    html = get_templates().env.get_template('components/config_dynamic_card.html').render(
        group=group, card_id='section-agent-team', _=make_translation_func(language)
    )
    controls = Inputs()
    controls.feed(html)
    assert controls.inputs[key]['type'] == 'number'
    assert controls.inputs[key]['min'] == '1'
    assert controls.inputs[key]['value'] == '3'
