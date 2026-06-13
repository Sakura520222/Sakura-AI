"""Strict tagged protocol for machine-readable PR review results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


PROTOCOL_VERSION = "1"
VALID_SEVERITIES = {"critical", "major", "minor", "suggestion"}
VALID_DECISIONS = {"approve", "request_changes", "comment"}

REPAIR_INSTRUCTION = """Your previous response did not match the required SAKURA_REVIEW protocol.
Reformat the same review conclusions only. Do not add, remove, or reconsider findings.
Return exactly one <SAKURA_REVIEW> envelope and no text outside it.
Use VERSION 1, a SCORE from 1 to 10, a valid DECISION, and complete FINDING fields.
Keep protocol tags and enum values in English. Preserve the requested language only inside natural-language fields."""


class ReviewProtocolError(ValueError):
    """Raised when an AI review response violates the tagged protocol."""


@dataclass(frozen=True)
class TaggedFinding:
    severity: str
    file_path: str | None
    start_line: int | None
    end_line: int | None
    title: str
    description: str
    suggestion: str | None


class TaggedReviewParser:
    """Parse the line-oriented SAKURA_REVIEW envelope without XML heuristics."""

    _root_fields = (
        "VERSION",
        "SCORE",
        "DECISION",
        "DECISION_REASON",
        "SUMMARY",
        "FINDINGS",
    )
    _finding_fields = (
        "SEVERITY",
        "FILE",
        "START_LINE",
        "END_LINE",
        "TITLE",
        "DESCRIPTION",
        "SUGGESTION",
    )
    _multiline_fields = {
        "DECISION_REASON",
        "SUMMARY",
        "TITLE",
        "DESCRIPTION",
        "SUGGESTION",
    }

    def parse(self, text: str) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise ReviewProtocolError("empty review response")

        lines = self._unwrap_code_fence(text)
        if lines[0].strip() != "<SAKURA_REVIEW>" or lines[-1].strip() != "</SAKURA_REVIEW>":
            raise ReviewProtocolError("response must contain exactly one SAKURA_REVIEW envelope")
        if sum(line.strip() == "<SAKURA_REVIEW>" for line in lines) != 1:
            raise ReviewProtocolError("duplicate SAKURA_REVIEW envelope")
        if sum(line.strip() == "</SAKURA_REVIEW>" for line in lines) != 1:
            raise ReviewProtocolError("duplicate SAKURA_REVIEW closing tag")

        root_lines = lines[1:-1]
        fields, finding_blocks = self._parse_root(root_lines)
        self._require_exact_fields(fields, self._root_fields, "review")

        version = fields["VERSION"]
        if version != PROTOCOL_VERSION:
            raise ReviewProtocolError(f"unsupported protocol version: {version}")

        score = self._parse_score(fields["SCORE"])
        decision = fields["DECISION"]
        if decision not in VALID_DECISIONS:
            raise ReviewProtocolError(f"invalid decision: {decision}")

        decision_reason = fields["DECISION_REASON"].strip()
        summary = fields["SUMMARY"].strip()
        if not decision_reason:
            raise ReviewProtocolError("DECISION_REASON must not be empty")
        if not summary:
            raise ReviewProtocolError("SUMMARY must not be empty")

        findings = [self._parse_finding(block) for block in finding_blocks]
        self._validate_score_consistency(score, findings)
        return {
            "score": score,
            "decision": decision,
            "decision_reason": decision_reason,
            "summary": summary,
            "findings": findings,
        }

    @staticmethod
    def _unwrap_code_fence(text: str) -> list[str]:
        """Accept a single Markdown fence around the complete envelope.

        Some providers reliably wrap structured text in ```xml or ```text even
        when instructed not to. Removing only that outer presentation wrapper
        is deterministic and does not reinterpret or repair review contents.
        """
        lines = text.strip().splitlines()
        if len(lines) >= 3 and lines[0].strip() in {
            "```",
            "```xml",
            "```text",
            "```plaintext",
        }:
            if lines[-1].strip() != "```":
                raise ReviewProtocolError("unterminated outer code fence")
            lines = lines[1:-1]
            while lines and not lines[0].strip():
                lines.pop(0)
            while lines and not lines[-1].strip():
                lines.pop()
        if not lines:
            raise ReviewProtocolError("empty review response")
        return lines

    def _parse_root(
        self, lines: list[str]
    ) -> tuple[dict[str, str], list[list[str]]]:
        fields: dict[str, str] = {}
        findings: list[list[str]] = []
        index = 0

        for field in self._root_fields:
            if field == "FINDINGS":
                if index >= len(lines) or lines[index].strip() != "<FINDINGS>":
                    raise ReviewProtocolError("expected <FINDINGS>")
                index += 1
                while index < len(lines) and lines[index].strip() != "</FINDINGS>":
                    if lines[index].strip() != "<FINDING>":
                        raise ReviewProtocolError("FINDINGS may contain only FINDING blocks")
                    block, index = self._consume_block(lines, index, "FINDING")
                    findings.append(block)
                if index >= len(lines):
                    raise ReviewProtocolError("missing </FINDINGS>")
                fields["FINDINGS"] = ""
                index += 1
                continue

            value, index = self._consume_field(lines, index, field)
            fields[field] = value

        if index != len(lines):
            raise ReviewProtocolError("unexpected content after FINDINGS")
        return fields, findings

    def _parse_finding(self, lines: list[str]) -> TaggedFinding:
        fields: dict[str, str] = {}
        index = 0
        for field in self._finding_fields:
            value, index = self._consume_field(lines, index, field)
            fields[field] = value
        if index != len(lines):
            raise ReviewProtocolError("unexpected content in FINDING")
        self._require_exact_fields(fields, self._finding_fields, "finding")

        severity = fields["SEVERITY"]
        if severity not in VALID_SEVERITIES:
            raise ReviewProtocolError(f"invalid severity: {severity}")

        file_value = fields["FILE"].strip()
        start_line = self._parse_optional_line(fields["START_LINE"], "START_LINE")
        end_line = self._parse_optional_line(fields["END_LINE"], "END_LINE")
        if file_value == "NONE":
            if start_line is not None or end_line is not None:
                raise ReviewProtocolError("overall findings must use NONE line values")
            file_path = None
        else:
            if not file_value or file_value.upper() == "NONE":
                raise ReviewProtocolError("FILE must be a path or exact NONE")
            if start_line is None or end_line is None:
                raise ReviewProtocolError("file findings require both line values")
            if start_line > end_line:
                raise ReviewProtocolError("START_LINE must not exceed END_LINE")
            file_path = file_value

        title = fields["TITLE"].strip()
        description = fields["DESCRIPTION"].strip()
        suggestion_value = fields["SUGGESTION"].strip()
        if not title or not description:
            raise ReviewProtocolError("finding title and description must not be empty")

        return TaggedFinding(
            severity=severity,
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            title=title,
            description=description,
            suggestion=None if suggestion_value == "NONE" else suggestion_value,
        )

    def _consume_field(
        self, lines: list[str], index: int, field: str
    ) -> tuple[str, int]:
        if index >= len(lines):
            raise ReviewProtocolError(f"missing {field}")

        single_prefix = f"<{field}>"
        single_suffix = f"</{field}>"
        stripped = lines[index].strip()
        if field not in self._multiline_fields:
            if not stripped.startswith(single_prefix) or not stripped.endswith(single_suffix):
                raise ReviewProtocolError(f"{field} must be on one line")
            value = stripped[len(single_prefix) : -len(single_suffix)]
            if "<" in value or ">" in value:
                raise ReviewProtocolError(f"reserved tag syntax in {field}")
            return value.strip(), index + 1

        if stripped != single_prefix:
            raise ReviewProtocolError(f"expected {single_prefix}")
        index += 1
        content: list[str] = []
        while index < len(lines) and lines[index].strip() != single_suffix:
            if self._is_reserved_tag_line(lines[index].strip()):
                raise ReviewProtocolError(f"reserved protocol tag inside {field}")
            content.append(lines[index])
            index += 1
        if index >= len(lines):
            raise ReviewProtocolError(f"missing {single_suffix}")
        return "\n".join(content).strip(), index + 1

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
            raise ReviewProtocolError(f"missing {end_tag}")
        return content, index + 1

    @staticmethod
    def _parse_score(value: str) -> int:
        try:
            score = int(value)
        except ValueError as exc:
            raise ReviewProtocolError("SCORE must be an integer") from exc
        if not 1 <= score <= 10:
            raise ReviewProtocolError("SCORE must be between 1 and 10")
        return score

    @staticmethod
    def _parse_optional_line(value: str, field: str) -> int | None:
        value = value.strip()
        if value == "NONE":
            return None
        try:
            line = int(value)
        except ValueError as exc:
            raise ReviewProtocolError(f"{field} must be a positive integer or NONE") from exc
        if line < 1:
            raise ReviewProtocolError(f"{field} must be positive")
        return line

    @staticmethod
    def _require_exact_fields(
        fields: dict[str, str], expected: tuple[str, ...], scope: str
    ) -> None:
        if set(fields) != set(expected):
            raise ReviewProtocolError(f"invalid {scope} fields")

    @classmethod
    def _is_reserved_tag_line(cls, line: str) -> bool:
        names = {"SAKURA_REVIEW", "FINDING", *cls._root_fields, *cls._finding_fields}
        return any(line in {f"<{name}>", f"</{name}>"} for name in names)

    @staticmethod
    def _validate_score_consistency(
        score: int, findings: list[TaggedFinding]
    ) -> None:
        severities = {finding.severity for finding in findings}
        if "critical" in severities and score > 3:
            raise ReviewProtocolError("critical findings require SCORE <= 3")
        if "major" in severities and score > 6:
            raise ReviewProtocolError("major findings require SCORE <= 6")


def to_review_result(parsed: dict[str, Any]) -> dict[str, Any]:
    """Convert a validated protocol object to the established result dictionary."""
    result: dict[str, Any] = {
        "summary": parsed["summary"],
        "comments": [],
        "inline_comments": [],
        "overall_score": parsed["score"],
        "issues": {"critical": [], "major": [], "minor": [], "suggestions": []},
        "parse_source": "tagged",
        "ai_decision": parsed["decision"],
        "ai_decision_reason": parsed["decision_reason"],
    }

    issue_keys = {
        "critical": "critical",
        "major": "major",
        "minor": "minor",
        "suggestion": "suggestions",
    }
    for finding in parsed["findings"]:
        issue_text = finding.title
        result["issues"][issue_keys[finding.severity]].append(issue_text)
        body_parts = [f"**{finding.title}**", finding.description]
        if finding.suggestion:
            body_parts.append(f"**Suggestion:** {finding.suggestion}")
        body = "\n\n".join(body_parts)

        if finding.file_path is None:
            result["comments"].append(
                {
                    "content": body,
                    "severity": finding.severity,
                    "type": "overall",
                }
            )
        else:
            result["inline_comments"].append(
                {
                    "file_path": finding.file_path,
                    "start_line": finding.start_line,
                    "line_number": finding.end_line,
                    "body": body,
                    "severity": finding.severity,
                }
            )
    return result


def safe_protocol_failure(error: Exception) -> dict[str, Any]:
    """Return a non-blocking result when both protocol attempts fail."""
    return {
        "summary": f"AI review output could not be validated: {error}",
        "comments": [],
        "inline_comments": [],
        "overall_score": None,
        "issues": {"critical": [], "major": [], "minor": [], "suggestions": []},
        "parse_source": "protocol_error",
        "ai_decision": "comment",
        "ai_decision_reason": "The structured review output was invalid; manual review is required.",
    }
