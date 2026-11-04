"""Explicit authoritative source double for unrelated relation protocol tests."""

import json
from copy import deepcopy

VERSION = "2026-10-02T00:00:00Z"


def discussion(comments, max_comments=20, max_chars=2000):
    rows = []
    for index, comment in enumerate(comments):
        rows.append(
            {
                "id": index + 1,
                "html_url": f"https://github.com/owner/repo/issues/1#issuecomment-{index + 1}",
                "user": {"login": comment.get("author", "reporter")},
                "created_at": VERSION,
                "updated_at": VERSION,
                "body_truncated": False,
                **deepcopy(comment),
            }
        )
    return rows, {
        "source": "github_issue_comments",
        "order": "newest_first",
        "total_count": len(rows),
        "included_count": len(rows),
        "max_comments": max_comments,
        "max_chars": max_chars,
        "bounded": True,
        "truncated": any(row["body_truncated"] for row in rows),
    }


def complete_source(facts, comments=None, max_comments=20, max_chars=2000):
    facts = deepcopy(facts)
    facts["number"] = facts.get("issue_number", facts.get("number"))
    facts["updated_at"] = facts.get("updated_at") or VERSION
    facts["body"] = facts.get("body") or ""
    facts["labels"] = [
        v["name"] if isinstance(v, dict) else v for v in facts.get("labels", [])
    ]
    facts.setdefault("state_reason", None)
    if "comments_context" not in facts or facts["comments_context"] is None:
        facts["comments"], facts["comments_context"] = discussion(
            comments if comments is not None else facts.get("comments", []),
            max_comments,
            max_chars,
        )
    return facts


class PacketSourceReader:
    """Stable source for protocol/budget tests; dedicated freshness tests use live repos."""

    def __init__(self, current, client):
        self.current = current
        self.client = client
        self.comments = []

    async def read(self, owner, name, number, **controls):
        if number == self.current.get("issue_number", self.current.get("number")):
            return complete_source(
                self.current,
                self.comments,
                controls["max_comments"],
                controls["max_chars"],
            )
        for call in reversed(self.client.call_with_retry.call_args_list):
            packet = json.loads(call.kwargs["messages"][1]["content"])
            for fact in packet["candidates"]:
                if fact["number"] == number:
                    return deepcopy(fact)
        raise ValueError("No test source")
