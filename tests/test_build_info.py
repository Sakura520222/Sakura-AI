from __future__ import annotations

import pytest

from backend.core.build_info import get_build_info
from backend.main import health


def test_build_info_defaults_to_safe_source_identity(monkeypatch):
    for key in (
        "SAKURA_BUILD_CHANNEL",
        "SAKURA_BUILD_REVISION",
        "SAKURA_BUILD_CREATED",
    ):
        monkeypatch.delenv(key, raising=False)
    assert get_build_info() == {
        "channel": "source",
        "revision": None,
        "created_at": None,
    }


def test_build_info_normalizes_image_identity(monkeypatch):
    monkeypatch.setenv("SAKURA_BUILD_CHANNEL", "development")
    monkeypatch.setenv("SAKURA_BUILD_REVISION", "a" * 40)
    monkeypatch.setenv("SAKURA_BUILD_CREATED", "2026-08-13T04:00:00+08:00")
    assert get_build_info() == {
        "channel": "development",
        "revision": "a" * 40,
        "created_at": "2026-08-12T20:00:00Z",
    }


def test_build_info_rejects_untrusted_values(monkeypatch):
    monkeypatch.setenv("SAKURA_BUILD_CHANNEL", "edge")
    monkeypatch.setenv("SAKURA_BUILD_REVISION", "short")
    monkeypatch.setenv("SAKURA_BUILD_CREATED", "not-a-timestamp")
    assert get_build_info() == {
        "channel": "source",
        "revision": None,
        "created_at": None,
    }


@pytest.mark.asyncio
async def test_health_preserves_top_level_version_and_exposes_build(monkeypatch):
    monkeypatch.setenv("SAKURA_BUILD_CHANNEL", "stable")
    monkeypatch.setenv("SAKURA_BUILD_REVISION", "c" * 40)
    monkeypatch.setenv("SAKURA_BUILD_CREATED", "2026-08-13T04:00:00Z")
    payload = await health()
    assert payload["version"]
    assert payload["build"] == {
        "channel": "stable",
        "revision": "c" * 40,
        "created_at": "2026-08-13T04:00:00Z",
    }
