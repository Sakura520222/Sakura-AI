"""Large-strategy context construction tests.

Verifies that ``large`` strategy fills ``context["files"]`` with every changed
file (full patch included) so ``get_file_diff`` works on demand, and that the
legacy ``file_summary`` / ``remaining_files`` fields are no longer produced
(they duplicated the compact file list rendered by the prompt builder).
"""

from types import SimpleNamespace

import pytest

from backend.services.pr_analyzer import PRAnalyzer, PRFileInfo

CODE_FILES = [
    PRFileInfo(
        path="backend/a.py",
        status="modified",
        additions=5,
        deletions=2,
        changes=7,
        patch="@@ -1,2 +1,5 @@\n-old\n+new\n+line",
        is_code_file=True,
    ),
    PRFileInfo(
        path="backend/b.py",
        status="added",
        additions=10,
        deletions=0,
        changes=10,
        patch="@@ -0,0 +1,10 @@\n+new file",
        is_code_file=True,
    ),
    PRFileInfo(
        path="backend/c.py",
        status="deleted",
        additions=0,
        deletions=3,
        changes=3,
        patch=None,
        is_code_file=True,
    ),
]


@pytest.fixture
def analyzer() -> PRAnalyzer:
    instance = PRAnalyzer()
    # Stub GitHub project-structure fetch so the unit test never hits the network.
    instance._get_project_structure_sync = lambda repo, max_files: []
    return instance


@pytest.fixture
def large_analysis() -> SimpleNamespace:
    return SimpleNamespace(
        strategy="large", code_files=CODE_FILES, changed_lines_map={}
    )


@pytest.fixture
def fake_pr() -> SimpleNamespace:
    return SimpleNamespace(base=SimpleNamespace(repo=None))


def test_large_strategy_fills_files_with_full_patch(
    analyzer: PRAnalyzer, large_analysis: SimpleNamespace, fake_pr: SimpleNamespace
):
    """large 策略：全部 code_files 进入 files 并保留完整 patch。"""
    context = analyzer._prepare_review_context_sync(large_analysis, fake_pr)

    files = context["files"]
    assert len(files) == len(CODE_FILES)

    by_path = {f["path"]: f for f in files}

    a = by_path["backend/a.py"]
    assert a["status"] == "modified"
    assert a["additions"] == 5
    assert a["deletions"] == 2
    assert a["changes"] == 7
    assert a["patch"] == CODE_FILES[0].patch

    # Files without a patch (e.g. pure deletions) keep metadata but omit the key.
    c = by_path["backend/c.py"]
    assert c["status"] == "deleted"
    assert "patch" not in c


def test_large_strategy_omits_file_summary(
    analyzer: PRAnalyzer, large_analysis: SimpleNamespace, fake_pr: SimpleNamespace
):
    """large 策略不再产生 file_summary（已废弃，compact 清单已覆盖文件元信息）。"""
    context = analyzer._prepare_review_context_sync(large_analysis, fake_pr)

    assert "file_summary" not in context


def test_large_strategy_omits_remaining_files(
    analyzer: PRAnalyzer, large_analysis: SimpleNamespace, fake_pr: SimpleNamespace
):
    """large 策略 files 含全部变更文件、无截断，不再产生 remaining_files。"""
    context = analyzer._prepare_review_context_sync(large_analysis, fake_pr)

    assert "remaining_files" not in context
