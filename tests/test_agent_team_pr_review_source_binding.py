"""PR /agent 任务来源绑定测试。

回归背景：``build_pr_review_task_draft`` 曾按 repo 取"最新已完成审查"而不限
定 PR number，导致在 PR #B 上触发 /agent 时错误携带 PR #A 的审查上下文
（任务标题/摘要与触发 PR 不符）。本文件锁定按 repo + pr_number 精确匹配的
行为：找不到本 PR 的已完成审查时直接报错，绝不回退到其他 PR。
"""

import operator
from types import SimpleNamespace

import pytest

from backend.services.agent_team.candidate_service import AgentTeamCandidateService


def _eq_pairs(whereclause):
    """Extract equality pairs (column_key, value) from a flat and_() clause."""

    pairs: dict[str, object] = {}
    if whereclause is None:
        return pairs
    for sub in getattr(whereclause, "clauses", [whereclause]):
        left = getattr(sub, "left", None)
        if left is None or not hasattr(left, "key"):
            continue
        if getattr(sub, "operator", None) is not operator.eq:
            continue
        right = getattr(sub, "right", None)
        pairs[left.key] = getattr(right, "value", right)
    return pairs


def _review(rid, pr_number, status="completed", title="title", score=8):
    return SimpleNamespace(
        id=rid,
        pr_number=pr_number,
        repo_owner="owner",
        repo_name="repo",
        title=title,
        branch="feature/x",
        overall_score=score,
        review_summary="summary",
        status=status,
    )


class _DraftDb:
    """Minimal AsyncSession double: filters in-memory rows by equality pairs."""

    def __init__(self, reviews=(), comments=(), task_statuses=()):
        self.reviews = list(reviews)
        self.comments = list(comments)
        self.task_statuses = list(task_statuses)
        self.review_queries: list[dict[str, object]] = []
        self.added = []

    async def scalar(self, stmt):
        where = stmt.whereclause
        table_name = where.clauses[0].left.table.name
        pairs = _eq_pairs(where)
        if table_name == "pr_reviews":
            self.review_queries.append(pairs)
            matched = [
                row
                for row in self.reviews
                if all(getattr(row, key, None) == value for key, value in pairs.items())
            ]
            # 生产查询按 id 倒序取最新一条
            matched.sort(key=lambda row: row.id, reverse=True)
            return matched[0] if matched else None
        if table_name == "agent_team_tasks":
            excluded_statuses = set()
            for clause in getattr(where, "clauses", ()):
                if getattr(getattr(clause, "left", None), "key", None) != "status":
                    continue
                values = getattr(getattr(clause, "right", None), "value", None)
                if isinstance(values, (list, tuple, set)):
                    excluded_statuses.update(values)
            return sum(status not in excluded_statuses for status in self.task_statuses)
        # 其他查询默认没有匹配项。
        return 0

    async def execute(self, stmt):
        pairs = _eq_pairs(stmt.whereclause)
        matched = [
            row
            for row in self.comments
            if all(getattr(row, key, None) == value for key, value in pairs.items())
        ]
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: matched))

    def add(self, entity):
        self.added.append(entity)

    async def commit(self):
        return None

    async def refresh(self, entity):
        return None


@pytest.fixture
def service(monkeypatch):
    svc = AgentTeamCandidateService()

    async def empty_allowlist():
        return set()

    monkeypatch.setattr(svc, "_load_repo_allowlist", empty_allowlist)
    return svc


@pytest.mark.asyncio
async def test_draft_binds_to_triggering_pr_not_repo_latest_review(service):
    """在 PR #506 触发 /agent 不得借用 repo 内最新的 #515 审查记录。"""

    db = _DraftDb(
        reviews=[
            _review(rid=800, pr_number=506, status="pending"),
            _review(rid=900, pr_number=515, title="ci(deps): bump actions/x"),
        ]
    )

    with pytest.raises(ValueError, match="#506"):
        await service.build_pr_review_task_draft(db, "owner/repo", 506)

    draft = await service.build_pr_review_task_draft(db, "owner/repo", 515)
    assert draft["source_id"] == 900
    assert "bump actions/x" in draft["title"]

    # 审查查询必须携带 pr_number 等值条件（防止回归为 repo 级查询）
    for query in db.review_queries:
        assert query.get("pr_number") is not None


@pytest.mark.asyncio
async def test_draft_rejects_when_other_repo_has_reviews(service):
    """本 PR 无已完成审查时报错信息应指向该 PR，而非泛化的 repo 提示。"""

    db = _DraftDb(reviews=[_review(rid=700, pr_number=515)])

    with pytest.raises(ValueError, match=r"owner/repo#506"):
        await service.build_pr_review_task_draft(db, "owner/repo", 506)


@pytest.mark.asyncio
async def test_draft_picks_latest_completed_review_for_same_pr(service):
    """同一 PR 多条记录时取 id 最大（最新）的已完成审查。"""

    db = _DraftDb(
        reviews=[
            _review(rid=310, pr_number=42, title="old run"),
            _review(rid=320, pr_number=42, title="new run"),
        ]
    )

    draft = await service.build_pr_review_task_draft(db, "owner/repo", 42)

    assert draft["source_id"] == 320
    assert "new run" in draft["title"]


@pytest.mark.asyncio
async def test_waiting_human_task_still_blocks_parallel_agent(service):
    db = _DraftDb(
        reviews=[_review(rid=320, pr_number=42)],
        task_statuses=["waiting_human"],
    )

    with pytest.raises(ValueError, match="已存在进行中的 Agent 修复任务"):
        await service.build_pr_review_task_draft(db, "owner/repo", 42)


@pytest.mark.asyncio
async def test_stale_failed_task_allows_agent_retry(service):
    db = _DraftDb(
        reviews=[_review(rid=320, pr_number=42)],
        task_statuses=["failed"],
    )

    draft = await service.build_pr_review_task_draft(db, "owner/repo", 42)

    assert draft["source_id"] == 320


@pytest.mark.asyncio
async def test_draft_summary_includes_matching_review_comments(service):
    """summary 应拼接本 PR 审查（source_id）下的评论。"""

    db = _DraftDb(
        reviews=[_review(rid=500, pr_number=7, title="fix: crash")],
        comments=[
            SimpleNamespace(
                id=1,
                review_id=500,
                severity="minor",
                file_path="a.py",
                line_number=3,
                content="nit",
            )
        ],
    )

    draft = await service.build_pr_review_task_draft(db, "owner/repo", 7)

    assert draft["source_issue_number"] == 7
    assert "审查意见" in draft["summary"]
    assert "nit" in draft["summary"]


@pytest.mark.asyncio
async def test_create_pr_review_task_persists_original_head_identity(service):
    db = _DraftDb(reviews=[_review(rid=500, pr_number=7)])

    task = await service.create_task_from_pr_review(
        db,
        "owner/repo",
        7,
        "alice",
        base_branch="develop",
        head_sha="a" * 40,
        head_branch="feature/pr",
        head_repo_full_name="alice/repo-fork",
        pr_url="https://github.com/owner/repo/pull/7",
    )

    assert db.added == [task]
    assert task.pr_number == 7
    assert task.pr_head_sha == "a" * 40
    assert task.pr_head_branch == "feature/pr"
    assert task.pr_head_repo_full_name == "alice/repo-fork"
    assert task.pr_url == "https://github.com/owner/repo/pull/7"
    assert task.base_branch == "develop"
