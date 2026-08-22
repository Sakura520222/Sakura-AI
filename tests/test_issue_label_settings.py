"""Issue 标签推荐开关与自动创建策略回归测试。"""

from __future__ import annotations

import pytest

import backend.services.issue_service as issue_service_module
from backend.services.issue_service import IssueService


class _RecommendationConfig:
    def __init__(self, settings: dict):
        self._settings = settings

    def get_recommendation_settings(self) -> dict:
        return self._settings


class _FakeGitHubApp:
    def __init__(self):
        self.created: list[str] = []
        self.applied: list[tuple[str, str, int, list[str]]] = []

    def create_label(
        self,
        _repo_owner: str,
        _repo_name: str,
        label_name: str,
        _color: str,
        _description: str,
    ) -> bool:
        self.created.append(label_name)
        return True

    def add_labels_to_issue(
        self,
        repo_owner: str,
        repo_name: str,
        issue_number: int,
        labels: list[str],
    ) -> bool:
        self.applied.append((repo_owner, repo_name, issue_number, labels))
        return True


class _FakeLabelService:
    DEFAULT_LABELS = {
        "missing": {"color": "0366d6", "description": ""},
    }

    def __init__(self, labels: dict[str, dict]):
        self.labels = labels
        self.fetch_count = 0

    async def get_repo_labels(self, _repo_owner: str, _repo_name: str):
        self.fetch_count += 1
        return self.labels


def _make_issue_service(github_app: _FakeGitHubApp) -> IssueService:
    service = object.__new__(IssueService)
    service.github_app = github_app
    return service


@pytest.mark.asyncio
async def test_disabled_label_recommendation_skips_all_issue_label_operations(
    monkeypatch,
):
    import backend.services.label_service as label_service_module

    github_app = _FakeGitHubApp()
    label_service = _FakeLabelService(
        {"existing": {"color": "000000", "description": ""}}
    )
    monkeypatch.setattr(
        issue_service_module,
        "get_label_config",
        lambda: _RecommendationConfig(
            {"enabled": False, "confidence_threshold": 0.7, "auto_create": True}
        ),
    )
    monkeypatch.setattr(label_service_module, "label_service", label_service)

    result = await _make_issue_service(github_app).apply_suggested_labels(
        "owner",
        "repo",
        42,
        [
            {"name": "existing", "confidence": 0.99},
            {"name": "missing", "confidence": 0.99},
        ],
        db=None,
    )

    assert result == {"applied": [], "suggested": [], "created": [], "failed": []}
    assert label_service.fetch_count == 0
    assert github_app.created == []
    assert github_app.applied == []


@pytest.mark.asyncio
async def test_enabled_label_recommendation_keeps_threshold_and_blocks_auto_create(
    monkeypatch,
):
    import backend.services.label_service as label_service_module

    github_app = _FakeGitHubApp()
    label_service = _FakeLabelService(
        {"existing": {"color": "000000", "description": ""}}
    )
    monkeypatch.setattr(
        issue_service_module,
        "get_label_config",
        lambda: _RecommendationConfig(
            {"enabled": True, "confidence_threshold": 0.8, "auto_create": False}
        ),
    )
    monkeypatch.setattr(label_service_module, "label_service", label_service)

    result = await _make_issue_service(github_app).apply_suggested_labels(
        "owner",
        "repo",
        42,
        [
            {"name": "existing", "confidence": 0.9, "reason": "high"},
            {"name": "existing", "confidence": 0.6, "reason": "low"},
            {"name": "missing", "confidence": 0.99, "reason": "new"},
        ],
        db=None,
    )

    assert result["applied"] == [
        {"name": "existing", "confidence": 0.9, "reason": "high"}
    ]
    assert result["suggested"] == [
        {"name": "existing", "confidence": 0.6, "reason": "low"}
    ]
    assert result["created"] == []
    assert result["failed"] == ["missing"]
    assert github_app.created == []
    assert github_app.applied == [("owner", "repo", 42, ["existing"])]
