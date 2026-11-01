"""Shared Agent Skill metadata helpers used by the API and tests."""

import json
from typing import Any


def _metadata_list(raw_value: str | None) -> list[str]:
    """Parse a list-like Skill metadata field for compact UI tags."""
    if not raw_value:
        return []
    try:
        value = json.loads(raw_value)
    except TypeError, ValueError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _filter_skills(
    skills: list[Any],
    *,
    query: str = "",
    status: str = "",
    source: str = "",
) -> list[Any]:
    """Apply the Skills ledger filters without changing persistence order."""
    normalized_query = query.strip().casefold()
    normalized_status = status.strip().casefold()
    normalized_source = source.strip().casefold()

    filtered: list[Any] = []
    for skill in skills:
        if normalized_status == "enabled" and not skill.enabled:
            continue
        if normalized_status == "disabled" and skill.enabled:
            continue
        if normalized_source and str(skill.source_type).casefold() != normalized_source:
            continue
        if normalized_query:
            searchable = "\n".join(
                str(value or "")
                for value in (
                    skill.name,
                    skill.slug,
                    skill.description,
                    skill.when_to_use,
                    skill.version,
                    skill.source_type,
                    skill.source_url,
                    skill.source_ref,
                    skill.allowed_tools,
                    skill.arguments,
                    skill.requires,
                )
            ).casefold()
            if normalized_query not in searchable:
                continue
        filtered.append(skill)
    return filtered
