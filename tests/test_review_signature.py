"""审查评论统一落款测试。

所有发布到 GitHub 的 AI 审查评论（整体评论与行内评论）都必须携带统一
落款，且跟随输出语言（中/英）。重试或降级重发时落款不得叠加。
"""

from unittest.mock import MagicMock, patch

import pytest

from backend.core.github_app import GitHubAppClient
from backend.models.database import ReviewDecision
from backend.services.ai_reviewer.constants import (
    SAKURA_AI_REPO_URL,
    append_review_signature,
    review_signature_footer,
)
from backend.services.decision_engine import DecisionEngine

SIGNATURE_ZH = f"*此评论由 [Sakura AI]({SAKURA_AI_REPO_URL}) 自动生成。*"
SIGNATURE_EN = (
    f"*This comment was generated automatically by [Sakura AI]({SAKURA_AI_REPO_URL}).*"
)


# ---------------------------------------------------------------------------
# 落款助手 / Signature helpers
# ---------------------------------------------------------------------------


def test_signature_footer_wording_matches_spec():
    """中英文文案与规格一致 / zh/en wording matches the spec."""
    assert review_signature_footer(False) == SIGNATURE_ZH
    assert review_signature_footer(True) == SIGNATURE_EN


def test_append_review_signature_adds_divider_and_is_idempotent():
    """追加落款带分隔线，重复追加不叠加 / Append once, never twice."""
    signed = append_review_signature("**问题**\n\n描述。", False)

    assert signed == f"**问题**\n\n描述。\n\n---\n\n{SIGNATURE_ZH}"
    # 重试路径：已落款的正文再次经过时保持不变
    assert append_review_signature(signed, False) == signed


def test_append_review_signature_strips_trailing_whitespace():
    """正文末尾多余空白不残留 / Trailing whitespace is stripped."""
    assert append_review_signature("body\n\n", True) == (
        f"body\n\n---\n\n{SIGNATURE_EN}"
    )


# ---------------------------------------------------------------------------
# decision_engine: 整体评论落款 / Overall review body signature
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    with patch("backend.services.decision_engine.get_strategy_config") as mock_config:
        mock_config.return_value.config = {
            "review_policy": {
                "enabled": True,
                "approve_threshold": 8,
                "block_threshold": 4,
                "block_on_critical": True,
                "max_major_issues": 1,
                "trust_ai_decision": True,
                "ai_decision_block_on_critical": True,
            }
        }
        return DecisionEngine()


def _review_result():
    return {"overall_score": 9, "issues": {"critical": [], "major": [], "minor": [], "suggestions": []}}


def test_format_review_body_ends_with_zh_signature(engine):
    engine.policy["review_templates"] = {"approve": "{summary}"}

    body = engine.format_review_body(
        ReviewDecision.APPROVE,
        _review_result(),
        "可以合并",
        output_language="zh-CN",
    )

    assert body.endswith(SIGNATURE_ZH)


def test_format_review_body_ends_with_en_signature(engine):
    engine.policy["review_templates_en"] = {"approve": "{summary}"}

    body = engine.format_review_body(
        ReviewDecision.APPROVE,
        _review_result(),
        "Approved",
        output_language="en",
    )

    assert body.endswith(SIGNATURE_EN)


# ---------------------------------------------------------------------------
# github_app: 行内评论落款 / Inline comment signature (worker path)
# ---------------------------------------------------------------------------


def _make_client_with_pull(monkeypatch):
    client = GitHubAppClient()
    pull = MagicMock()
    repo = MagicMock()
    repo.get_pull.return_value = pull
    repo_client = MagicMock()
    repo_client.get_repo.return_value = repo
    monkeypatch.setattr(client, "get_repo_client", lambda _owner, _repo: repo_client)
    return client, pull


def test_inline_comments_get_signature_in_output_language(monkeypatch):
    client, pull = _make_client_with_pull(monkeypatch)
    inline = [
        {"file_path": "src/a.py", "line_number": 10, "body": "**问题**\n\n描述。"},
        {"file_path": "src/b.py", "line_number": 20, "body": "English finding."},
    ]

    result = client.submit_review_with_inline_comments(
        "owner",
        "repo",
        42,
        "COMMENT",
        "body",
        inline_comments=inline,
        enable_idempotency_check=False,
        output_language="zh-CN",
    )

    assert result["success"] is True
    posted = pull.create_review.call_args.kwargs["comments"]
    assert posted[0]["body"] == f"**问题**\n\n描述。\n\n---\n\n{SIGNATURE_ZH}"
    assert posted[1]["body"] == f"English finding.\n\n---\n\n{SIGNATURE_ZH}"


def test_inline_comments_signature_follows_english(monkeypatch):
    client, pull = _make_client_with_pull(monkeypatch)

    client.submit_review_with_inline_comments(
        "owner",
        "repo",
        42,
        "COMMENT",
        "body",
        inline_comments=[{"file_path": "src/a.py", "line_number": 10, "body": "Finding."}],
        enable_idempotency_check=False,
        output_language="en",
    )

    posted = pull.create_review.call_args.kwargs["comments"]
    assert posted[0]["body"] == f"Finding.\n\n---\n\n{SIGNATURE_EN}"


def test_inline_comments_signature_not_duplicated_on_retry(monkeypatch):
    """已落款的行内评论重发时不叠加 / No double signature on retry."""
    client, pull = _make_client_with_pull(monkeypatch)
    already_signed = f"Finding.\n\n---\n\n{SIGNATURE_ZH}"

    client.submit_review_with_inline_comments(
        "owner",
        "repo",
        42,
        "COMMENT",
        "body",
        inline_comments=[{"file_path": "src/a.py", "line_number": 10, "body": already_signed}],
        enable_idempotency_check=False,
        output_language="zh-CN",
    )

    posted = pull.create_review.call_args.kwargs["comments"]
    assert posted[0]["body"] == already_signed


def test_inline_comments_input_not_mutated_by_signing(monkeypatch):
    """签名不修改调用方传入的原始数据 / Caller's data stays untouched."""
    client, _ = _make_client_with_pull(monkeypatch)
    original = {"file_path": "src/a.py", "line_number": 10, "body": "Finding."}

    client.submit_review_with_inline_comments(
        "owner",
        "repo",
        42,
        "COMMENT",
        "body",
        inline_comments=[original],
        enable_idempotency_check=False,
        output_language="en",
    )

    assert original["body"] == "Finding."


# ---------------------------------------------------------------------------
# comment_service: 旧路径行内评论落款 / Legacy path inline signature
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_batch_inline_comments_signs_each_body():
    from backend.services.comment_service import CommentService

    pr = MagicMock()
    service = CommentService()

    await service.create_batch_inline_comments(
        pr,
        [{"file_path": "src/a.py", "line_number": 5, "body": "描述", "severity": "critical"}],
        output_language="zh-CN",
    )

    posted = pr.create_review.call_args.kwargs["comments"]
    assert posted[0]["body"] == f"🔴 描述\n\n---\n\n{SIGNATURE_ZH}"


@pytest.mark.asyncio
async def test_create_batch_inline_comments_english_signature():
    from backend.services.comment_service import CommentService

    pr = MagicMock()
    service = CommentService()

    await service.create_batch_inline_comments(
        pr,
        [{"file_path": "src/a.py", "line_number": 5, "body": "Finding", "severity": "major"}],
        output_language="en",
    )

    posted = pr.create_review.call_args.kwargs["comments"]
    assert posted[0]["body"] == f"🟡 Finding\n\n---\n\n{SIGNATURE_EN}"


# ---------------------------------------------------------------------------
# 品牌署名可点击跳转仓库 / Brand attributions link to the repo
# ---------------------------------------------------------------------------


def test_review_signature_brands_are_markdown_links():
    """主落款品牌词为链接 / The main signature brand word is a link."""
    assert f"[Sakura AI]({SAKURA_AI_REPO_URL})" in SIGNATURE_ZH
    assert f"[Sakura AI]({SAKURA_AI_REPO_URL})" in SIGNATURE_EN


def test_agent_team_pr_body_brand_links_to_repo():
    """Agent 生成 PR 的品牌署名（标题/落款/元数据头）均可点击跳转仓库。"""
    from backend.services.agent_team.pr_service import AgentTeamPRService

    service = AgentTeamPRService(workspace_service=object())
    link = f"[Sakura Agent]({SAKURA_AI_REPO_URL})"

    body = service.build_pr_body(
        task_title="标题",
        task_summary="摘要",
        fullstack_analysis="分析",
        fullstack_plan="计划",
        review_summary="审查",
        iteration_count=1,
        source_type="issue",
        source_issue_number=5,
    )

    assert f"## {link} 自动生成的 PR" in body
    assert f"*此 PR 由 {link} 自动生成" in body

    header = service._build_metadata_header("issue", 5, 1)
    assert f"**Auto-generated by {link}**" in header


def test_scan_report_footer_brand_links_to_repo():
    """扫描报告落款品牌词为链接 / Scan report footer brand word is a link."""
    from types import SimpleNamespace

    from backend.services.scan_report_service import ScanReportService

    scan = SimpleNamespace(
        created_at=None,
        commit_sha="abcdef1234567890",
        overall_health_score=90,
        repo_name="owner/repo",
        code_file_count=10,
        critical_count=0,
        major_count=0,
        minor_count=0,
        suggestion_count=0,
    )

    body = ScanReportService.__new__(ScanReportService).generate_issue_body(scan, [])

    assert f"*此报告由 [Sakura AI]({SAKURA_AI_REPO_URL}) 自动生成*" in body
