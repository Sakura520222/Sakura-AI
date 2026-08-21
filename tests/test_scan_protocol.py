"""SAKURA_SCAN 信封协议解析测试"""

import pytest

from backend.services.scan_protocol import (
    PROTOCOL_VERSION,
    SCAN_PROTOCOL_TEMPLATE,
    SCAN_REPAIR_INSTRUCTION,
    ScanProtocolError,
    TaggedScanParser,
    safe_scan_protocol_failure,
)

VALID_ENVELOPE = """<SAKURA_SCAN>
<VERSION>1</VERSION>
<OVERALL_SCORE>82</OVERALL_SCORE>
<SUMMARY>
整体质量良好，存在一个高危注入风险。
两行总结。
</SUMMARY>
<FINDINGS>
<FINDING>
<SEVERITY>critical</SEVERITY>
<CATEGORY>security</CATEGORY>
<FILE>app/db.py</FILE>
<START_LINE>10</START_LINE>
<END_LINE>12</END_LINE>
<TITLE>
SQL 注入风险
</TITLE>
<DESCRIPTION>
拼接用户输入构造查询。
</DESCRIPTION>
<SUGGESTION>
改用参数化查询。
</SUGGESTION>
<CONFIDENCE>92</CONFIDENCE>
</FINDING>
<FINDING>
<SEVERITY>suggestion</SEVERITY>
<CATEGORY>maintainability</CATEGORY>
<FILE>NONE</FILE>
<START_LINE>NONE</START_LINE>
<END_LINE>NONE</END_LINE>
<TITLE>
补充类型注解
</TITLE>
<DESCRIPTION>
多处函数缺少返回类型注解。
</DESCRIPTION>
<SUGGESTION>NONE</SUGGESTION>
<CONFIDENCE>40</CONFIDENCE>
</FINDING>
</FINDINGS>
</SAKURA_SCAN>"""


def test_parse_valid_envelope():
    result = TaggedScanParser().parse(VALID_ENVELOPE)

    assert result["parse_source"] == "tagged_scan"
    assert result["overall_score"] == 82
    assert "整体质量良好" in result["summary"]
    assert len(result["findings"]) == 2

    first = result["findings"][0]
    assert first["severity"] == "critical"
    assert first["category"] == "security"
    assert first["file_path"] == "app/db.py"
    assert first["line_start"] == 10
    assert first["line_end"] == 12
    assert first["confidence"] == 92
    assert first["suggestion"] == "改用参数化查询。"

    second = result["findings"][1]
    assert second["file_path"] is None
    assert second["line_start"] is None
    assert second["line_end"] is None
    assert second["suggestion"] is None


def test_parse_envelope_inside_code_fence():
    fenced = "```xml\n" + VALID_ENVELOPE + "\n```"
    result = TaggedScanParser().parse(fenced)

    assert result["overall_score"] == 82


def test_parse_tolerates_text_outside_envelope():
    noisy = "Here is my scan result:\n" + VALID_ENVELOPE + "\nDone."
    result = TaggedScanParser().parse(noisy)

    assert len(result["findings"]) == 2


def test_parse_empty_findings_collection():
    envelope = VALID_ENVELOPE.replace(
        VALID_ENVELOPE[VALID_ENVELOPE.index("<FINDINGS>") :],
        "<FINDINGS>\n</FINDINGS>\n</SAKURA_SCAN>",
    )
    result = TaggedScanParser().parse(envelope)

    assert result["findings"] == []


@pytest.mark.parametrize(
    "mutate",
    [
        # 版本错误
        lambda s: s.replace("<VERSION>1</VERSION>", "<VERSION>2</VERSION>"),
        # 非法 severity
        lambda s: s.replace(
            "<SEVERITY>critical</SEVERITY>", "<SEVERITY>blocker</SEVERITY>"
        ),
        # 非法 category
        lambda s: s.replace(
            "<CATEGORY>security</CATEGORY>", "<CATEGORY>style</CATEGORY>"
        ),
        # score 越界
        lambda s: s.replace(
            "<OVERALL_SCORE>82</OVERALL_SCORE>", "<OVERALL_SCORE>0</OVERALL_SCORE>"
        ),
        # score 非数字
        lambda s: s.replace(
            "<OVERALL_SCORE>82</OVERALL_SCORE>", "<OVERALL_SCORE>high</OVERALL_SCORE>"
        ),
        # confidence 越界
        lambda s: s.replace(
            "<CONFIDENCE>92</CONFIDENCE>", "<CONFIDENCE>120</CONFIDENCE>"
        ),
        # 空 SUMMARY
        lambda s: s.replace("整体质量良好，存在一个高危注入风险。\n两行总结。", "   "),
        # 缺字段（删掉第一个 CONFIDENCE）
        lambda s: s.replace("<CONFIDENCE>92</CONFIDENCE>\n", "", 1),
        # 行号非正整数
        lambda s: s.replace(
            "<START_LINE>10</START_LINE>", "<START_LINE>0</START_LINE>"
        ),
    ],
)
def test_parse_rejects_protocol_violations(mutate):
    with pytest.raises(ScanProtocolError):
        TaggedScanParser().parse(mutate(VALID_ENVELOPE))


def test_parse_rejects_duplicate_envelopes():
    with pytest.raises(ScanProtocolError):
        TaggedScanParser().parse(VALID_ENVELOPE + "\n" + VALID_ENVELOPE)


def test_parse_rejects_empty_text():
    with pytest.raises(ScanProtocolError):
        TaggedScanParser().parse("   ")


def test_constants_are_exported():
    assert PROTOCOL_VERSION == "1"
    assert "<SAKURA_SCAN>" in SCAN_PROTOCOL_TEMPLATE
    assert "SAKURA_SCAN" in SCAN_REPAIR_INSTRUCTION
    assert "exactly once" in SCAN_REPAIR_INSTRUCTION


def test_safe_scan_protocol_failure_shape():
    result = safe_scan_protocol_failure(ScanProtocolError("boom"))

    assert result["overall_score"] is None
    assert result["findings"] == []
    assert result["parse_source"] == "scan_protocol_error"
    assert "boom" in result["summary"]
