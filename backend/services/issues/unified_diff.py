"""Validate unified hunks before counting or grounding directed evidence."""

import re
from dataclasses import dataclass

_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?")
_HEADERS = (
    "diff --git ",
    "index ",
    "--- ",
    "+++ ",
    "old mode ",
    "new mode ",
    "new file mode ",
    "deleted file mode ",
    "similarity index ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
)


@dataclass
class UnifiedDiff:
    additions: int
    deletions: int
    runs: dict[str, list[str]]


def parse_unified_diff(patch: str, *, allow_no_hunk: bool = False) -> UnifiedDiff:
    """Require complete old/new spans, preserving ++/-- source within hunks.

    GitHub file patches usually omit file headers. A signed line is source only
    while the hunk has unconsumed spans; metadata outside hunks is never evidence.
    Callers may allow absent hunks only with authoritative zero-line counts.
    """
    if not isinstance(patch, str):
        raise ValueError("Invalid unified-diff text")
    runs = {"added": [], "removed": []}
    direction, run = None, []
    old_left = new_left = additions = deletions = 0
    old_end = new_end = 0
    saw_hunk = marker_allowed = False

    def finish():
        if run:
            runs[direction].append("\n".join(run))
            run.clear()

    lines = patch.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for line in lines:
        header = _HUNK.fullmatch(line)
        if header:
            if old_left or new_left:
                raise ValueError("Truncated unified-diff hunk")
            finish()
            old_start, old_count, new_start, new_count = header.groups()
            old_start, new_start = int(old_start), int(new_start)
            old_left = int(old_count) if old_count is not None else 1
            new_left = int(new_count) if new_count is not None else 1
            if (
                (old_left and not old_start)
                or (new_left and not new_start)
                or not (old_left or new_left)
            ):
                raise ValueError("Invalid unified-diff span")
            # A zero span refers to the boundary after start, not to a line.
            old_begin = old_start if old_left else old_start + 1
            new_begin = new_start if new_left else new_start + 1
            if saw_hunk and (old_begin < old_end or new_begin < new_end):
                raise ValueError("Overlapping or unordered unified-diff hunks")
            old_end, new_end = old_begin + old_left, new_begin + new_left
            direction, saw_hunk, marker_allowed = None, True, False
            continue
        if line == "\\ No newline at end of file" and marker_allowed:
            finish()
            direction, marker_allowed = None, False
            continue
        if old_left or new_left:
            prefix = line[:1]
            if prefix == " ":
                old_left -= 1
                new_left -= 1
                change = None
            elif prefix == "+":
                new_left -= 1
                additions += 1
                change = "added"
            elif prefix == "-":
                old_left -= 1
                deletions += 1
                change = "removed"
            else:
                raise ValueError("Invalid unified-diff hunk line")
            if old_left < 0 or new_left < 0:
                raise ValueError("Inconsistent unified-diff span")
            if change is None or change != direction:
                finish()
                direction = change
            if change is not None:
                run.append(line[1:])
            marker_allowed = True
            continue
        if not line.startswith(_HEADERS):
            raise ValueError("Unexpected content outside unified-diff hunk")
        finish()
        direction, marker_allowed = None, False
    if (not saw_hunk and not allow_no_hunk) or old_left or new_left:
        raise ValueError("Missing or truncated unified-diff hunk")
    finish()
    return UnifiedDiff(additions, deletions, runs)


def parse_file_patch(patch: str, *, additions: int, deletions: int) -> UnifiedDiff:
    """Validate line coverage, including complete metadata-only file changes."""
    if any(type(count) is not int or count < 0 for count in (additions, deletions)):
        raise ValueError("Invalid file change counts")
    parsed = parse_unified_diff(patch, allow_no_hunk=additions == 0 and deletions == 0)
    if (parsed.additions, parsed.deletions) != (additions, deletions):
        raise ValueError("File change counts do not match patch")
    return parsed
