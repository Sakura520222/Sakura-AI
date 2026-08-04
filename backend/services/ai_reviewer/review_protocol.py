"""Strict tagged protocol for machine-readable PR review results."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from loguru import logger

PROTOCOL_VERSION = "1"
VALID_SEVERITIES = {"critical", "major", "minor", "suggestion"}
VALID_DECISIONS = {"approve", "request_changes", "comment"}
SEVERITY_TO_ISSUE_KEY = {
    "critical": "critical",
    "major": "major",
    "minor": "minor",
    "suggestion": "suggestions",
}

REPAIR_INSTRUCTION = """Your previous response did not match the required SAKURA_REVIEW protocol.
Reformat the same review conclusions only. Do not add, remove, or reconsider findings.
Return exactly one <SAKURA_REVIEW> envelope and no text outside it.
Use VERSION 1, a SCORE from 1 to 10, a valid DECISION, and complete FINDING fields.
Put DECISION_REASON, SUMMARY, TITLE, DESCRIPTION, and SUGGESTION opening and closing tags on separate lines.
Each FINDING must contain exactly these fields, each appearing exactly once and in this exact order: SEVERITY, FILE, START_LINE, END_LINE, TITLE, DESCRIPTION, SUGGESTION.
Do not repeat any field. After DESCRIPTION the next tag must be <SUGGESTION>; never re-emit <START_LINE> or <END_LINE> with NONE, and never write a second <DESCRIPTION>.
If a finding needs no one-click code fix, put the value NONE inside <SUGGESTION>NONE</SUGGESTION>; keep the tag name SUGGESTION and keep the original START_LINE/END_LINE values unchanged.
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

        lines = self._extract_unique_envelope(text)

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
        score = self._validate_score_consistency(score, findings)
        return {
            "score": score,
            "decision": decision,
            "decision_reason": decision_reason,
            "summary": summary,
            "findings": findings,
        }

    @staticmethod
    def _extract_unique_envelope(text: str) -> list[str]:
        """Extract one complete envelope without changing its contents.

        Some providers reliably wrap structured text in ```xml or ```text even
        when instructed not to, or prepend a short completion preamble. Removing
        only those presentation wrappers is deterministic and does not infer,
        reinterpret, or repair any review field.
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

        opening = [
            index
            for index, line in enumerate(lines)
            if line.strip() == "<SAKURA_REVIEW>"
        ]
        closing = [
            index
            for index, line in enumerate(lines)
            if line.strip() == "</SAKURA_REVIEW>"
        ]
        if len(opening) != 1 or len(closing) != 1 or closing[0] <= opening[0]:
            raise ReviewProtocolError(
                "response must contain exactly one SAKURA_REVIEW envelope"
            )
        return lines[opening[0] : closing[0] + 1]

    def _parse_root(self, lines: list[str]) -> tuple[dict[str, str], list[list[str]]]:
        fields: dict[str, str] = {}
        findings: list[list[str]] = []
        index = 0

        for field in self._root_fields:
            # Tolerate blank lines between root fields (readability spacing).
            while index < len(lines) and not lines[index].strip():
                index += 1
            if field == "FINDINGS":
                if index >= len(lines) or lines[index].strip() != "<FINDINGS>":
                    raise ReviewProtocolError("expected <FINDINGS>")
                index += 1
                while index < len(lines) and lines[index].strip() != "</FINDINGS>":
                    if not lines[index].strip():
                        # Tolerate blank lines between FINDING blocks: the model
                        # often inserts them for readability, and rejecting them
                        # forces an otherwise-valid review into manual re-review.
                        index += 1
                        continue
                    if lines[index].strip() != "<FINDING>":
                        raise ReviewProtocolError(
                            "FINDINGS may contain only FINDING blocks"
                        )
                    block, index = self._consume_block(lines, index, "FINDING")
                    findings.append(block)
                if index >= len(lines):
                    raise ReviewProtocolError("missing </FINDINGS>")
                fields["FINDINGS"] = ""
                index += 1
                continue

            value, index = self._consume_field(
                lines, index, field, field_order=self._root_fields
            )
            fields[field] = value

        if index != len(lines):
            raise ReviewProtocolError("unexpected content after FINDINGS")
        return fields, findings

    def _parse_finding(self, lines: list[str]) -> TaggedFinding:
        fields: dict[str, str] = {}
        index = 0
        for field in self._finding_fields:
            # Tolerate blank lines between finding fields (readability spacing).
            while index < len(lines) and not lines[index].strip():
                index += 1
            value, index = self._consume_field(
                lines, index, field, field_order=self._finding_fields
            )
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
        # Preserve SUGGESTION indentation (including the first line); stripping
        # here would eat the first line's indentation and misalign the one-click
        # suggestion. Normalize only when checking for the NONE sentinel.
        suggestion_value = fields["SUGGESTION"]
        if not title or not description:
            raise ReviewProtocolError("finding title and description must not be empty")

        return TaggedFinding(
            severity=severity,
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            title=title,
            description=description,
            suggestion=None if suggestion_value.strip() == "NONE" else suggestion_value,
        )

    def _consume_field(
        self,
        lines: list[str],
        index: int,
        field: str,
        *,
        field_order: tuple[str, ...] | None = None,
    ) -> tuple[str, int]:
        if index >= len(lines):
            raise ReviewProtocolError(f"missing {field}")

        single_prefix = f"<{field}>"
        single_suffix = f"</{field}>"
        stripped = lines[index].strip()
        if field not in self._multiline_fields:
            if not stripped.startswith(single_prefix) or not stripped.endswith(
                single_suffix
            ):
                raise ReviewProtocolError(f"{field} must be on one line")
            value = stripped[len(single_prefix) : -len(single_suffix)]
            if "<" in value or ">" in value:
                raise ReviewProtocolError(f"reserved tag syntax in {field}")
            return value.strip(), index + 1

        # Full single-line form: <TAG>value</TAG>. Do not .strip() the value —
        # SUGGESTION code needs its leading indentation preserved to align with
        # the original source; natural-language fields are normalized later in
        # _parse_finding as needed.
        if stripped.startswith(single_prefix) and stripped.endswith(single_suffix):
            value = stripped[len(single_prefix) : -len(single_suffix)]
            if self._is_reserved_tag_line(value.strip()):
                raise ReviewProtocolError(f"reserved protocol tag inside {field}")
            return value, index + 1

        # Opening tag may sit on its own line or share the line with the first
        # content line (compact form "<TAG>first line"). Both are accepted.
        if not stripped.startswith(single_prefix):
            raise self._unexpected_field_error(field, stripped, field_order)
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
                # Compact closing: "last line</TAG>"
                content.append(current[: current.rfind(single_suffix)])
                index += 1
                closed = True
                break
            if self._is_reserved_tag_line(stripped_current):
                raise ReviewProtocolError(f"reserved protocol tag inside {field}")
            content.append(current)
            index += 1

        if not closed:
            # Tolerate a missing closing tag on a trailing multiline field.
            # In practice this is SUGGESTION (the last finding field): the
            # model sometimes emits replacement code and omits </SUGGESTION>.
            # Other multiline fields hit a reserved-tag line first, so this
            # branch only rescues a benign truncation instead of masking
            # structural corruption. Accept the collected content so the
            # finding (and its one-click suggestion) survives rather than
            # forcing the whole review into manual re-review.
            logger.warning(
                "tolerated missing </{}>; accepted {} content line(s)",
                field,
                len(content),
            )
        return self._strip_blank_lines(content), index

    @classmethod
    def _unexpected_field_error(
        cls,
        expected: str,
        stripped: str,
        field_order: tuple[str, ...] | None,
    ) -> ReviewProtocolError:
        """Translate "expected X but found Y" into a model-actionable message.

        The parser consumes fields in a fixed order, each exactly once. When the
        model repeats or reorders a tag, the raw ``expected <X>`` is opaque to the
        repair model, so the repair loop cannot converge. Point at the actual tag,
        flag repeats explicitly, and append the full field order so the model can
        correct itself instead of re-emitting the same shape.
        """
        actual = cls._extract_tag_name(stripped)
        order = ""
        if field_order is not None:
            order = (
                "; each field must appear exactly once, in this order: "
                + ", ".join(field_order)
            )
        if (
            field_order is not None
            and actual in field_order
            and field_order.index(actual) < field_order.index(expected)
        ):
            return ReviewProtocolError(
                f"<{actual}> is repeated (already used earlier in this block); "
                f"expected <{expected}> next{order}"
            )
        return ReviewProtocolError(
            f"expected <{expected}> next but found <{actual}>{order}"
        )

    @staticmethod
    def _extract_tag_name(stripped: str) -> str:
        """Extract TAG from '<TAG>...' or '</TAG>'; fall back to truncated text."""
        match = re.match(r"</?([A-Z_]+)>", stripped)
        return match.group(1) if match else stripped[:40]

    @staticmethod
    def _strip_blank_lines(lines: list[str]) -> str:
        """Join lines, dropping only leading/trailing blank lines.

        A bare ``str.strip()`` on the joined text would also strip the
        first content line's indentation, which misaligns a multi-line
        SUGGESTION replacement against the original source when GitHub
        renders it. Preserve per-line indentation and trim blank lines only.
        """
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
            raise ReviewProtocolError(
                f"{field} must be a positive integer or NONE"
            ) from exc
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
    def _validate_score_consistency(score: int, findings: list[TaggedFinding]) -> int:
        """Clamp SCORE to the ceiling implied by finding severities.

        The model sometimes assigns a score exceeding its own severities
        (e.g. SCORE=7 with a major finding). Rejecting here would force the
        whole review into manual re-review and lose every finding, so clamp
        the score down to the severity ceiling with a warning instead.
        """
        severities = {finding.severity for finding in findings}
        ceiling = 10
        if "critical" in severities:
            ceiling = min(ceiling, 3)
        if "major" in severities:
            ceiling = min(ceiling, 6)
        if score > ceiling:
            logger.warning(
                "score {} exceeds ceiling {} for severities {}; clamped to {}",
                score,
                ceiling,
                severities,
                ceiling,
            )
            return ceiling
        return score


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

    for finding in parsed["findings"]:
        issue_text = finding.title
        result["issues"][SEVERITY_TO_ISSUE_KEY[finding.severity]].append(issue_text)
        body_parts = [f"**{finding.title}**", finding.description]
        if finding.suggestion:
            if finding.file_path is not None:
                # GitHub one-click suggestion: replaces START_LINE..END_LINE
                body_parts.append(
                    f"**Suggestion:**\n```suggestion\n{finding.suggestion}\n```"
                )
            else:
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
