"""Human PR text and isolated ownership of Sakura generated sections."""

import re
from collections.abc import Collection

_SECTION_NAMES = frozenset({"summary", "depgraph", "issue-links"})
_SECTION_MARKER = re.compile(
    r"<!--\s*sakura-ai-(summary|depgraph|issue-links)-(start|end)\s*-->"
)


def remove_sakura_generated_sections(
    body: str | None, *, sections: Collection[str] | None = None
) -> str:
    """Remove selected sections without consuming an adjacent generated block.

    A complete outer block owns all nested marker content. A start without
    its matching end terminates at the next recognized start, or EOF.
    This preserves independent sections while still excluding truncated output
    from human evidence. Other text is copied verbatim.
    """
    selected = _SECTION_NAMES if sections is None else frozenset(sections)
    if not selected <= _SECTION_NAMES:
        raise ValueError("Unknown Sakura generated section")
    body = body or ""
    markers = list(_SECTION_MARKER.finditer(body))
    # Pair each kind with a stack before determining ownership. A complete
    # outer section includes every nested marker, even of a different kind.
    # Selective writers must never mutate an intact other writer's content.
    stacks = {kind: [] for kind in _SECTION_NAMES}
    matching_ends = {}
    for index, marker in enumerate(markers):
        kind, boundary = marker.groups()
        if boundary == "start":
            stacks[kind].append(index)
        elif stacks[kind]:
            matching_ends[stacks[kind].pop()] = index

    spans = []
    index = 0
    while index < len(markers):
        marker = markers[index]
        kind, boundary = marker.groups()
        if boundary != "start":
            index += 1
            continue
        end_index = matching_ends.get(index)
        if end_index is not None:
            spans.append((marker.start(), markers[end_index].end(), kind))
            index = end_index + 1
            continue
        # Only an unmatched outer start may recover at the next start. The
        # recovered block is processed independently on the next iteration.
        next_index = index + 1
        while next_index < len(markers) and markers[next_index].group(2) != "start":
            next_index += 1
        end = markers[next_index].start() if next_index < len(markers) else len(body)
        spans.append((marker.start(), end, kind))
        index = next_index
    parts, offset = [], 0
    for start, end, kind in spans:
        if kind in selected:
            parts.append(body[offset:start])
            offset = end
    parts.append(body[offset:])
    return "".join(parts)


def replace_sakura_generated_section(body: str | None, section: str, block: str) -> str:
    """Replace one writer's section while preserving every other section."""
    clean = remove_sakura_generated_sections(body, sections={section}).rstrip()
    if not block:
        return clean
    return f"{clean}\n\n{block}" if clean else block


def strip_sakura_generated_sections(body: str | None) -> str:
    """Return human text only, excluding complete and truncated generated output."""
    return remove_sakura_generated_sections(body).strip()
