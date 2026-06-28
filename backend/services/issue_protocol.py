"""Strict tagged protocol for machine-readable Issue analysis results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


PROTOCOL_VERSION = "1"
VALID_PRIORITIES = {"critical", "high", "medium", "low"}


class IssueProtocolError(ValueError):
    """Raised when an AI Issue analysis response violates the tagged protocol."""


@dataclass(frozen=True)
class TaggedIssueLabel:
    name: str
    confidence: float
    reason: str


@dataclass(frozen=True)
class TaggedIssueAssignee:
    username: str
    confidence: float
    reason: str


class TaggedIssueAnalysisParser:
    """Parse the line-oriented SAKURA_ISSUE_ANALYSIS envelope."""

    _root_fields = (
        "VERSION",
        "CATEGORY",
        "PRIORITY",
        "SUMMARY",
        "FEASIBILITY",
        "SUGGESTED_LABELS",
        "SUGGESTED_ASSIGNEES",
        "SUGGESTED_MILESTONE",
        "DUPLICATE_OF",
        "SUGGESTED_TITLE",
    )
    _label_fields = ("NAME", "CONFIDENCE", "REASON")
    _assignee_fields = ("USERNAME", "CONFIDENCE", "REASON")
    _multiline_fields = {"SUMMARY", "FEASIBILITY", "SUGGESTED_TITLE", "REASON"}

    def __init__(self, valid_categories: set[str] | None = None):
        self.valid_categories = valid_categories

    def parse(self, text: str) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise IssueProtocolError("empty issue analysis response")

        lines = self._extract_unique_envelope(text)
        fields, label_blocks, assignee_blocks = self._parse_root(lines[1:-1])
        self._require_exact_fields(fields, self._root_fields, "issue analysis")

        version = fields["VERSION"]
        if version != PROTOCOL_VERSION:
            raise IssueProtocolError(f"unsupported protocol version: {version}")

        category = fields["CATEGORY"]
        if self.valid_categories is not None and category not in self.valid_categories:
            raise IssueProtocolError(f"invalid category: {category}")

        priority = fields["PRIORITY"]
        if priority not in VALID_PRIORITIES:
            raise IssueProtocolError(f"invalid priority: {priority}")

        summary = fields["SUMMARY"].strip()
        feasibility = fields["FEASIBILITY"].strip()
        if not summary:
            raise IssueProtocolError("SUMMARY must not be empty")
        if not feasibility:
            raise IssueProtocolError("FEASIBILITY must not be empty")

        suggested_milestone = self._parse_optional_text(fields["SUGGESTED_MILESTONE"])
        duplicate_of = self._parse_optional_issue_number(fields["DUPLICATE_OF"])
        suggested_title = self._parse_optional_text(fields["SUGGESTED_TITLE"])

        return {
            "category": category,
            "priority": priority,
            "summary": summary,
            "feasibility": feasibility,
            "suggested_labels": [self._parse_label(block) for block in label_blocks],
            "suggested_assignees": [
                self._parse_assignee(block) for block in assignee_blocks
            ],
            "suggested_milestone": suggested_milestone,
            "duplicate_of": duplicate_of,
            "suggested_title": suggested_title,
            "parse_source": "tagged_issue",
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
                raise IssueProtocolError("unterminated outer code fence")
            lines = lines[1:-1]
            while lines and not lines[0].strip():
                lines.pop(0)
            while lines and not lines[-1].strip():
                lines.pop()
        if not lines:
            raise IssueProtocolError("empty issue analysis response")

        opening = [
            index
            for index, line in enumerate(lines)
            if line.strip() == "<SAKURA_ISSUE_ANALYSIS>"
        ]
        closing = [
            index
            for index, line in enumerate(lines)
            if line.strip() == "</SAKURA_ISSUE_ANALYSIS>"
        ]
        if len(opening) != 1 or len(closing) != 1 or closing[0] <= opening[0]:
            raise IssueProtocolError(
                "response must contain exactly one SAKURA_ISSUE_ANALYSIS envelope"
            )
        return lines[opening[0] : closing[0] + 1]

    def _parse_root(
        self, lines: list[str]
    ) -> tuple[dict[str, str], list[list[str]], list[list[str]]]:
        fields: dict[str, str] = {}
        labels: list[list[str]] = []
        assignees: list[list[str]] = []
        index = 0

        for field in self._root_fields:
            if field == "SUGGESTED_LABELS":
                labels, index = self._consume_collection(lines, index, field, "LABEL")
                fields[field] = ""
                continue
            if field == "SUGGESTED_ASSIGNEES":
                assignees, index = self._consume_collection(
                    lines, index, field, "ASSIGNEE"
                )
                fields[field] = ""
                continue

            value, index = self._consume_field(lines, index, field)
            fields[field] = value

        if index != len(lines):
            raise IssueProtocolError("unexpected content after issue analysis fields")
        return fields, labels, assignees

    def _parse_label(self, lines: list[str]) -> dict[str, Any]:
        fields = self._parse_block_fields(lines, self._label_fields, "label")
        name = fields["NAME"].strip()
        reason = fields["REASON"].strip()
        if not name:
            raise IssueProtocolError("label NAME must not be empty")
        return {
            "name": name,
            "confidence": self._parse_confidence(fields["CONFIDENCE"]),
            "reason": reason,
        }

    def _parse_assignee(self, lines: list[str]) -> dict[str, Any]:
        fields = self._parse_block_fields(lines, self._assignee_fields, "assignee")
        username = fields["USERNAME"].strip()
        reason = fields["REASON"].strip()
        if not username:
            raise IssueProtocolError("assignee USERNAME must not be empty")
        return {
            "username": username,
            "confidence": self._parse_confidence(fields["CONFIDENCE"]),
            "reason": reason,
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
            raise IssueProtocolError(f"unexpected content in {scope}")
        self._require_exact_fields(fields, expected, scope)
        return fields

    def _consume_collection(
        self, lines: list[str], index: int, field: str, block_name: str
    ) -> tuple[list[list[str]], int]:
        if index >= len(lines) or lines[index].strip() != f"<{field}>":
            raise IssueProtocolError(f"expected <{field}>")
        index += 1
        blocks: list[list[str]] = []
        while index < len(lines) and lines[index].strip() != f"</{field}>":
            if lines[index].strip() != f"<{block_name}>":
                raise IssueProtocolError(
                    f"{field} may contain only {block_name} blocks"
                )
            block, index = self._consume_block(lines, index, block_name)
            blocks.append(block)
        if index >= len(lines):
            raise IssueProtocolError(f"missing </{field}>")
        return blocks, index + 1

    def _consume_field(
        self, lines: list[str], index: int, field: str
    ) -> tuple[str, int]:
        if index >= len(lines):
            raise IssueProtocolError(f"missing {field}")

        single_prefix = f"<{field}>"
        single_suffix = f"</{field}>"
        stripped = lines[index].strip()
        if field not in self._multiline_fields:
            if not stripped.startswith(single_prefix) or not stripped.endswith(
                single_suffix
            ):
                raise IssueProtocolError(f"{field} must be on one line")
            value = stripped[len(single_prefix) : -len(single_suffix)]
            if "<" in value or ">" in value:
                raise IssueProtocolError(f"reserved tag syntax in {field}")
            return value.strip(), index + 1

        if stripped != single_prefix:
            raise IssueProtocolError(f"expected {single_prefix}")
        index += 1
        content: list[str] = []
        while index < len(lines) and lines[index].strip() != single_suffix:
            if self._is_reserved_tag_line(lines[index].strip()):
                raise IssueProtocolError(f"reserved protocol tag inside {field}")
            content.append(lines[index])
            index += 1
        if index >= len(lines):
            raise IssueProtocolError(f"missing {single_suffix}")
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
            raise IssueProtocolError(f"missing {end_tag}")
        return content, index + 1

    @staticmethod
    def _parse_confidence(value: str) -> float:
        try:
            confidence = float(value)
        except ValueError as exc:
            raise IssueProtocolError("CONFIDENCE must be a number") from exc
        if not 0 <= confidence <= 1:
            raise IssueProtocolError("CONFIDENCE must be between 0 and 1")
        return confidence

    @staticmethod
    def _parse_optional_issue_number(value: str) -> int | None:
        value = value.strip()
        if value == "NONE":
            return None
        try:
            issue_number = int(value)
        except ValueError as exc:
            raise IssueProtocolError(
                "DUPLICATE_OF must be a positive integer or NONE"
            ) from exc
        if issue_number < 1:
            raise IssueProtocolError("DUPLICATE_OF must be positive")
        return issue_number

    @staticmethod
    def _parse_optional_text(value: str) -> str | None:
        value = value.strip()
        return None if value == "NONE" else value

    @staticmethod
    def _require_exact_fields(
        fields: dict[str, str], expected: tuple[str, ...], scope: str
    ) -> None:
        if set(fields) != set(expected):
            raise IssueProtocolError(f"invalid {scope} fields")

    @classmethod
    def _is_reserved_tag_line(cls, line: str) -> bool:
        names = {
            "SAKURA_ISSUE_ANALYSIS",
            "LABEL",
            "ASSIGNEE",
            *cls._root_fields,
            *cls._label_fields,
            *cls._assignee_fields,
        }
        return any(line in {f"<{name}>", f"</{name}>"} for name in names)


def safe_issue_protocol_failure(error: Exception) -> dict[str, Any]:
    """Return a non-blocking Issue analysis result on protocol failure."""
    return {
        "category": "other",
        "priority": "medium",
        "summary": f"AI Issue analysis output could not be validated: {error}",
        "feasibility": "无法评估",
        "suggested_labels": [],
        "suggested_assignees": [],
        "suggested_milestone": None,
        "duplicate_of": None,
        "suggested_title": None,
        "parse_source": "protocol_error",
    }
