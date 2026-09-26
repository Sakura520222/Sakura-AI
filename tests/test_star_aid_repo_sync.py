"""Repository synchronization and fail-safe cleanup tests for Star Aid."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from backend.models.star_aid_models import StarAidRepository
from backend.services import star_aid_github_service as gh
from backend.services import star_aid_service


@pytest.mark.asyncio
async def test_short_page_finishes_without_an_extra_request(monkeypatch):
    response = httpx.Response(
        200,
        json=[{"id": 1, "full_name": "owner/repo"}],
        request=httpx.Request("GET", "https://api.github.com/user/repos"),
    )
    client = AsyncMock()
    client.get.side_effect = [response, AssertionError("unexpected page 2")]
    client.__aenter__.return_value = client
    monkeypatch.setattr(gh.httpx, "AsyncClient", lambda: client)

    listed = await gh.list_user_public_repositories("token")

    assert listed.success and listed.complete
    assert [r["id"] for r in listed.repositories] == [1]
    client.get.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("page_count", [1, 2])
async def test_full_terminal_page_without_next_link_is_complete(monkeypatch, page_count):
    responses = []
    for page in range(page_count):
        headers = (
            {"link": f'<https://api.github.com/user/repos?page={page + 2}>; rel="next"'}
            if page + 1 < page_count else {}
        )
        responses.append(
            httpx.Response(
                200, headers=headers,
                json=[
                    {"id": page * 100 + index, "full_name": f"owner/repo-{page}-{index}"}
                    for index in range(100)
                ],
                request=httpx.Request("GET", "https://api.github.com/user/repos"),
            )
        )
    client = AsyncMock()
    client.get.side_effect = [*responses, AssertionError("unexpected extra page")]
    client.__aenter__.return_value = client
    monkeypatch.setattr(gh.httpx, "AsyncClient", lambda: client)

    listed = await gh.list_user_public_repositories("token")

    assert listed.success and listed.complete
    assert len(listed.repositories) == page_count * 100
    assert client.get.await_count == page_count


@pytest.mark.asyncio
async def test_non_list_payload_is_incomplete_and_preserves_displayed_repos(monkeypatch):
    """A 200 object is not an empty terminal page and must not trigger cleanup."""
    response = httpx.Response(
        200,
        json={"message": "unexpected"},
        request=httpx.Request("GET", "https://api.github.com/user/repos"),
    )
    client = AsyncMock()
    client.get.return_value = response
    client.__aenter__.return_value = client
    monkeypatch.setattr(gh.httpx, "AsyncClient", lambda: client)

    listed = await gh.list_user_public_repositories("token")
    assert not listed.success and not listed.complete
    assert listed.error_code == "invalid_payload"

    existing = StarAidRepository(
        id=1, owner_user_id=123, repo_id=1001,
        full_name="owner/repo-one", is_displayed=True, is_public=True,
    )
    monkeypatch.setattr(
        star_aid_service.gh, "get_effective_access_token",
        AsyncMock(return_value=("token", gh.GitHubCallResult(success=True))),
    )
    monkeypatch.setattr(
        star_aid_service.gh, "list_user_public_repositories",
        AsyncMock(return_value=listed),
    )
    session = AsyncMock()
    result = await star_aid_service.refresh_available_repositories(session, 123)
    assert result["success"] is False
    assert result["message"] == "invalid_payload"
    assert existing.is_displayed and existing.is_public
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_page_failure_does_not_clear_displayed_repos(monkeypatch):
    """第 1 页请求失败时，complete=False，绝不执行 destructive cleanup 清空现有展示仓库。"""
    user_id = 123
    existing_repo = StarAidRepository(
        id=1,
        owner_user_id=user_id,
        repo_id=1001,
        full_name="owner/repo-one",
        is_displayed=True,
        is_public=True,
    )

    monkeypatch.setattr(
        star_aid_service.gh,
        "get_effective_access_token",
        AsyncMock(return_value=("fake_token", gh.GitHubCallResult(success=True))),
    )

    # 第 1 页直接返回失败（例如网络错误或 500）
    failed_result = gh.RepositoryListResult(
        success=False,
        complete=False,
        repositories=[],
        status_code=500,
        error_code="server_error",
    )
    monkeypatch.setattr(
        star_aid_service.gh,
        "list_user_public_repositories",
        AsyncMock(return_value=failed_result),
    )

    # 模拟 session
    fake_session = AsyncMock()
    exec_result = MagicMock()
    exec_result.scalars.return_value.all.return_value = [existing_repo]
    fake_session.execute.return_value = exec_result

    res = await star_aid_service.refresh_available_repositories(fake_session, user_id)

    assert res["success"] is False
    assert res["message"] == "server_error"
    # 现有仓库状态必须保留，未被标记为 is_displayed=False
    assert existing_repo.is_displayed is True
    assert existing_repo.is_public is True


@pytest.mark.asyncio
async def test_bad_credentials_marks_reauth_with_credential_identity(monkeypatch):
    """A revoked but locally unexpired token is marked only if it is still current."""
    user_id = 123
    monkeypatch.setattr(
        star_aid_service.gh,
        "get_effective_access_token",
        AsyncMock(
            return_value=(
                "revoked-token",
                gh.GitHubCallResult(
                    success=True,
                    credential_encrypted_access_token="encrypted-token",
                ),
            )
        ),
    )
    monkeypatch.setattr(
        star_aid_service.gh,
        "list_user_public_repositories",
        AsyncMock(
            return_value=gh.RepositoryListResult(
                success=False,
                complete=False,
                status_code=401,
                error_code="bad_credentials",
            )
        ),
    )
    mark_reauth = AsyncMock(return_value=True)
    monkeypatch.setattr(
        star_aid_service.gh,
        "mark_reauth_required",
        mark_reauth,
    )
    session = AsyncMock()

    result = await star_aid_service.refresh_available_repositories(session, user_id)

    assert result == {"success": False, "synced": 0, "message": "reauth_required"}
    mark_reauth.assert_awaited_once_with(
        session,
        user_id,
        expected_encrypted_access_token="encrypted-token",
    )


@pytest.mark.asyncio
async def test_mid_page_failure_does_not_clear_displayed_repos(monkeypatch):
    """拉取多页中间某一页失败，虽部分同步，但 complete=False，必须跳过 stale cleanup。"""
    user_id = 123
    repo1 = StarAidRepository(
        id=1,
        owner_user_id=user_id,
        repo_id=1001,
        full_name="owner/repo-one",
        is_displayed=True,
        is_public=True,
    )
    repo2 = StarAidRepository(
        id=2,
        owner_user_id=user_id,
        repo_id=1002,
        full_name="owner/repo-two",
        is_displayed=True,
        is_public=True,
    )

    monkeypatch.setattr(
        star_aid_service.gh,
        "get_effective_access_token",
        AsyncMock(return_value=("fake_token", gh.GitHubCallResult(success=True))),
    )

    # 包含第 1 页的 repo1，但第 2 页失败导致 complete=False
    partial_result = gh.RepositoryListResult(
        success=False,
        complete=False,
        repositories=[{"id": 1001, "full_name": "owner/repo-one", "stargazers_count": 5}],
        status_code=502,
        error_code="bad_gateway",
    )
    monkeypatch.setattr(
        star_aid_service.gh,
        "list_user_public_repositories",
        AsyncMock(return_value=partial_result),
    )

    fake_session = AsyncMock()

    def fake_execute(stmt, *args, **kwargs):
        res = MagicMock()
        # 查找 repo1
        res.scalar_one_or_none.return_value = repo1
        # owner 仓库列表
        res.scalars.return_value.all.return_value = [repo1, repo2]
        return res

    fake_session.execute = AsyncMock(side_effect=fake_execute)

    res = await star_aid_service.refresh_available_repositories(fake_session, user_id)

    assert res["success"] is False
    assert res["message"] == "bad_gateway"
    # repo2 虽不在 partial 列表中，但因为 complete=False，绝不被隐藏
    assert repo2.is_displayed is True
    assert repo2.is_public is True


@pytest.mark.asyncio
async def test_complete_success_performs_stale_cleanup(monkeypatch):
    """当且仅当 complete=True and success=True 时，才执行 stale cleanup。"""
    user_id = 123
    repo1 = StarAidRepository(
        id=1,
        owner_user_id=user_id,
        repo_id=1001,
        full_name="owner/repo-one",
        is_displayed=True,
        is_public=True,
    )
    repo_deleted = StarAidRepository(
        id=2,
        owner_user_id=user_id,
        repo_id=1002,
        full_name="owner/repo-deleted",
        is_displayed=True,
        is_public=True,
    )

    monkeypatch.setattr(
        star_aid_service.gh,
        "get_effective_access_token",
        AsyncMock(return_value=("fake_token", gh.GitHubCallResult(success=True))),
    )

    full_result = gh.RepositoryListResult(
        success=True,
        complete=True,
        repositories=[{"id": 1001, "full_name": "owner/repo-one", "stargazers_count": 10}],
        status_code=200,
    )
    monkeypatch.setattr(
        star_aid_service.gh,
        "list_user_public_repositories",
        AsyncMock(return_value=full_result),
    )

    fake_session = AsyncMock()

    def fake_execute(stmt, *args, **kwargs):
        res = MagicMock()
        res.scalar_one_or_none.return_value = repo1
        res.scalars.return_value.all.return_value = [repo1, repo_deleted]
        return res

    fake_session.execute = AsyncMock(side_effect=fake_execute)

    res = await star_aid_service.refresh_available_repositories(fake_session, user_id)

    assert res["success"] is True
    assert res["message"] == "ok"
    # repo1 正常显示
    assert repo1.is_displayed is True
    # repo_deleted 已从远端删除或设为私有，在完整同步成功后正确被清理移出展示池
    assert repo_deleted.is_displayed is False
    assert repo_deleted.is_public is False
