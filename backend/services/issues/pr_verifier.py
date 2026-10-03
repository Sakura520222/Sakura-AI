"""Strict PR relation decisions grounded in current source facts."""

import asyncio
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from backend.core.ai_protocol.errors import (
    AllCandidatesFailedError,
    ReviewCancelledError,
)
from backend.core.config import get_dynamic_config
from backend.services.ai_reviewer.api_client import AIApiClient
from backend.services.issues.pr_budget import (
    PRBudgetError,
    check_boundary,
    resolve_pr_input_budget,
)
from backend.services.pr_body import strip_sakura_generated_sections


def _changed_runs(patch: str) -> dict[str, list[str]]:
    """Keep diff direction and contiguous runs within each unified-diff hunk."""
    runs = {"added": [], "removed": []}
    direction, lines, in_hunk = None, [], False

    def finish():
        if lines:
            runs[direction].append("\n".join(lines))
            lines.clear()

    for line in patch.splitlines():
        if re.match(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", line):
            finish()
            direction, in_hunk = None, True
            continue
        change = {"+": "added", "-": "removed"}.get(line[:1]) if in_hunk else None
        if change is None or change != direction:
            finish()
            direction = change
        if change is not None:
            lines.append(line[1:])
    finish()
    return runs


@dataclass
class PRVerificationResult:
    succeeded: bool
    relations: list[dict[str, Any]] = field(default_factory=list)
    failure: str | None = None


class PRRelationVerifier:
    def __init__(self, client=None):
        self.client = client or AIApiClient()

    async def resolve_budget(self, *, cancel_event=None, deadline=None):
        return await resolve_pr_input_budget(
            self.client, cancel_event=cancel_event, deadline=deadline
        )

    async def verify(
        self,
        *,
        pr_title: str,
        pr_body: str,
        candidates: list[dict],
        files: list[dict],
        cancel_event=None,
        deadline=None,
        context=None,
        observer=None,
        raise_configuration_error=False,
    ) -> PRVerificationResult:
        if cancel_event is not None and cancel_event.is_set():
            raise ReviewCancelledError()
        if deadline is not None and deadline.is_expired():
            return PRVerificationResult(False, failure="deadline")
        try:
            facts = {}
            for candidate in candidates:
                number = candidate.get("number")
                if (
                    type(number) is not int
                    or number <= 0
                    or number in facts
                    or not isinstance(candidate.get("title"), str)
                    or not isinstance(candidate.get("body"), str)
                    or candidate.get("state") != "open"
                ):
                    raise ValueError("incomplete candidate facts")
                facts[number] = candidate
            if not facts:
                return PRVerificationResult(True)
            related_threshold = await get_dynamic_config(
                "pr_issue_related_confidence_threshold", fresh=True
            )
            closing_threshold = await get_dynamic_config(
                "pr_issue_closing_confidence_threshold", fresh=True
            )
            if deadline is not None and deadline.is_expired():
                return PRVerificationResult(False, failure="deadline")
            budget = await self.resolve_budget(
                cancel_event=cancel_event, deadline=deadline
            )
            messages = budget.messages(
                pr_title=pr_title,
                pr_body=strip_sakura_generated_sections(pr_body),
                files=files,
                candidates=candidates,
            )
            check_boundary(cancel_event, deadline)
            response = await self.client.call_with_retry(
                messages=messages,
                model="",
                role="summary",
                cancel_event=cancel_event,
                context=context,
                observer=observer,
            )
            if cancel_event is not None and cancel_event.is_set():
                raise ReviewCancelledError()
            data = json.loads(response.choices[0].message.content)
            if (
                not isinstance(data, dict)
                or set(data) != {"relations"}
                or not isinstance(data["relations"], list)
            ):
                raise ValueError("invalid relation envelope")
            accepted, seen = [], set()
            patches = {f["path"]: f for f in files}
            changed_runs = {
                path: _changed_runs(source.get("patch") or "")
                for path, source in patches.items()
            }
            for relation in data["relations"]:
                if not isinstance(relation, dict) or set(relation) != {
                    "number",
                    "relation",
                    "confidence",
                    "reason",
                    "evidence",
                }:
                    raise ValueError("invalid relation fields")
                number, kind, confidence = (
                    relation["number"],
                    relation["relation"],
                    relation["confidence"],
                )
                if (
                    type(number) is not int
                    or number not in facts
                    or number in seen
                    or kind not in {"closes", "related"}
                ):
                    raise ValueError("unknown or repeated candidate/relation")
                seen.add(number)
                if (
                    type(confidence) not in (float, int)
                    or not math.isfinite(confidence)
                    or not 0 <= confidence <= 1
                ):
                    raise ValueError("invalid confidence")
                if (
                    not isinstance(relation["reason"], str)
                    or not relation["reason"].strip()
                ):
                    raise ValueError("missing reason")
                evidence = relation["evidence"]
                if not isinstance(evidence, list) or not evidence:
                    raise ValueError("missing evidence")
                for item in evidence:
                    if not isinstance(item, dict) or set(item) != {
                        "path",
                        "change",
                        "code_quote",
                        "issue_quote",
                    }:
                        raise ValueError("invalid evidence fields")
                    if any(
                        not isinstance(value, str) or not value.strip()
                        for value in item.values()
                    ):
                        raise ValueError("empty evidence")
                    if item["change"] not in {"added", "removed"}:
                        raise ValueError("invalid change direction")
                    source = patches.get(item["path"])
                    if source is None:
                        raise ValueError("unobserved changed path")
                    issue_text = facts[number]["title"] + "\n" + facts[number]["body"]
                    if (
                        not any(
                            item["code_quote"] in run
                            for run in changed_runs[item["path"]][item["change"]]
                        )
                        or item["issue_quote"] not in issue_text
                    ):
                        raise ValueError("ungrounded evidence")
                # Complete diff is a necessary condition for full resolution.
                if kind == "closes" and (
                    not files or any(not f.get("complete") for f in files)
                ):
                    continue
                if confidence >= (
                    closing_threshold if kind == "closes" else related_threshold
                ):
                    accepted.append({**facts[number], **relation})
            return PRVerificationResult(True, accepted)
        except asyncio.CancelledError, ReviewCancelledError:
            raise
        except Exception as exc:
            if raise_configuration_error and isinstance(exc, AllCandidatesFailedError):
                raise
            if (
                raise_configuration_error
                and isinstance(exc, PRBudgetError)
                and exc.missing_role
            ):
                # Public role resolution returns [] on a missing/failed binding.
                # Preserve the legacy list API's configuration-error boundary.
                raise AllCandidatesFailedError(
                    "角色 summary 无可用 AI 候选模型，无法核验 PR 关系预算。"
                ) from exc
            # Do not log provider content/credentials; retain an observable status.
            failure = (
                exc.failure if isinstance(exc, PRBudgetError) else type(exc).__name__
            )
            logger.warning("PR relation verification failed: {}", failure)
            return PRVerificationResult(False, failure=failure)
