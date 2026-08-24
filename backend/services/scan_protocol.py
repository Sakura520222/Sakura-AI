"""Strict tagged protocol for machine-readable repository scan results.

与 review_protocol / issue_protocol 同族的 SAKURA_SCAN 信封：行级标签、
字段全量校验、解析失败抛 ScanProtocolError，由公共
``run_protocol_repair_loop`` 执行累积式格式修复。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = "1"
VALID_SEVERITIES = {"critical", "major", "minor", "suggestion"}
VALID_CATEGORIES = {
    "security",
    "performance",
    "reliability",
    "maintainability",
    "architecture",
}

SCAN_PROTOCOL_TEMPLATE = """<SAKURA_SCAN>
<VERSION>1</VERSION>
<OVERALL_SCORE>1-100</OVERALL_SCORE>
<SUMMARY>
Markdown scan summary
</SUMMARY>
<FINDINGS>
<FINDING>
<SEVERITY>critical|major|minor|suggestion</SEVERITY>
<CATEGORY>security|performance|reliability|maintainability|architecture</CATEGORY>
<FILE>repository/path|NONE</FILE>
<START_LINE>positive integer|NONE</START_LINE>
<END_LINE>positive integer|NONE</END_LINE>
<TITLE>
Short title
</TITLE>
<DESCRIPTION>
Evidence-based description
</DESCRIPTION>
<SUGGESTION>
Actionable fix guidance | NONE
</SUGGESTION>
<CONFIDENCE>0-100</CONFIDENCE>
</FINDING>
</FINDINGS>
</SAKURA_SCAN>"""

SCAN_REPAIR_INSTRUCTION = """Your previous response did not match the required SAKURA_SCAN protocol.
Reformat the same scan conclusions only. Do not add, remove, or reconsider findings.
Return exactly one <SAKURA_SCAN> envelope and no text outside it.
Use VERSION 1, an OVERALL_SCORE from 1 to 100, and complete FINDING fields.
Put SUMMARY, TITLE, DESCRIPTION, and SUGGESTION opening and closing tags on separate lines.
Each FINDING must contain exactly these fields, each appearing exactly once and in this exact order: SEVERITY, CATEGORY, FILE, START_LINE, END_LINE, TITLE, DESCRIPTION, SUGGESTION, CONFIDENCE.
Do not repeat any field. FILE, START_LINE, and END_LINE use NONE when a finding is not tied to a location; SUGGESTION uses NONE when no actionable fix applies.
Keep protocol tags and enum values in English. Preserve the requested language only inside natural-language fields."""


class ScanProtocolError(ValueError):
    """Raised when an AI scan response violates the tagged protocol."""


@dataclass(frozen=True)
class TaggedScanFinding:
    severity: str
    category: str
    file_path: str | None
    start_line: int | None
    end_line: int | None
    title: str
    description: str
    suggestion: str | None
    confidence: int


class TaggedScanParser:
    """Parse the line-oriented SAKURA_SCAN envelope."""

    _root_fields = ("VERSION", "OVERALL_SCORE", "SUMMARY", "FINDINGS")
    _finding_fields = (
        "SEVERITY",
        "CATEGORY",
        "FILE",
        "START_LINE",
        "END_LINE",
        "TITLE",
        "DESCRIPTION",
        "SUGGESTION",
        "CONFIDENCE",
    )
    _multiline_fields = {"SUMMARY", "TITLE", "DESCRIPTION", "SUGGESTION"}

    def parse(self, text: str) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise ScanProtocolError("empty scan response")

        lines = self._extract_unique_envelope(text)
        fields, finding_blocks = self._parse_root(lines[1:-1])
        self._require_exact_fields(fields, self._root_fields, "scan")

        version = fields["VERSION"]
        if version != PROTOCOL_VERSION:
            raise ScanProtocolError(f"unsupported protocol version: {version}")

        overall_score = self._parse_score(fields["OVERALL_SCORE"])
        summary = fields["SUMMARY"].strip()
        if not summary:
            raise ScanProtocolError("SUMMARY must not be empty")

        findings = [self._parse_finding(block) for block in finding_blocks]
        return {
            "overall_score": overall_score,
            "summary": summary,
            "findings": findings,
            "parse_source": "tagged_scan",
        }

    @staticmethod
    def _extract_unique_envelope(text: str) -> list[str]:
        lines = text.strip().splitlines()
        if len(lines) >= 3 and lines[0].strip() in {
            "```",
            "```xml",
            "```text",
            "```plaintext",
        }:
            if lines[-1].strip() != "```":
                raise ScanProtocolError("unterminated outer code fence")
            lines = lines[1:-1]
            while lines and not lines[0].strip():
                lines.pop(0)
            while lines and not lines[-1].strip():
                lines.pop()
        if not lines:
            raise ScanProtocolError("empty scan response")

        opening = [
            index for index, line in enumerate(lines) if line.strip() == "<SAKURA_SCAN>"
        ]
        closing = [
            index
            for index, line in enumerate(lines)
            if line.strip() == "</SAKURA_SCAN>"
        ]
        if len(opening) != 1 or len(closing) != 1 or closing[0] <= opening[0]:
            raise ScanProtocolError(
                "response must contain exactly one SAKURA_SCAN envelope"
            )
        return lines[opening[0] : closing[0] + 1]

    def _parse_root(self, lines: list[str]) -> tuple[dict[str, str], list[list[str]]]:
        fields: dict[str, str] = {}
        findings: list[list[str]] = []
        index = 0

        for field in self._root_fields:
            if field == "FINDINGS":
                findings, index = self._consume_collection(
                    lines, index, field, "FINDING"
                )
                fields[field] = ""
                continue
            value, index = self._consume_field(lines, index, field)
            fields[field] = value

        if index != len(lines):
            raise ScanProtocolError("unexpected content after scan fields")
        return fields, findings

    def _parse_finding(self, lines: list[str]) -> dict[str, Any]:
        fields = self._parse_block_fields(lines, self._finding_fields, "finding")

        severity = fields["SEVERITY"]
        if severity not in VALID_SEVERITIES:
            raise ScanProtocolError(f"invalid severity: {severity}")

        category = fields["CATEGORY"]
        if category not in VALID_CATEGORIES:
            raise ScanProtocolError(f"invalid category: {category}")

        file_value = fields["FILE"].strip()
        start_line = self._parse_optional_positive_int(fields["START_LINE"])
        end_line = self._parse_optional_positive_int(fields["END_LINE"])
        if file_value == "NONE":
            if start_line is not None or end_line is not None:
                raise ScanProtocolError("overall findings must use NONE line values")
            file_path = None
        else:
            if not file_value or file_value.upper() == "NONE":
                raise ScanProtocolError("FILE must be a path or exact NONE")
            if start_line is None or end_line is None:
                raise ScanProtocolError("file findings require both line values")
            if start_line > end_line:
                raise ScanProtocolError("START_LINE must not exceed END_LINE")
            file_path = file_value
        title = fields["TITLE"].strip()
        description = fields["DESCRIPTION"].strip()
        if not title:
            raise ScanProtocolError("finding TITLE must not be empty")
        if not description:
            raise ScanProtocolError("finding DESCRIPTION must not be empty")

        return {
            "severity": severity,
            "category": category,
            "file_path": file_path,
            "line_start": start_line,
            "line_end": end_line,
            "title": title,
            "description": description,
            "suggestion": self._parse_optional_text(fields["SUGGESTION"]),
            "confidence": self._parse_confidence(fields["CONFIDENCE"]),
        }

    def _parse_block_fields(
        self, lines: list[str], expected: tuple[str, ...], scope: str
    ) -> dict[str, str]:
        fields: dict[str, str] = {}
        index = 0
        for field in expected:
            value, index = self._consume_field(lines, index, field)
            fields[field] = value
        if index != len(lines):
            raise ScanProtocolError(f"unexpected content in {scope}")
        self._require_exact_fields(fields, expected, scope)
        return fields

    def _consume_collection(
        self, lines: list[str], index: int, field: str, block_name: str
    ) -> tuple[list[list[str]], int]:
        if index >= len(lines) or lines[index].strip() != f"<{field}>":
            raise ScanProtocolError(f"expected <{field}>")
        index += 1
        blocks: list[list[str]] = []
        while index < len(lines) and lines[index].strip() != f"</{field}>":
            if lines[index].strip() != f"<{block_name}>":
                raise ScanProtocolError(f"{field} may contain only {block_name} blocks")
            block, index = self._consume_block(lines, index, block_name)
            blocks.append(block)
        if index >= len(lines):
            raise ScanProtocolError(f"missing </{field}>")
        return blocks, index + 1

    def _consume_field(
        self, lines: list[str], index: int, field: str
    ) -> tuple[str, int]:
        if index >= len(lines):
            raise ScanProtocolError(f"missing {field}")

        single_prefix = f"<{field}>"
        single_suffix = f"</{field}>"
        stripped = lines[index].strip()
        if field not in self._multiline_fields:
            if not stripped.startswith(single_prefix) or not stripped.endswith(
                single_suffix
            ):
                raise ScanProtocolError(f"{field} must be on one line")
            value = stripped[len(single_prefix) : -len(single_suffix)]
            if "<" in value or ">" in value:
                raise ScanProtocolError(f"reserved tag syntax in {field}")
            return value.strip(), index + 1

        if stripped.startswith(single_prefix) and stripped.endswith(single_suffix):
            value = stripped[len(single_prefix) : -len(single_suffix)]
            if self._is_reserved_tag_line(value.strip()):
                raise ScanProtocolError(f"reserved protocol tag inside {field}")
            return value, index + 1

        if not stripped.startswith(single_prefix):
            raise ScanProtocolError(f"expected {single_prefix}")
        after_open = stripped[len(single_prefix) :]
        index += 1
        content: list[str] = []
        if after_open.strip():
            content.append(after_open)

        closed = False
        while index < len(lines):
            current = lines[index]
            stripped_current = current.strip()
            if stripped_current == single_suffix:
                index += 1
                closed = True
                break
            if stripped_current.endswith(single_suffix):
                content.append(current[: current.rfind(single_suffix)])
                index += 1
                closed = True
                break
            if self._is_reserved_tag_line(stripped_current):
                raise ScanProtocolError(f"reserved protocol tag inside {field}")
            content.append(current)
            index += 1

        if not closed:
            raise ScanProtocolError(f"missing {single_suffix}")
        return self._strip_blank_lines(content), index

    @staticmethod
    def _strip_blank_lines(lines: list[str]) -> str:
        body = list(lines)
        while body and not body[0].strip():
            body.pop(0)
        while body and not body[-1].strip():
            body.pop()
        return "\n".join(body)

    @staticmethod
    def _consume_block(
        lines: list[str], index: int, name: str
    ) -> tuple[list[str], int]:
        end_tag = f"</{name}>"
        index += 1
        content: list[str] = []
        while index < len(lines) and lines[index].strip() != end_tag:
            content.append(lines[index])
            index += 1
        if index >= len(lines):
            raise ScanProtocolError(f"missing {end_tag}")
        return content, index + 1

    @staticmethod
    def _parse_score(value: str) -> int:
        try:
            score = int(value)
        except ValueError as exc:
            raise ScanProtocolError(
                "OVERALL_SCORE must be an integer from 1 to 100"
            ) from exc
        if not 1 <= score <= 100:
            raise ScanProtocolError("OVERALL_SCORE must be between 1 and 100")
        return score

    @staticmethod
    def _parse_confidence(value: str) -> int:
        try:
            confidence = int(value)
        except ValueError as exc:
            raise ScanProtocolError(
                "CONFIDENCE must be an integer from 0 to 100"
            ) from exc
        if not 0 <= confidence <= 100:
            raise ScanProtocolError("CONFIDENCE must be between 0 and 100")
        return confidence

    @staticmethod
    def _parse_optional_positive_int(value: str) -> int | None:
        value = value.strip()
        if value == "NONE":
            return None
        try:
            number = int(value)
        except ValueError as exc:
            raise ScanProtocolError(
                "line fields must be a positive integer or NONE"
            ) from exc
        if number < 1:
            raise ScanProtocolError("line fields must be positive")
        return number

    @staticmethod
    def _parse_optional_text(value: str) -> str | None:
        value = value.strip()
        return None if value == "NONE" else value

    @staticmethod
    def _require_exact_fields(
        fields: dict[str, str], expected: tuple[str, ...], scope: str
    ) -> None:
        if set(fields) != set(expected):
            raise ScanProtocolError(f"invalid {scope} fields")

    @classmethod
    def _is_reserved_tag_line(cls, line: str) -> bool:
        names = {
            "SAKURA_SCAN",
            "FINDING",
            *cls._root_fields,
            *cls._finding_fields,
        }
        return any(line in {f"<{name}>", f"</{name}>"} for name in names)


def safe_scan_protocol_failure(error: Exception) -> dict[str, Any]:
    """协议修复耗尽后的降级结果：显式失败而非伪装成空扫描。"""
    return {
        "overall_score": None,
        "summary": f"Scan protocol parse failed after repair attempts: {error!s}",
        "findings": [],
        "parse_source": "scan_protocol_error",
    }
