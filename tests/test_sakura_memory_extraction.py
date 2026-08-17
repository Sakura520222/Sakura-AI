from types import SimpleNamespace

import pytest

from backend.services import sakura_memory_service as service_module
from backend.webui.routes import sakura_memory as route_module


@pytest.mark.asyncio
async def test_trigger_extract_uses_public_service_without_private_state_lookup(
    monkeypatch,
):
    class Service:
        def __init__(self):
            self.calls = []

        async def _get_or_create_state(self, repo_full_name):
            raise AssertionError("route must not call private state lookup")

        async def extract_and_save_knowledge(
            self, repo, repo_full_name, reflection_count=None
        ):
            self.calls.append((repo, repo_full_name, reflection_count))
            return True

    service = Service()
    repo = object()
    db = object()
    admin_actions = []

    monkeypatch.setattr(route_module, "_get_repo", lambda repo_full_name: repo)
    monkeypatch.setattr(
        "backend.services.sakura_memory_service.get_sakura_memory_service",
        lambda: service,
    )

    async def fake_log_admin_action(db, user_id, category, action, target):
        admin_actions.append((db, user_id, category, action, target))

    def fake_toast_redirect(
        url,
        message="toast.success",
        toast_type="success",
        status_code=302,
        lang="",
        **kwargs,
    ):
        return {
            "url": url,
            "message": message,
            "toast_type": toast_type,
        }

    monkeypatch.setattr(route_module, "log_admin_action", fake_log_admin_action)
    monkeypatch.setattr(route_module, "toast_redirect", fake_toast_redirect)
    monkeypatch.setattr(route_module, "detect_language", lambda: "zh-CN")

    response = await route_module.trigger_extract(
        "owner/repo",
        request=object(),
        db=db,
        user={"user_id": 7},
        csrf_token="csrf",
    )

    assert service.calls == [(repo, "owner/repo", None)]
    assert admin_actions == [(db, 7, "sakura_trigger", "extract", "owner/repo")]
    assert response["message"] == "toast.sakura_extract_triggered"


@pytest.mark.asyncio
async def test_extract_and_save_knowledge_preserves_explicit_zero_reflection_count(
    monkeypatch,
):
    service = service_module.SakuraMemoryService.__new__(
        service_module.SakuraMemoryService
    )
    repo = object()
    committed = []
    updates = []

    async def fake_get_sakura_branch(repo_arg):
        assert repo_arg is repo
        return "sakura-ref"

    async def fake_commit_files(repo_arg, files, commit_msg):
        committed.append((repo_arg, files, commit_msg))

    async def fail_state_lookup(repo_full_name):
        raise AssertionError("explicit zero must not trigger state lookup")

    async def fake_update_state(repo_full_name, **kwargs):
        updates.append((repo_full_name, kwargs))

    class Extractor:
        async def extract_knowledge(self, **kwargs):
            assert kwargs["repo"] is repo
            assert kwargs["repo_full_name"] == "owner/repo"
            assert kwargs["sakura_ref"] == "sakura-ref"
            assert kwargs["model"] == "test-model"
            assert kwargs["reflection_count"] == 0
            return {"rules/test.md": "content"}

    service.write_service = SimpleNamespace(
        get_sakura_branch=fake_get_sakura_branch,
        commit_files=fake_commit_files,
    )
    service._get_config = lambda: {"consolidation": {"model": "test-model"}}
    service._get_or_create_state = fail_state_lookup
    service._update_state = fake_update_state
    monkeypatch.setattr(
        "backend.services.sakura_knowledge_extractor.SakuraKnowledgeExtractor",
        Extractor,
    )

    success = await service.extract_and_save_knowledge(
        repo, "owner/repo", reflection_count=0
    )

    assert success is True
    assert updates == [
        (
            "owner/repo",
            {"last_extraction_count": 0, "knowledge_extracted": True},
        )
    ]
    assert committed == [
        (
            repo,
            {".sakura/rules/test.md": "content"},
            "chore(sakura): extract structured knowledge from reflections",
        )
    ]


@pytest.mark.asyncio
async def test_post_reflection_checks_reuses_loaded_state_for_extraction(monkeypatch):
    service = service_module.SakuraMemoryService.__new__(
        service_module.SakuraMemoryService
    )
    state = SimpleNamespace(
        last_consolidation_count=99,
        consolidation_interval=10,
        last_extraction_count=3,
    )
    config = {
        "consolidation": {"interval": 10},
        "knowledge_extraction": {"min_reflections": 2},
    }
    repo = object()
    extracted = []

    async def fake_consolidate(repo_arg, repo_full_name, new_count):
        raise AssertionError("consolidation should not run")

    async def fail_state_lookup(repo_full_name):
        raise AssertionError("periodic extraction should reuse loaded state")

    async def fake_extract(repo_arg, repo_full_name, reflection_count=None):
        extracted.append((repo_arg, repo_full_name, reflection_count))
        return True

    service.consolidate = fake_consolidate
    service._get_or_create_state = fail_state_lookup
    service._get_config = lambda: config
    service.extract_and_save_knowledge = fake_extract

    await service._post_reflection_checks(repo, "owner/repo", 5, state, config)

    assert extracted == [(repo, "owner/repo", 5)]
