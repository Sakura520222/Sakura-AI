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
    """title 或 summary 缺失时拒绝 output（GitHub API 要求两者并存）。"""
    out = GitHubAppClient._build_check_run_output("t", None, None)
    assert out is None


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


# ---------------- cleanup_stale_check_runs ----------------


def test_cleanup_stale_keeps_latest_and_finalizes_others(app_with_repo):
    """多个 active run：保留 id 最大者，其余 update 成 completed+cancelled(superseded)。"""
    app, _client, repo = app_with_repo
    cr_stale1 = MagicMock()
    cr_stale1.name = "Sakura AI Review"
    cr_stale1.status = "in_progress"
    cr_stale1.id = 100
    cr_stale2 = MagicMock()
    cr_stale2.name = "Sakura AI Review"
    cr_stale2.status = "queued"
    cr_stale2.id = 200
    cr_latest = MagicMock()
    cr_latest.name = "Sakura AI Review"
    cr_latest.status = "in_progress"
    cr_latest.id = 300
    commit = MagicMock()
    commit.get_check_runs.return_value = [cr_stale1, cr_stale2, cr_latest]
    repo.get_commit.return_value = commit

    latest_id = app.cleanup_stale_check_runs("o", "r", "sha", "Sakura AI Review")

    assert latest_id == 300
    cr_stale1.edit.assert_called_once_with(status="completed", conclusion="cancelled")
    cr_stale2.edit.assert_called_once_with(status="completed", conclusion="cancelled")
    cr_latest.edit.assert_not_called()  # 最新的保留不动


def test_cleanup_stale_with_external_id_cancels_webhook_placeholder(app_with_repo):
    """external_id 提供时：收敛 webhook 预创建占位（review_job_id=webhook-incremental），
    让 worker 接管时 placeholder 不悬挂；复用匹配的 latest。"""
    app, _client, repo = app_with_repo
    cr_match = MagicMock()
    cr_match.name = "Sakura AI Review"
    cr_match.status = "in_progress"
    cr_match.id = 300
    cr_match.external_id = "sakura-ai:v1:99:review"
    cr_placeholder = MagicMock()
    cr_placeholder.name = "Sakura AI Review"
    cr_placeholder.status = "queued"
    cr_placeholder.id = 100
    cr_placeholder.external_id = "sakura-ai:v1:webhook-incremental:review"
    commit = MagicMock()
    commit.get_check_runs.return_value = [cr_placeholder, cr_match]
    repo.get_commit.return_value = commit

    latest_id = app.cleanup_stale_check_runs(
        "o",
        "r",
        "sha",
        "Sakura AI Review",
        external_id="sakura-ai:v1:99:review",
    )

    assert latest_id == 300
    cr_placeholder.edit.assert_called_once_with(
        status="completed", conclusion="cancelled"
    )
    cr_match.edit.assert_not_called()  # 匹配的 latest 保留不动


def test_cleanup_stale_with_external_id_keeps_other_jobs(app_with_repo):
    """external_id 提供时：不触碰其他合法并行执行的 Check（不同 review_job_id，
    非 webhook 占位），避免误取消正常审查。"""
    app, _client, repo = app_with_repo
    cr_match = MagicMock()
    cr_match.name = "Sakura AI Review"
    cr_match.status = "in_progress"
    cr_match.id = 300
    cr_match.external_id = "sakura-ai:v1:99:review"
    cr_other_job = MagicMock()
    cr_other_job.name = "Sakura AI Review"
    cr_other_job.status = "in_progress"
    cr_other_job.id = 200
    cr_other_job.external_id = "sakura-ai:v1:55:review"  # 另一合法执行
    commit = MagicMock()
    commit.get_check_runs.return_value = [cr_other_job, cr_match]
    repo.get_commit.return_value = commit

    latest_id = app.cleanup_stale_check_runs(
        "o",
        "r",
        "sha",
        "Sakura AI Review",
        external_id="sakura-ai:v1:99:review",
    )

    assert latest_id == 300
    cr_other_job.edit.assert_not_called()  # 其他合法执行不动
    cr_match.edit.assert_not_called()


def test_cleanup_stale_skips_completed_runs(app_with_repo):
    """已 completed 的 run 视为历史，不清理；无 active 时返回 None。"""
    app, _client, repo = app_with_repo
    cr_done = MagicMock()
    cr_done.name = "Sakura AI Review"
    cr_done.status = "completed"
    cr_done.id = 999
    commit = MagicMock()
    commit.get_check_runs.return_value = [cr_done]
    repo.get_commit.return_value = commit

    assert app.cleanup_stale_check_runs("o", "r", "sha", "Sakura AI Review") is None
    cr_done.edit.assert_not_called()


def test_cleanup_stale_single_active_returned(app_with_repo):
    """仅一个 active run 时直接返回其 id，不调 edit。"""
    app, _client, repo = app_with_repo
    cr = MagicMock()
    cr.name = "Sakura AI Review"
    cr.status = "in_progress"
    cr.id = 555
    commit = MagicMock()
    commit.get_check_runs.return_value = [cr]
    repo.get_commit.return_value = commit

    assert app.cleanup_stale_check_runs("o", "r", "sha", "Sakura AI Review") == 555
    cr.edit.assert_not_called()
