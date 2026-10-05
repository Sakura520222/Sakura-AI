"""Observable repetition prompts strategy review; it never terminates work.

The user-specified window of ten is a detection window, not an execution budget.
Changed results or operations and real user context reset detection. Exact
comparison is deliberately not a claim of semantic loop detection.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


class StrategySelfCheckState:
    def __init__(self):
        self.cursor = 0
        self.calls: dict[str, dict[str, Any]] = {}
        self.streaks: dict[str, tuple[str, int, bool]] = {}
        self.guidance_ids: set[str] = set()

    @staticmethod
    def _parse(value):
        try:
            return json.loads(value)
        except TypeError, ValueError:
            return value

    def update(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        events = []
        while self.cursor < len(messages):
            message = messages[self.cursor]
            self.cursor += 1
            metadata = message.get("metadata") or {}
            marker = metadata.get("strategy_self_check")
            if isinstance(marker, dict):
                kind = marker.get("kind")
                prior = self.streaks.get(kind)
                if prior and prior[0] == marker.get("fingerprint"):
                    self.streaks[kind] = (prior[0], prior[1], True)
                    events = [
                        event
                        for event in events
                        if event["metadata"]["strategy_self_check"] != marker
                    ]
                continue
            role = message.get("role")
            if role == "user":
                if not (
                    metadata.get("completion_reminder")
                    or metadata.get("repository_context")
                ):
                    ids = (
                        metadata.get("guidance_ids")
                        or message.get("guidance_ids")
                        or message.get("prompt_ids")
                    )
                    if ids:
                        identities = {
                            json.dumps(item, sort_keys=True, default=str)
                            for item in ids
                        }
                        if identities <= self.guidance_ids:
                            continue
                        self.guidance_ids.update(identities)
                    self.streaks.clear()
                    events.clear()
                continue
            if role == "assistant" and message.get("tool_calls"):
                self.calls.update(
                    {
                        call["id"]: call.get("function") or {}
                        for call in message["tool_calls"]
                    }
                )
                continue
            if role == "assistant":
                kind, evidence = "text", message.get("content") or ""
            elif role == "tool":
                function = self.calls.pop(message.get("tool_call_id"), None)
                if function is None:
                    continue
                kind = "tool"
                evidence = {
                    "tool": function.get("name"),
                    "arguments": self._parse(function.get("arguments")),
                    "result": self._parse(message.get("content")),
                }
                self.streaks.pop("text", None)
            else:
                continue
            payload = json.dumps(
                evidence, sort_keys=True, ensure_ascii=False, default=str
            )
            fingerprint = hashlib.sha256(payload.encode()).hexdigest()
            previous, count, warned = self.streaks.get(kind, ("", 0, False))
            if previous != fingerprint:
                count, warned = 0, False
            count += 1
            if count == 10 and not warned:
                warned = True
                marker = {"kind": kind, "window": 10, "fingerprint": fingerprint}
                events.append(
                    {
                        "role": "user",
                        "content": "The runtime observed ten consecutive identical tool/argument/result observations or identical text replies. Reassess your strategy and seek new code, test, state or diagnostic evidence. Continue autonomously; this observation does not end the task or change the model. Call finish_task only when the task is complete and verified.",
                        "metadata": {"strategy_self_check": marker},
                    }
                )
            self.streaks[kind] = (fingerprint, count, warned)
        return events
