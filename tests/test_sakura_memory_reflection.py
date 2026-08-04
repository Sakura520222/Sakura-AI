"""Sakura 反思 Prompt 构造测试。

聚焦：喂给反思 AI 的审查评论必须保持完整，不得出现字符级截断
（如 external_ci_failures 被砍成 external_ci_），否则反思 AI 会
把"输入传输截断"误判成"原始审查输出缺陷"。
"""

from types import SimpleNamespace

import pytest

from backend.services import sakura_memory_service as service_module


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, stmt):
        return _FakeResult(self._rows)


class _FakeSessionCM:
    """async_session() 的替身：返回带预设评论行的 session。"""

    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return _FakeSession(self._rows)

    async def __aexit__(self, *exc):
        return False


def _make_comment(content, severity="suggestion", file_path="a.py", line_number=42):
    return SimpleNamespace(
        severity=severity,
        file_path=file_path,
        line_number=line_number,
        content=content,
    )


async def _noop(*_args, **_kwargs):
    """异步空操作，屏蔽 reflect 的副作用依赖（写状态、后处理）。"""
    return


@pytest.mark.asyncio
async def test_fetch_comments_from_db_keeps_full_content_without_truncation(monkeypatch):
    """评论正文不得被字符级截断。

    构造一条长度超过 150 字符的评论，关键标识符 external_ci_failures
    落在 150 字符之后。旧实现用 content[:150] 会把它砍成 external_ci_，
    让反思 AI 误以为原始审查评论被截断。
    """
    long_body = "前序问题：" + ("x" * 140) + " (1) external_ci_failures untrusted wrap"
    assert len(long_body) > 150
    assert "external_ci_failures" in long_body[150:]

    fake_comment = _make_comment(long_body)

    monkeypatch.setattr(
        service_module,
        "async_session",
        lambda: _FakeSessionCM([fake_comment]),
    )

    service = service_module.SakuraMemoryService.__new__(
        service_module.SakuraMemoryService
    )

    result = await service._fetch_comments_from_db(review_id=1)

    assert "external_ci_failures" in result, (
        "评论正文被截断：external_ci_failures 丢失，反思 AI 会看到残缺输入"
    )


@pytest.mark.asyncio
async def test_fetch_comments_from_db_respects_configured_comment_limit(monkeypatch):
    """评论条数上限必须从配置读取，不得硬编码。

    DB 返回 5 条评论，配置 max_comments=3，则反思只应收到 3 条。
    旧实现硬编码 comments[:20] 会忽略配置。
    """
    rows = [_make_comment(f"comment #{i}") for i in range(5)]

    monkeypatch.setattr(
        service_module,
        "async_session",
        lambda: _FakeSessionCM(rows),
    )

    service = service_module.SakuraMemoryService.__new__(
        service_module.SakuraMemoryService
    )
    monkeypatch.setattr(
        service,
        "_get_config",
        lambda: {"reflection": {"max_comments": 3}},
    )

    result = await service._fetch_comments_from_db(review_id=1)

    assert len(result.strip().splitlines()) == 3, (
        "评论条数未遵守配置的 max_comments 上限"
    )


def test_format_review_result_comments_keeps_full_content_without_truncation():
    """fallback 路径（review_result dict）的评论正文也不得截断。

    无 review_id 时反思从 review_result["comments"] 取评论，旧实现用
    content[:100] 会砍断长评论。本测试锁定该路径也完整保留正文。
    """
    long_body = "x" * 110 + " external_ci_failures"
    assert len(long_body) > 100  # 超过旧 [:100] 截断点
    comments = [{"severity": "suggestion", "content": long_body}]

    result = service_module.SakuraMemoryService._format_review_result_comments(
        comments, max_count=30
    )

    assert "external_ci_failures" in result, "fallback 路径评论正文被截断"


def test_format_review_result_comments_respects_max_count():
    """fallback 路径同样按 max_count 限制条数。"""
    comments = [{"severity": "suggestion", "content": f"c{i}"} for i in range(5)]

    result = service_module.SakuraMemoryService._format_review_result_comments(
        comments, max_count=3
    )

    assert len(result.strip().splitlines()) == 3


def test_format_changed_files_respects_max_count():
    """变更文件列表按 max_count 限制条数，不得硬编码。"""
    files = [
        SimpleNamespace(path=f"a{i}.py", status="modified", additions=1, deletions=1)
        for i in range(5)
    ]

    result = service_module.SakuraMemoryService._format_changed_files(
        files, max_count=3
    )

    assert len(result.strip().splitlines()) == 3


def test_format_new_commits_respects_max_count():
    """新增提交列表按 max_count 限制条数，不得硬编码。"""
    commits = [{"sha": f"sha{i}", "title": f"t{i}"} for i in range(5)]

    result = service_module.SakuraMemoryService._format_new_commits(
        commits, max_count=3
    )

    assert len(result.strip().splitlines()) == 3


@pytest.mark.asyncio
async def test_reflect_passes_full_pr_description_without_truncation(monkeypatch):
    """PR 描述不得被字符级截断。

    旧实现 pr.body[:500] 会砍断长描述。构造 body 超过 500 字符、关键
    marker 落在 500 之后，验证完整描述进入反思 prompt。
    """
    long_body = "B" * 600 + " unique_marker_xyz"
    assert len(long_body) > 500
    assert "unique_marker_xyz" in long_body[500:]

    pr = SimpleNamespace(
        number=42, body=long_body, head=SimpleNamespace(sha="abc1234")
    )
    analysis = SimpleNamespace(
        code_files=[
            SimpleNamespace(
                path="a.py", status="modified", additions=1, deletions=1
            )
        ],
        strategy="default",
        is_incremental=False,
        new_commits=[],
    )

    service = service_module.SakuraMemoryService.__new__(
        service_module.SakuraMemoryService
    )

    async def fake_state(repo_full_name):
        return SimpleNamespace(is_initialized=True, reflection_count=0)

    monkeypatch.setattr(service, "_get_or_create_state", fake_state)

    class _FakeWriteService:
        async def get_sakura_branch(self, repo):
            return "sakura-ref"

        async def read_file(self, repo, path, ref=None):
            return "memory content"

        async def commit_files(self, repo, files, commit_msg):
            return None

    service.write_service = _FakeWriteService()

    async def fake_comments(review_id):
        return "无评论"

    monkeypatch.setattr(service, "_fetch_comments_from_db", fake_comments)

    captured = {}

    async def fake_call_llm(prompt, model=None):
        captured["prompt"] = prompt
        return "# reflection"

    monkeypatch.setattr(service, "_get_model", lambda cfg: "fake-model")
    monkeypatch.setattr(service, "_call_llm", fake_call_llm)
    monkeypatch.setattr(service, "_update_state", _noop)
    monkeypatch.setattr(service, "_post_reflection_checks", _noop)

    await service.reflect(
        repo=object(),
        repo_full_name="owner/repo",
        pr=pr,
        review_result={"overall_score": 8, "decision": "comment", "summary": "ok"},
        analysis=analysis,
        pr_info={"action": ""},
        history_summary=None,
        review_id=1,
    )

    assert "unique_marker_xyz" in captured["prompt"], (
        "PR 描述被字符级截断，反思 AI 收到残缺输入"
    )
