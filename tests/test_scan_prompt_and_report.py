"""扫描 prompt 构建与报告模板测试"""

from types import SimpleNamespace

from backend.services.scan_prompt_builder import (
    build_sakura_knowledge_section,
    build_scan_system_prompt,
    build_scan_user_message,
)
from backend.services.scan_report_service import ScanReportService
from backend.workers.scan_worker import _parse_trigger_user_id


def _context() -> dict:
    return {
        "repo_name": "owner/repo",
        "commit_sha": "abc1234",
        "total_files": 12,
        "total_size": "1.2 KB",
        "project_structure": "项目根目录: /",
        "file_tree": "  app.py",
    }


def test_system_prompt_injects_envelope_and_language():
    prompt = build_scan_system_prompt("owner/repo", 12, language="en")

    assert "<SAKURA_SCAN>" in prompt
    assert "owner/repo (12 collected code files)" in prompt
    assert "English" in prompt
    assert "untrusted evidence" in prompt
    # focus 来自 strategy.scan 默认（含五维说明）
    assert "security" in prompt.lower()


def test_system_prompt_respects_explicit_focus():
    prompt = build_scan_system_prompt(
        "owner/repo", 3, language="zh-CN", focus_prompt="Custom audit focus."
    )

    assert "Custom audit focus." in prompt
    assert "Simplified Chinese" in prompt


def test_user_message_wraps_evidence_boundary():
    knowledge = build_sakura_knowledge_section("overview md", "memory md")
    message = build_scan_user_message(_context(), project_knowledge=knowledge)

    begin = message.index("=== BEGIN UNTRUSTED REPOSITORY EVIDENCE ===")
    end = message.index("=== END UNTRUSTED REPOSITORY EVIDENCE ===")
    assert begin < end
    # 仓库证据与 .sakura 知识都在边界内，指令在边界外
    assert message.index("owner/repo") > begin
    assert message.index("Project memory") > begin
    assert message.index("SAKURA_SCAN envelope") > end


def test_sakura_knowledge_section_empty_for_blank_input():
    assert build_sakura_knowledge_section("", "") == ""


def _finding(severity: str, category: str, path: str, title: str):
    return SimpleNamespace(
        severity=severity,
        category=category,
        file_path=path,
        line_start=3,
        line_end=3,
        title=title,
        description="desc",
        suggestion=None,
        confidence=70,
    )


def _scan(**overrides):
    base = {
        "repo_name": "owner/repo",
        "created_at": None,
        "commit_sha": "abcdef1234567890",
        "trigger_type": "manual",
        "overall_health_score": 70,
        "code_file_count": 10,
        "indexed_chunks": 31,
        "scan_rounds": 6,
        "summary": "发现一个关键问题。",
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "started_at": None,
        "completed_at": None,
        "critical_count": 1,
        "major_count": 0,
        "minor_count": 0,
        "suggestion_count": 1,
        "total_findings": 2,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_issue_body_dense_layout_and_folding():
    scan = _scan()
    findings = [
        _finding("critical", "security", "app/db.py", "SQL 注入"),
        _finding("suggestion", "maintainability", None, "类型注解"),
    ]
    body = ScanReportService.__new__(ScanReportService).generate_issue_body(
        scan, findings, language="en"
    )

    # AI summary 进入报告
    assert "发现一个关键问题。" in body
    # critical 默认展开、suggestion 折叠
    assert "<details open>" in body
    assert "<details>\n<summary>" in body
    # 维度矩阵与概览高密度字段
    assert "security" in body
    assert "| Files scanned | 10 |" in body
    assert "| AI rounds | 6 |" in body
    # 首次扫描无历史对比
    assert "First scan" in body
    # 落款品牌链接（与既有签名测试对齐）
    assert "*This report was generated automatically by" in body


def test_issue_body_trend_with_previous_scan():
    scan = _scan()
    previous = SimpleNamespace(
        completed_at=None,
        overall_health_score=50,
        report_issue_number=42,
    )
    previous_findings = [
        _finding("critical", "security", "app/db.py", "SQL 注入"),
        _finding("major", "performance", "web/route.py", "慢查询"),
    ]
    findings = [
        _finding("critical", "security", "app/db.py", "SQL 注入"),
        _finding("minor", "reliability", "tasks/queue.py", "重试缺失"),
    ]
    body = ScanReportService.__new__(ScanReportService).generate_issue_body(
        scan,
        findings,
        previous_scan=previous,
        previous_findings=previous_findings,
        language="en",
    )

    assert "| +20 (50 → 70) |" in body  # 评分变化
    assert "| New | 1 |" in body
    assert "| Resolved | 1 |" in body
    assert "| Persisting | 1 |" in body


def test_issue_body_defaults_to_chinese():
    scan = _scan(
        summary="",
        critical_count=0,
        suggestion_count=0,
        total_findings=0,
    )
    body = ScanReportService.__new__(ScanReportService).generate_issue_body(
        scan, []
    )

    assert "| 扫描文件数 | 10 |" in body
    assert "首次扫描，无历史对比" in body
    assert "未发现问题，代码质量良好" in body


def test_parse_trigger_user_id():
    assert _parse_trigger_user_id("webui:42") == 42
    assert _parse_trigger_user_id("api:7") == 7
    assert _parse_trigger_user_id("scheduled") is None
    assert _parse_trigger_user_id("webui:alice") is None
    assert _parse_trigger_user_id(None) is None
