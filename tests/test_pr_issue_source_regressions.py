"""Captured GitHub #574/#570 and #618/#612 facts, with controlled boundaries.

Both captured Issues are closed. Controlled counterfactual open-state cases are staged
explicitly. Full patches are retained; tests do not manufacture completeness.
"""

import json
import re
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.models.database import PRIssueLink
from backend.services.issues.candidate_retriever import IssueCandidateRetriever
from backend.services.issues.pr_link_sync import PRRelationSyncService
from backend.services.issues.pr_verifier import PRRelationVerifier
from backend.services.pr_body import strip_sakura_generated_sections
from backend.services.pr_issue_linker import PRIssueLinker
from tests import test_issue_candidate_foundation

# Reuse the complete external corpus boundary doubles, not a fake retriever.
foundation = test_issue_candidate_foundation.foundation


@pytest.fixture
def captured_cases():
    path = Path(__file__).parent / "fixtures" / "pr_issue_relation_cases.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
@pytest.mark.parametrize("pr_number", [574, 618])
@pytest.mark.parametrize("state_scenario", ["controlled_open", "captured_closed"])
async def test_source_grounded_pr_pairs_flow_through_retrieval_verification_and_sync(
    monkeypatch, foundation, captured_cases, pr_number, state_scenario
):
    service, collection, issue_repo, settings = foundation
    case = deepcopy(
        next(c for c in captured_cases["cases"] if c["pr"]["number"] == pr_number)
    )
    assert case["issue"]["state"] == "closed"
    assert case["pr"]["url"].endswith(f"/pull/{pr_number}")
    assert case["issue"]["url"].endswith(f"/issues/{case['issue']['number']}")
    assert (
        captured_cases["provenance"]["patches"]
        == "Full captured GitHub patches; no excerpt truncation"
    )
    staged = {
        **case["issue"],
        "state": "open" if state_scenario == "controlled_open" else "closed",
        "labels": [],
        "state_reason": None,
        "updated_at": "2026-10-02T00:00:00Z",
    }
    issue_repo.rows = [staged]
    settings.update(
        {
            "semantic_issue_max_links": 5,
            "semantic_issue_similarity_threshold": 0.8,
            "pr_issue_related_confidence_threshold": 0.85,
            "pr_issue_closing_confidence_threshold": 0.95,
        }
    )
    from backend.services.issues import pr_verifier

    monkeypatch.setattr(
        pr_verifier,
        "get_dynamic_config",
        AsyncMock(side_effect=lambda key, **_: settings[key]),
    )
    facts = case["pr"]

    class PR:
        head = SimpleNamespace(sha=facts["head_sha"])
        base = SimpleNamespace(sha=facts["base_sha"])
        title = facts["title"]
        body = facts["body"]
        changed_files = facts["changed_files"]

        def get_files(self):
            return [SimpleNamespace(**f) for f in case["files"]]

        def edit(self, *, body):
            self.body = body

    pr = PR()
    payloads = []

    async def call(**kwargs):
        payload = json.loads(kwargs["messages"][1]["content"])
        payloads.append(payload)
        if pr_number == 574:
            # Force semantic recall despite the unrelated domains. The real
            # strict decision is supplied by the controlled AI boundary.
            assert "Host Updater" in payload["issues"][0]["title"]
            assert "三镜像" in payload["issues"][0]["body"]
            assert {f["path"] for f in payload["files"]} == {
                "pyproject.toml",
                "requirements.txt",
            }
            assert all("pyyaml>=6.0.3" in f["patch"] for f in payload["files"])
            assert "Updater 重构" not in payload["pr"]["human_body"]
            assert "Resolves #570" not in payload["pr"]["human_body"]
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content='{"relations":[]}'))
                ]
            )
        assert "GitHub App" in payload["issues"][0]["title"]
        assert (
            "用户不能通过修改 installation id 越权查看其他人的 installation"
            in payload["issues"][0]["body"]
        )
        assert len(payload["files"]) == 21 and all(
            f["complete"] for f in payload["files"]
        )
        assert {f["path"] for f in payload["files"]} == {
            f["filename"] for f in case["files"]
        }
        relations = [
            {
                "number": 612,
                "relation": "closes",
                "confidence": 0.99,
                "reason": "User-scoped authorization center and authenticated user page implement the supplied requirements",
                "evidence": [
                    {
                        "path": "backend/webui/routes/github_app.py",
                        "change": "added",
                        "code_quote": "user: dict = Depends(require_auth),",
                        "issue_quote": "普通用户不再因为 `require_admin` 无法管理自己的 App installation",
                    },
                    {
                        "path": "backend/services/github_user_authorization_service.py",
                        "change": "added",
                        "code_quote": 'f"{_GITHUB_API_BASE}/user/installations/{installation_id}/repositories"',
                        "issue_quote": "用户只能看到自己有权访问的 GitHub App installations",
                    },
                ],
            }
        ]
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({"relations": relations})
                    )
                )
            ]
        )

    client = SimpleNamespace(call_with_retry=AsyncMock(side_effect=call))
    engine = create_engine("sqlite:///:memory:")
    PRIssueLink.__table__.create(engine)
    session = Session(engine)
    session.add(
        PRIssueLink(
            repo_name="Sakura520222/Sakura-AI",
            pr_id=pr_number,
            issue_number=570,
            link_type="semantic",
        )
    )
    session.commit()

    class DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, stmt):
            return session.execute(stmt)

        def add(self, row):
            session.add(row)

        async def delete(self, row):
            session.delete(row)

        async def flush(self):
            session.flush()

        async def commit(self):
            session.commit()

        async def rollback(self):
            session.rollback()

    linker = PRIssueLinker.__new__(PRIssueLinker)
    linker._reference_pattern = re.compile(
        r"(?:Fixes|Closes|Resolves)\s+#(\d+)", re.IGNORECASE
    )
    result = await PRRelationSyncService(
        retriever=IssueCandidateRetriever(service),
        verifier=PRRelationVerifier(client),
        session_factory=DB,
        linker=linker,
    ).synchronize(
        SimpleNamespace(get_pull=lambda _: pr), "Sakura520222", "Sakura-AI", pr_number
    )
    assert result.succeeded
    # The real query and raw-source index receive original source facts.
    assert service.embedding_service.embed_query.call_args.args[0] == facts[
        "title"
    ] + "\n" + strip_sakura_generated_sections(facts["body"])
    assert (
        collection.docs[f"issue_{case['issue']['number']}"]["content"]
        == staged["title"] + "\n" + staged["body"]
    )
    rows = session.scalars(select(PRIssueLink)).all()
    if pr_number == 618 and state_scenario == "controlled_open":
        assert "Closes #612" in pr.body
        assert len(rows) == 1 and rows[0].issue_number == 612
        evidence = json.loads(rows[0].inference_reason)["evidence"]
        assert evidence[0]["path"] == "backend/webui/routes/github_app.py"
    else:
        assert not rows
        assert "Closes #570" not in pr.body and "Resolves #570" not in pr.body
        assert "Closes #612" not in pr.body
    if state_scenario == "captured_closed":
        client.call_with_retry.assert_not_awaited()
    else:
        assert len(payloads) == 1
    session.close()


def test_source_fixture_contains_complete_actual_file_diffs(captured_cases):
    for case in captured_cases["cases"]:
        assert len(case["files"]) == case["pr"]["changed_files"]
        for file in case["files"]:
            patch = file["patch"]
            assert patch
            assert (
                sum(
                    line.startswith("+") and not line.startswith("+++")
                    for line in patch.splitlines()
                )
                == file["additions"]
            )
            assert (
                sum(
                    line.startswith("-") and not line.startswith("---")
                    for line in patch.splitlines()
                )
                == file["deletions"]
            )
