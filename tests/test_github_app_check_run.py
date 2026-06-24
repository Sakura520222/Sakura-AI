"""GitHubAppClient Check Run 方法的单元测试。"""

from unittest.mock import MagicMock, patch

import pytest

from backend.core.github_app import GitHubAppClient


@pytest.fixture()
def app_with_repo():
    """返回 (app, mock_client, mock_repo)，mock 掉 get_repo_client。"""
    app = GitHubAppClient()
    mock_repo = MagicMock()
    mock_client = MagicMock()
    mock_client.get_repo.return_value = mock_repo
    with patch.object(app, "get_repo_client", return_value=mock_client):
        yield app, mock_client, mock_repo


# ---------------- _build_check_run_output ----------------


def test_build_check_run_output_all_empty_returns_none():
    assert GitHubAppClient._build_check_run_output(None, None, None) is None


def test_build_check_run_output_full():
    out = GitHubAppClient._build_check_run_output("t", "s", "x")
    assert out == {"title": "t", "summary": "s", "text": "x"}


def test_build_check_run_output_partial():
    out = GitHubAppClient._build_check_run_output("t", None, None)
    assert out == {"title": "t"}


# ---------------- create_check_run ----------------


def test_create_check_run_success(app_with_repo):
    app, _client, repo = app_with_repo
    repo.create_check_run.return_value = MagicMock(id=42)

    result = app.create_check_run(
        "owner", "repo", "Sakura AI Review", "abc123", status="queued"
    )

    assert result == {"id": 42, "status": "queued", "conclusion": None}
    repo.create_check_run.assert_called_once_with(
        name="Sakura AI Review", head_sha="abc123", status="queued"
    )


def test_create_check_run_with_output_and_conclusion(app_with_repo):
    app, _client, repo = app_with_repo
    repo.create_check_run.return_value = MagicMock(id=7)

    app.create_check_run(
        "owner",
        "repo",
        "name",
        "sha",
        status="completed",
        conclusion="success",
        output_title="T",
        output_summary="S",
        output_text="X",
    )

    repo.create_check_run.assert_called_once_with(
        name="name",
        head_sha="sha",
        status="completed",
        conclusion="success",
        output={"title": "T", "summary": "S", "text": "X"},
    )


def test_create_check_run_no_client_returns_none():
    app = GitHubAppClient()
    with patch.object(app, "get_repo_client", return_value=None):
        assert app.create_check_run("o", "r", "n", "s") is None


def test_create_check_run_exception_returns_none(app_with_repo):
    app, _client, repo = app_with_repo
    repo.create_check_run.side_effect = RuntimeError("boom")
    assert app.create_check_run("o", "r", "n", "s") is None


# ---------------- update_check_run ----------------


def test_update_check_run_success(app_with_repo):
    app, _client, repo = app_with_repo
    cr = MagicMock()
    repo.get_check_run.return_value = cr

    ok = app.update_check_run(
        "o",
        "r",
        99,
        status="completed",
        conclusion="neutral",
        output_title="T",
        output_summary="S",
    )

    assert ok is True
    cr.edit.assert_called_once_with(
        status="completed",
        conclusion="neutral",
        output={"title": "T", "summary": "S"},
    )


def test_update_check_run_no_fields_skips_edit(app_with_repo):
    app, _client, repo = app_with_repo
    cr = MagicMock()
    repo.get_check_run.return_value = cr

    # 不传任何字段时不应调用 edit
    app.update_check_run("o", "r", 99)
    cr.edit.assert_not_called()


def test_update_check_run_no_client_returns_false():
    app = GitHubAppClient()
    with patch.object(app, "get_repo_client", return_value=None):
        assert app.update_check_run("o", "r", 1) is False


def test_update_check_run_exception_returns_false(app_with_repo):
    app, _client, repo = app_with_repo
    repo.get_check_run.side_effect = RuntimeError("x")
    assert app.update_check_run("o", "r", 1, status="completed") is False


# ---------------- find_check_run_for_sha ----------------


def test_find_check_run_for_sha_found(app_with_repo):
    app, _client, repo = app_with_repo
    cr_other = MagicMock()
    cr_other.name = "other"
    cr_target = MagicMock()
    cr_target.name = "Sakura AI Review"
    cr_target.id = 555
    repo.get_check_runs_for_ref.return_value = [cr_other, cr_target]

    assert app.find_check_run_for_sha("o", "r", "sha", "Sakura AI Review") == 555
    repo.get_check_runs_for_ref.assert_called_once_with(ref="sha")


def test_find_check_run_for_sha_not_found(app_with_repo):
    app, _client, repo = app_with_repo
    cr = MagicMock()
    cr.name = "other"
    repo.get_check_runs_for_ref.return_value = [cr]

    assert app.find_check_run_for_sha("o", "r", "sha", "Sakura AI Review") is None


def test_find_check_run_for_sha_no_client_returns_none():
    app = GitHubAppClient()
    with patch.object(app, "get_repo_client", return_value=None):
        assert app.find_check_run_for_sha("o", "r", "sha", "n") is None


def test_find_check_run_for_sha_exception_returns_none(app_with_repo):
    app, _client, repo = app_with_repo
    repo.get_check_runs_for_ref.side_effect = RuntimeError("x")
    assert app.find_check_run_for_sha("o", "r", "sha", "n") is None
